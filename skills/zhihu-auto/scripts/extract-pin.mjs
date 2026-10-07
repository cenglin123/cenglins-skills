#!/usr/bin/env node
/**
 * 想法（pin）抓取 —— API 通道（纯 Node fetch，零依赖、无浏览器）
 *
 * 为什么单独一个脚本：
 *   - 想法是知乎的第三种内容形态（问题/回答、专栏文章、想法），结构与两者都不同：
 *     `content` 是**数组**（text / link / image 混排），不是单段 HTML。
 *   - `extract.mjs` 是问题页专用、`extract-article.mjs` 是专栏文章专用；
 *     对 `www.zhihu.com/pin/<id>` 两者的 URL 解析都会正确拒绝（exit 2）。
 *
 * 实测（2026-10）：`/api/v4/pins/<id>` **不需要 x-zse-96 请求签名**，带 Cookie 即 200 ——
 * 与专栏文章 API（要求签名，只能走 stealth 浏览器）相反。因此本脚本零依赖、无浏览器。
 *
 * 用法：
 *   node extract-pin.mjs --url "https://www.zhihu.com/pin/<id>" [--out <文件>] [--help]
 *   --url 也接受纯数字 id。
 *
 * Cookie：可选。存在则携带（可读需登录的内容），缺失则匿名尝试（公开想法通常可读）。
 *
 * 退出码：0 成功；1 已删除 / 需登录 / 被风控 / 未取到内容；2 用法错误。
 */

