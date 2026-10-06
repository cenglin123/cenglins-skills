#!/usr/bin/env node
/**
 * 专栏文章抓取（zhuanlan.zhihu.com/p/<id>）—— 渲染通道
 *
 * 为什么单独一个脚本而不并进 extract.mjs：
 *   - extract.mjs 的解析器是**问题页**专用的（.AnswerItem / 回答列表 / 「查看剩余」按钮），
 *     在专栏文章页上跑不出任何回答；
 *   - 专栏文章页是**单篇正文**（[itemprop="articleBody"]），结构、交互、防爬挑战都不同。
 *
 * 为什么必须走真实浏览器：
 *   2026-10 实测，知乎 HTML 层对无签名访问统一返回 403 + `zh-zse-ck` 挑战页
 *   （API 层 `/api/v4/articles/<id>` 则要求 `x-zse-96` 请求签名，本脚本不实现签名绕过）。
 *   stealth 浏览器能通过该挑战 —— 匿名即可读公开文章，**不注入 Cookie**，
 *   因此登录墙 / 付费 / 仅关注者可见的文章**明确不支持**（见下方 detect）。
 *
 * 用法：
 *   node extract-article.mjs --url "https://zhuanlan.zhihu.com/p/<id>" [--out <文件>] [--timeout <秒>] [--help]
 *   --url 也接受纯数字文章 id。
 *
 * 输出：txt（元信息头 + 正文 + 外链附录）。LinkCard（无文字的外链卡片）会解码
 * `link.zhihu.com/?target=<urlencoded>` 还原真实目标地址。
 *
 * 退出码：0 成功；1 页面被拦 / 未取到正文 / 需登录；2 用法错误。
 */

import { writeFileSync, existsSync } from 'fs';
import { resolve } from 'path';
import { loadPuppeteer, stripCtrl } from './lib/env.mjs';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';

// ── Chrome 自动检测（Chromium 内核均可；含 Edge 兜底）──
function findChromium() {
  const cands = [
    process.env.LOCALAPPDATA + '\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    process.env.LOCALAPPDATA + '\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  ];
  for (const c of cands) { try { if (c && existsSync(c)) return c; } catch { /* ignore */ } }
  return null;
}

function printHelp() {
  console.log(`用法：node extract-article.mjs --url <专栏文章URL|文章id> [选项]

  抓取一篇知乎专栏文章的正文为 txt（匿名可读文章；登录墙/付费文章不支持）。

必填：
  --url <url|id>      https://zhuanlan.zhihu.com/p/<id> 、https://www.zhihu.com/p/<id> 或纯数字 id

选项：
  --out <文件>        输出 txt 路径；省略时保存到当前目录（知乎文章_<id>.txt）
  --timeout <秒>      页面加载超时，默认 90
  --no-links          不在输出末尾附外链清单
  --help              显示本帮助

退出码：0 成功；1 页面被拦 / 需登录 / 未取到正文；2 用法错误。
说明：公开文章**匿名即可读**，本脚本不注入 Cookie —— 登录墙文章会明确报不支持，
     而不是把你的登录态带进浏览器进程。`);
}

function parseArgs(argv) {
  if (argv.includes('--help') || argv.includes('-h')) return { help: true };
  const get = (flag) => {
    const eq = argv.find((a) => String(a).startsWith(flag + '='));
    if (eq !== undefined) return String(eq).slice(flag.length + 1);
    const i = argv.indexOf(flag);
    if (i >= 0) {
      const nxt = argv[i + 1];
      if (nxt === undefined || String(nxt).startsWith('-')) {
        console.error(`❌ 参数 ${flag} 缺少取值（例如：${flag}=600 或 ${flag} 600）`);
        process.exit(2);
      }
      return String(nxt);
    }
    return null;
  };
  const url = get('--url');
  if (!url) { console.error('❌ 缺少 --url（专栏文章链接或纯数字 id）'); process.exit(2); }
  // 值型 flag 会消费其后的取值；未知参数判定必须跳过这些取值，
  // 否则 --out <路径> 里的路径会被当未知参数（与 get-cookie A16 同类缺陷，勿再手写一遍）
  const VALUE_FLAGS = ['--url', '--out', '--timeout'];
  const unknown = [];
  for (let i = 0; i < argv.length; i++) {
    const t = String(argv[i]);
    const vf = VALUE_FLAGS.find((f) => t === f || t.startsWith(f + '='));
    if (vf) { if (!t.includes('=')) i++; continue; }
    if (['--no-links', '--help', '-h'].includes(t)) continue;
    unknown.push(t);
  }
  if (unknown.length) {
    console.error(`❌ 未知参数: ${unknown.join(' ')}（只接受 --url / --out / --timeout / --no-links / --help）`);
    process.exit(2);
  }
  const m = String(url).match(/(?:zhuanlan\.zhihu\.com\/p\/|^\/?p\/|^)(\d{6,})\/?$/)
         || String(url).match(/www\.zhihu\.com\/p\/(\d{6,})/);
  if (!m) {
    console.error(`❌ 无法从 --url 解析出文章 id：${url}\n   支持形如 https://zhuanlan.zhihu.com/p/<id> 或纯数字 id`);
    process.exit(2);
  }
  const timeoutSec = Number(get('--timeout') || 90);
  if (!Number.isFinite(timeoutSec) || timeoutSec < 15) {
    console.error('❌ --timeout 必须 >= 15 秒');
    process.exit(2);
  }
  return {
    help: false,
    id: m[1],
    url: /zhuanlan\.zhihu\.com|www\.zhihu\.com/.test(String(url)) ? String(url) : `https://zhuanlan.zhihu.com/p/${m[1]}`,
    out: get('--out') || null,
    timeoutSec,
    links: !argv.includes('--no-links'),
  };
}

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) return printHelp();

  const exe = findChromium();
  if (!exe) {
    console.error('❌ 未找到 Chrome / Edge。请安装，或设置 CHROME_PATH 指向 Chromium 内核浏览器可执行文件。');
    process.exit(1);
  }
  console.log(`[1/4] 浏览器: ${exe}`);
  const { puppeteer, StealthPlugin } = await loadPuppeteer();
  puppeteer.use(StealthPlugin());

  const browser = await puppeteer.launch({
    executablePath: exe,
    headless: true,
    defaultViewport: { width: 1280, height: 900 },
    args: ['--no-first-run', '--disable-blink-features=AutomationControlled', '--lang=zh-CN', '--window-size=1280,900'],
  });

  let data;
  try {
    const page = await browser.newPage();
    await page.setUserAgent(UA);
    console.log(`[2/4] 打开 ${opts.url}`);
    const resp = await page.goto(opts.url, { waitUntil: 'networkidle2', timeout: opts.timeoutSec * 1000 });
    console.log(`      HTTP ${resp ? resp.status() : '?'}  最终 URL: ${page.url().slice(0, 90)}`);
    // 等 React 渲染 + 懒加载图片占位
    await new Promise((r) => setTimeout(r, 2500));

    console.log('[3/4] 解析正文…');
    data = await page.evaluate(() => {
      const g = (s) => document.querySelector(s);
      const head = (document.body?.innerText || '').slice(0, 600);
      const blocked = /异常|限制本次访问|安全验证|40362|10003/.test(head);
      const loginWall = /登录后即可|开通会员|仅关注者可见|付费咨询/.test(head)
        || !!g('[class*="SignFlowHomepage"], [class*="Modal-wrapper"]');
      const title = (g('h1.Post-Title') || g('[itemprop="headline"]') || g('h1'))?.innerText?.trim() || '';
      const author = (g('.AuthorInfo-name .UserLink-link') || g('[itemprop="author"] [itemprop="name"]'))?.innerText?.trim() || '';
      const authorHead = (g('.AuthorInfo-badgeText') || g('.AuthorInfo-badge'))?.innerText?.trim() || '';
      const votes = (g('button.VoteButton--up') || g('[aria-label*="赞同"]'))?.innerText?.trim() || '';
      const dateMeta = g('meta[itemprop="datePublished"]')?.content || '';
      const root = g('[itemprop="articleBody"]') || g('.Post-RichTextContainer') || g('.RichText');
      const body = root?.innerText?.trim() || '';
      const links = [];
      if (root) {
        const seen = new Set();
        for (const a of root.querySelectorAll('a[href]')) {
          let href = a.href || '';
          const text = (a.innerText || '').trim();
          if (/zhihu\.com\/(topic|people|pin|question|zhida\.zhihu\.com)/.test(href)) continue;
          try {
            const u = new URL(href);
            if (u.hostname === 'link.zhihu.com') {
              const t = u.searchParams.get('target');
              if (t) href = decodeURIComponent(t);
            }
          } catch { /* 保留原 href */ }
          if (!/^https?:/i.test(href)) continue;
          const key = href;
          if (seen.has(key)) continue;
          seen.add(key);
          links.push({ text, href });
        }
      }
      return {
        blocked, loginWall, title, author, authorHead, votes, dateMeta, body, links,
        finalUrl: location.href,
        textLen: (document.body?.innerText || '').length,
      };
    });

    if (data.blocked) {
      console.error('\n❌ 页面被知乎风控拦截（命中「异常/限制访问/安全验证」文案）。');
      console.error('   这是 IP/指纹层的限制，不是 Cookie 过期 —— 本脚本不注入 Cookie，等一段时间再试，');
      console.error('   或改用真实浏览器人工打开后手动复制正文。');
      process.exit(1);
    }
    if (data.loginWall && !data.body) {
      console.error('\n❌ 该文章需要登录 / 会员 / 关注后可见 —— 本脚本匿名访问，不支持此场景。');
      process.exit(1);
    }
    if (!data.body) {
      console.error(`\n❌ 未取到正文。页面可见字符数 ${data.textLen}，最终 URL: ${data.finalUrl}`);
      console.error('   可能原因：文章已删除/仅自己可见、结构改版、或被重定向到其他页面。');
      process.exit(1);
    }

    const outPath = resolve(opts.out || `知乎文章_${opts.id}.txt`);
    const L = [
      `标题: ${data.title || '(无)'}`,
      `作者: ${data.author || '(匿名)'}${data.authorHead ? '  ｜  ' + data.authorHead : ''}`,
      `赞同: ${data.votes || '(未知)'}${data.dateMeta ? '  发布: ' + data.dateMeta : ''}`,
      `URL: https://zhuanlan.zhihu.com/p/${opts.id}`,
      `字数: ${data.body.length}`,
      '='.repeat(70),
      '',
      data.body,
    ];
    if (opts.links && data.links.length) {
      L.push('', '-'.repeat(70), '', `附录：正文外链（${data.links.length} 条，LinkCard 已解码）`);
      for (const l of data.links) L.push(`  ${l.text ? `[${l.text}] ` : ''}${l.href}`);
    }
    writeFileSync(outPath, L.join('\n'), 'utf-8');

    console.log('[4/4] 完成');
    console.log(`  标题: ${data.title || '(无)'}`);
    console.log(`  作者: ${data.author || '(匿名)'}   正文 ${data.body.length} 字   外链 ${data.links.length} 条`);
    console.log(`  输出: ${outPath}`);
  } catch (err) {
    console.error(`\n❌ ${stripCtrl(err && err.message || err)}`);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error('\n❌ ' + stripCtrl(err && err.message || err));
  process.exit(1);
});