import { writeFileSync, existsSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { resolveCookieFile, parseNetscapeCookieText, cookiesToHeader, stripCtrl } from './lib/env.mjs';

const API_HOST = 'https://www.zhihu.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';

function printHelp() {
  console.log(`用法：node extract-pin.mjs --url <想法URL|id> [--out <文件>] [--help]

  抓取一条知乎想法（pin）为 txt。

必填：
  --url <url|id>      https://www.zhihu.com/pin/<id> 或纯数字 id

选项：
  --out <文件>        输出 txt 路径；省略时保存到当前目录（知乎想法_<id>.txt）

Cookie 可选：存在（~/.zhihu-auto/www.zhihu.com_cookies.txt）则携带，缺失则匿名尝试。

退出码：0 成功；1 已删除 / 需登录 / 被风控 / 未取到内容；2 用法错误。`);
}

function parseArgs(argv) {
  if (argv.includes('--help') || argv.includes('-h')) return { help: true };
  // 值型 flag 消费其后的取值；未知参数判定必须跳过这些取值（与 extract-article 同一纪律）
  const VALUE_FLAGS = ['--url', '--out'];
  const unknown = [];
  for (let i = 0; i < argv.length; i++) {
    const t = String(argv[i]);
    const vf = VALUE_FLAGS.find((f) => t === f || t.startsWith(f + '='));
    if (vf) {
      if (!t.includes('=')) {
        const nxt = argv[i + 1];
        if (nxt === undefined || String(nxt).startsWith('-')) {
          const eg = t === '--url' ? 'https://www.zhihu.com/pin/<id>' : '<输出文件.txt>';
        console.error(`❌ 参数 ${t} 缺少取值（例如：${t} ${eg}）`);
          process.exit(2);
        }
        i++;
      }
      continue;
    }
    if (['--help', '-h'].includes(t)) continue;
    unknown.push(t);
  }
  if (unknown.length) {
    console.error(`❌ 未知参数: ${unknown.join(' ')}（只接受 --url / --out / --help）`);
    process.exit(2);
  }
  const eq = argv.find((a) => String(a).startsWith('--url='));
  const url = eq !== undefined ? String(eq).slice('--url='.length) : (argv.includes('--url') ? String(argv[argv.indexOf('--url') + 1]) : null);
  if (!url) { console.error('❌ 缺少 --url（想法链接或纯数字 id）'); process.exit(2); }
  const m = String(url).match(/www\.zhihu\.com\/pin\/(\d{6,})/) || String(url).match(/^(\d{6,})$/);
  if (!m) {
    console.error(`❌ 无法从 --url 解析出想法 id：${url}\n   支持形如 https://www.zhihu.com/pin/<id> 或纯数字 id\n   （专栏文章请用 extract-article.mjs；问题页请用 extract.mjs）`);
    process.exit(2);
  }
  const outEq = argv.find((a) => String(a).startsWith('--out='));
  const out = outEq !== undefined ? String(outEq).slice('--out='.length)
    : (argv.includes('--out') ? String(argv[argv.indexOf('--out') + 1]) : null);
  return { help: false, id: m[1], out: out || null };
}

function stripHtml(h) {
  return (h || '')
    .replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n\n')
    .replace(/<img[^>]*alt="([^"]*)"[^>]*>/gi, '[图:$1]')
    .replace(/<img[^>]*>/gi, '[图]')
    .replace(/<a[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/gi, '$2 ($1)')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\n{3,}/g, '\n\n').trim();
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) return printHelp();

  // Cookie 可选：缺失时匿名尝试（公开想法通常可读），存在则携带
  const cookieFile = resolveCookieFile();
  let cookieHeader = '';
  if (existsSync(cookieFile)) {
    cookieHeader = cookiesToHeader(parseNetscapeCookieText(readFileSync(cookieFile, 'utf-8')), 'www.zhihu.com');
    console.log('[1/3] 携带 Cookie（匿名 + Cookie 的可用性差异未系统验证，遇需登录会明确报错）');
  } else {
    console.log('[1/3] 未找到 Cookie 文件，尝试匿名访问…');
  }

  console.log(`[2/3] GET /api/v4/pins/${opts.id}`);
  const r = await fetch(`${API_HOST}/api/v4/pins/${opts.id}`, {
    headers: { 'User-Agent': UA, Accept: 'application/json', Referer: `${API_HOST}/pin/${opts.id}`, ...(cookieHeader ? { Cookie: cookieHeader } : {}) },
    redirect: 'manual',
  });
  if (r.status >= 300 && r.status < 400) {
    console.error(`❌ HTTP ${r.status} 重定向（带 Cookie 的请求不应跳转）—— 多半是登录墙，请重跑 get-cookie.mjs 刷新登录态。`);
    process.exit(1);
  }
  const body = await r.text();
  if (r.status === 404) {
    console.error('❌ 404：想法不存在或已被删除。');
    process.exit(1);
  }
  if (r.status === 403 || r.status === 401) {
    console.error(`❌ HTTP ${r.status}（${stripCtrl(body.slice(0, 120))}）`);
    console.error('   鉴权/风控失败。注意：这与 Cookie 是否过期要分开判断 ——');
    console.error('   可先用一个公开问题页的 feeds API（limit=1）验证 Cookie 本身是否有效，再决定是否重新登录。');
    process.exit(1);
  }
  if (!r.ok) {
    console.error(`❌ HTTP ${r.status}: ${stripCtrl(body.slice(0, 150))}`);
    process.exit(1);
  }
  let j;
  try { j = JSON.parse(body); } catch {
    console.error('❌ 返回非 JSON（可能是风控页）: ' + stripCtrl(body.slice(0, 120)));
    process.exit(1);
  }
  if (j.error) {
    console.error(`❌ API 错误 code=${j.error.code}: ${stripCtrl(j.error.message || '')}`);
    process.exit(1);
  }
  if (j.is_deleted) { console.error('❌ 该想法已被作者删除。'); process.exit(1); }

  console.log('[3/3] 解析 content[] …');
  // content 是数组：text / link / image 混排，不能当单段 HTML 处理
  let html = '';
  for (const item of (Array.isArray(j.content) ? j.content : [])) {
    if (item && item.content) html += item.content + '\n';
    else if (item && item.type === 'link' && item.url) html += `\n[链接] ${item.title ?? ''} ${item.url}\n`;
    else if (item && item.type === 'image') html += '\n[图]\n';
  }
  if (!html && j.content_html) html = String(j.content_html);
  const text = stripHtml(html);
  if (!text) {
    console.error('❌ 未取到文本内容（可能是纯图片想法，或结构改版）。');
    if (Array.isArray(j.content)) console.error('   content 类型: ' + j.content.map((c) => c.type).join(', '));
    process.exit(1);
  }

  const src = j.source_pin_id ? `\n（这是一条转发，源想法 id: ${j.source_pin_id}）` : '';
  const outPath = resolve(opts.out || `知乎想法_${opts.id}.txt`);
  const L = [
    `类型: 想法 (pin)`,
    `作者: ${j.author?.name ?? '(匿名)'}${j.author?.headline ? '  ｜  ' + j.author.headline : ''}`,
    `发布: ${j.created ? new Date(j.created * 1000).toLocaleString('zh-CN') : '?'}  更新: ${j.updated ? new Date(j.updated * 1000).toLocaleString('zh-CN') : '?'}`,
    `赞同: ${j.like_count ?? '?'}  评论: ${j.comment_count ?? '?'}  转发: ${j.repin_count ?? '?'}${j.page_view_count != null ? '  浏览: ' + j.page_view_count : ''}`,
    `URL: ${API_HOST}/pin/${opts.id}`,
    `字数: ${text.length}`,
    '='.repeat(70),
    '',
    text,
    src,
  ];
  writeFileSync(outPath, L.join('\n'), 'utf-8');

  console.log(`  作者: ${j.author?.name ?? '(匿名)'}   赞同 ${j.like_count ?? '?'}  评论 ${j.comment_count ?? '?'}   正文 ${text.length} 字`);
  if (j.content?.some((c) => c.type === 'image')) console.log('  ⚠️ 含图片：图片里的文字（截图等）不会出现在输出里。');
  console.log(`  输出: ${outPath}`);
}

main().catch((err) => {
  console.error('\n❌ ' + stripCtrl(err && err.message || err));
  process.exit(1);
});
