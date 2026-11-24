/**
 * B 站视频评论区无头抓取脚本（纯 API 方案，零 npm 依赖，无需浏览器）
 *
 * 原理：
 *   1. /x/web-interface/view?bvid=  → 拿 aid、标题、UP 主、官方评论总数
 *   2. /x/web-interface/nav        → 拿 WBI 签名密钥（img_key/sub_key）并检测登录态
 *   3. /x/v2/reply/wbi/main        → 游标分页拉取主评论（WBI 签名）
 *   4. /x/v2/reply/reply           → 可选（--replies），展开楼中楼回复
 *
 * 用法：
  *   node extract.mjs --url <BV号或视频URL> [--count 200] [--sort hot|time]
 *                    [--output <文件>] [--cookies <Netscape cookie 文件>]
 *                    [--replies] [--delay 1.2] [--max-pages 100]
 *
 * Cookie 失效时的表现见 SKILL.md「Cookie 失效处理」一节。
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import crypto from 'crypto';
import { homedir } from 'os';

const __dirname = dirname(fileURLToPath(import.meta.url));
const dataDir = resolve(homedir(), '.bilibili-comment-extractor');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

// WBI 混排索引表（B 站 Web 端公开算法）
const MIXIN_KEY_ENC_TAB = [
  46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
  27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
  37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
  22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
];

function printHelp() {
  console.log(`用法：
  node extract.mjs --url <BV号或URL> [--count 200] [--sort hot|time]
                   [--output <文件>] [--cookies <cookie文件>]
                   [--replies] [--delay 1.2] [--max-pages 100]

参数：
  --url        必填，视频 URL 或 BV 号
  --count      目标主评论数，默认 200
  --sort       hot=热度（默认）| time=最新（抓全量请用 time，游标可正常翻到底）
  --replies    展开楼中楼（抓全每条主评论下的回复，慢）
  --delay      每次请求间隔秒数，默认 1.2（风控关键参数，不要低于 0.5）
  --max-pages  最多翻页数，默认 100（每页约 20 条主评论）
  --cookies    Netscape 格式 cookie 文件，默认用户数据目录下 www.bilibili.com_cookies.txt
  --output     输出 txt 路径；省略时保存到当前工作目录`);
}

function parseCliArgs(argv) {
  const values = new Map();
  const flags = new Set();
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--help' || token === '-h') return { help: true };
    if (!token.startsWith('--')) throw new Error(`未知参数: ${token}`);
    const equalAt = token.indexOf('=');
    const key = equalAt >= 0 ? token.slice(2, equalAt) : token.slice(2);
    if (key === 'replies') { flags.add('replies'); continue; }
    const value = equalAt >= 0 ? token.slice(equalAt + 1) : argv[++i];
    if (!['url', 'count', 'sort', 'output', 'cookies', 'delay', 'max-pages'].includes(key)) {
      throw new Error(`未知参数: --${key}`);
    }
    if (!value || value.startsWith('--')) throw new Error(`参数 --${key} 缺少值`);
    values.set(key, value);
  }

  const rawUrl = values.get('url');
  if (!rawUrl) throw new Error('缺少必填参数 --url');
  const bvMatch = rawUrl.match(/BV[0-9A-Za-z]{10}/);
  if (!bvMatch) throw new Error('--url 中未找到有效 BV 号');

  const count = Number(values.get('count') || 200);
  if (!Number.isSafeInteger(count) || count < 1) throw new Error('--count 必须是大于 0 的整数');

  const sort = values.get('sort') || 'hot';
  if (!['hot', 'time'].includes(sort)) throw new Error('--sort 只能是 hot 或 time');

  const delay = Number(values.get('delay') || 1.2);
  if (!Number.isFinite(delay) || delay < 0.5) throw new Error('--delay 必须不小于 0.5 秒');

  const maxPages = Number(values.get('max-pages') || 100);
  if (!Number.isSafeInteger(maxPages) || maxPages < 1) throw new Error('--max-pages 必须是大于 0 的整数');

  return {
    help: false,
    bvid: bvMatch[0],
    videoUrl: `https://www.bilibili.com/video/${bvMatch[0]}`,
    count,
    mode: sort === 'hot' ? 3 : 2,
    sortLabel: sort === 'hot' ? '热度' : '最新',
    withReplies: flags.has('replies'),
    delayMs: delay * 1000,
    maxPages,
    cookieFile: values.has('cookies')
      ? resolve(process.cwd(), values.get('cookies'))
      : resolve(dataDir, 'www.bilibili.com_cookies.txt'),
    outputFile: values.has('output') ? resolve(process.cwd(), values.get('output')) : '',
  };
}

// ── Cookie（Netscape 格式 → Cookie 请求头）──────────────────
function loadCookieHeader(path) {
  if (!existsSync(path)) {
    throw new Error(`Cookie 文件不存在: ${path}\n请先运行 get-cookie.mjs 获取，或手动导出 Netscape 格式 Cookie 到该路径`);
  }
  const pairs = [];
  let sessdataExpires = null;
  for (const line of readFileSync(path, 'utf-8').split('\n')) {
    if (line.startsWith('#') || line.trim() === '') continue;
    const parts = line.split('\t');
    if (parts.length < 7) continue;
    pairs.push(`${parts[5]}=${parts.slice(6).join('\t')}`);
    if (parts[5] === 'SESSDATA') sessdataExpires = Number(parts[4]);
  }
  return { header: pairs.join('; '), sessdataExpires };
}

// 启动时检查 SESSDATA 有效期：过期直接终止并指引重新获取，临期给警告
function checkSessdata(sessdataExpires, cookieFile) {
  if (sessdataExpires === null) {
    console.warn('⚠️ Cookie 中没有 SESSDATA，只能匿名抓取（通常只能拿到首页热评，分页会返回空）');
    console.warn('   如需完整抓取，请运行 get-cookie.mjs 获取登录 Cookie');
    return;
  }
  const daysLeft = Math.floor((sessdataExpires * 1000 - Date.now()) / 86400000);
  if (daysLeft < 0) {
    console.error(`❌ SESSDATA 已过期 ${-daysLeft} 天（${new Date(sessdataExpires * 1000).toLocaleDateString('zh-CN')} 到期）`);
    console.error('   请重新获取 Cookie：node get-cookie.mjs（需手动登录一次）');
    console.error(`   或用浏览器扩展导出 Netscape 格式 Cookie 覆盖: ${cookieFile}`);
    process.exit(1);
  }
  console.log(`      SESSDATA 剩余有效期 ${daysLeft} 天`);
  if (daysLeft < 14) {
    console.warn(`      ⚠️ 临期提醒：不足 14 天，建议尽快运行 get-cookie.mjs 刷新`);
  }
}

// ── WBI 签名 ────────────────────────────────────────────────
function wbiSign(params, imgKey, subKey) {
  const mixinKey = MIXIN_KEY_ENC_TAB.map(i => (imgKey + subKey)[i]).join('').slice(0, 32);
  const p = { ...params, wts: Math.floor(Date.now() / 1000) };
  const query = Object.keys(p).sort()
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(String(p[k]).replace(/[!'()*]/g, ''))}`)
    .join('&');
  const wRid = crypto.createHash('md5').update(query + mixinKey).digest('hex');
  return `${query}&w_rid=${wRid}`;
}

// ── HTTP 封装 ───────────────────────────────────────────────
function makeApi(cookieHeader, referer) {
  return async function api(path, params, wbiKeys = null) {
    const query = wbiKeys ? wbiSign(params, wbiKeys.imgKey, wbiKeys.subKey)
                          : new URLSearchParams(params).toString();
    const resp = await fetch(`https://api.bilibili.com${path}?${query}`, {
      headers: {
        'User-Agent': UA,
        'Referer': referer,
        'Cookie': cookieHeader,
        'Accept': 'application/json',
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} @ ${path}`);
    return resp.json();
  };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

const COOKIE_EXPIRED_HINT = '登录态失效（code=-101）。请运行 get-cookie.mjs 重新获取 Cookie 后重试';

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
  if (Number(process.versions.node.split('.')[0]) < 18) {
    console.error(`❌ 需要 Node.js 18+（依赖全局 fetch），当前 v${process.versions.node}`);
    process.exit(1);
  }

  let options;
  try {
    options = parseCliArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`❌ ${error.message}\n`);
    printHelp();
    process.exit(2);
  }
  if (options.help) { printHelp(); process.exit(0); }

  console.log('=== B 站视频评论区无头抓取（纯 API）===\n');

  // 1. Cookie
  const { header: cookieHeader, sessdataExpires } = loadCookieHeader(options.cookieFile);
  console.log(`[1/5] 加载 Cookie（${options.cookieFile}）`);
  checkSessdata(sessdataExpires, options.cookieFile);
  const api = makeApi(cookieHeader, options.videoUrl);

  // 2. 视频信息
  console.log(`[2/5] 解析视频 ${options.bvid} ...`);
  const view = await api('/x/web-interface/view', { bvid: options.bvid });
  if (view.code !== 0) throw new Error(`view 接口返回 code=${view.code} ${view.message}（请确认 BV 号正确、视频未下架）`);
  const v = view.data;
  console.log(`      标题: ${v.title}`);
  console.log(`      UP主: ${v.owner.name} | 分区: ${v.tname} | 官方评论数: ${v.stat.reply}`);
  const aid = v.aid;

  // 3. WBI 密钥（同时检测登录态）
  console.log('[3/5] 获取 WBI 签名密钥...');
  const nav = await api('/x/web-interface/nav', {});
  const imgKey = nav.data.wbi_img.img_url.split('/').pop().replace(/\.\w+$/, '');
  const subKey = nav.data.wbi_img.sub_url.split('/').pop().replace(/\.\w+$/, '');
  const wbiKeys = { imgKey, subKey };
  if (nav.data.isLogin) {
    console.log(`      登录态正常（mid=${nav.data.mid}）`);
  } else {
    console.warn('      ⚠️ nav 接口报告未登录：Cookie 已失效或不完整，只能抓首页热评');
    console.warn('         请运行 get-cookie.mjs 重新获取 Cookie');
  }

  // 4. 游标分页拉取主评论
  // 注意（实测结论）：
  //  - mode=2(时间) 游标正常逐页前进，可深翻到底；
  //  - mode=3(热度) 第 2 页起 next_offset 冻结不变，但同游标重复请求仍返回全新主评论
  //    （实测 8 页 159 条零重复），因此按 rpid 本地去重累计，以「整页无新增」作为到底信号。
  console.log(`[4/5] 拉取主评论（目标 ${options.count} 条，排序=${options.sortLabel}）...`);
  const roots = [];
  const seenRpids = new Set();
  let offset = '';
  let stopReason = '';
  let riskControlHits = 0;
  let noNewPages = 0;
  let offsetFrozen = false;
  let allCount = 0;

  for (let page = 1; page <= options.maxPages; page++) {
    const paginationStr = JSON.stringify({ offset });
    const data = await api('/x/v2/reply/wbi/main', {
      oid: aid, type: 1, mode: options.mode, plat: 1,
      web_location: 1315875, pagination_str: paginationStr,
    }, wbiKeys);

    if (data.code !== 0) {
      if (data.code === -101) {
        console.error(`\n❌ ${COOKIE_EXPIRED_HINT}`);
        process.exit(1);
      }
      if (data.code === -352 || data.code === 412) {
        riskControlHits++;
        if (riskControlHits >= 3) {
          stopReason = `触发风控（code=${data.code}）连续 ${riskControlHits} 次，已停止；可增大 --delay 后重试`;
          break;
        }
        console.log(`      风控 code=${data.code}，等待 ${5 * riskControlHits} 秒后重试...`);
        await sleep(5000 * riskControlHits);
        page--; // 重试本页
        continue;
      }
      stopReason = `接口返回 code=${data.code}（${data.message || '未知错误'}）`;
      break;
    }
    riskControlHits = 0;

    const cursor = data.data.cursor;
    const replies = data.data.replies || [];
    if (page === 1) {
      allCount = cursor.all_count;
      console.log(`      接口报告全部评论数(含楼中楼): ${allCount}`);
      if (replies.length === 0 && Number(v.stat.reply) > 0) {
        console.error('\n❌ 官方评论数大于 0 但接口返回空：Cookie 大概率已失效');
        console.error('   请运行 get-cookie.mjs 重新获取 Cookie 后重试');
        process.exit(1);
      }
    }

    let newOnThisPage = 0;
    for (const r of replies) {
      const key = String(r.rpid);
      if (seenRpids.has(key)) continue;
      seenRpids.add(key);
      newOnThisPage++;
      roots.push({
        rpid: r.rpid,
        uname: r.member.uname,
        level: r.member.level_info.current_level,
        message: r.content.message,
        like: r.like,
        ctime: r.ctime,
        // 楼中楼总数：新接口字段为 count / rcount，旧字段 rcnt 已不下发
        rcnt: r.count ?? r.rcount ?? (r.replies || []).length,
        embedded: (r.replies || []).map(sub => ({
          uname: sub.member.uname,
          upName: r.member.uname,
          message: sub.content.message,
          like: sub.like,
        })),
      });
    }
    console.log(`      第 ${page} 页：+${newOnThisPage} 条新主评论（去重后累计 ${roots.length} 条）`);

    if (roots.length >= options.count) { stopReason = `达到目标数量 ${options.count}`; break; }
    if (cursor.is_end || replies.length === 0) { stopReason = '接口返回 is_end / 无更多数据'; break; }

    // 整页无新增 = 采样/翻页已到底（热度排序的正常终止信号）
    if (newOnThisPage === 0) {
      noNewPages++;
      if (noNewPages >= 3) { stopReason = '连续 3 页无新增评论，判定到底'; break; }
    } else {
      noNewPages = 0;
    }

    const nextOffset = cursor.pagination_reply?.next_offset;
    if (nextOffset && nextOffset !== offset) {
      offset = nextOffset;
    } else if (!offsetFrozen) {
      offsetFrozen = true;
      console.log('      注意：next_offset 冻结（热度排序常见），改用去重累计继续采样');
    }

    await sleep(options.delayMs);
  }
  if (!stopReason) stopReason = `达到最大翻页数 ${options.maxPages}（可加大 --max-pages）`;
  console.log(`      停止原因: ${stopReason}`);

  // 5. 可选：展开楼中楼
  let expandedRoots = 0;
  if (options.withReplies) {
    console.log('[5/5] 展开楼中楼回复...');
    for (const root of roots) {
      if (root.rcnt <= root.embedded.length) continue;
      const full = [];
      const seenSub = new Set();
      let pn = 1;
      while (true) {
        const sub = await api('/x/v2/reply/reply', {
          oid: aid, type: 1, root: String(root.rpid), ps: 10, pn,
        }, wbiKeys);
        if (sub.code === -101) {
          console.error(`\n❌ ${COOKIE_EXPIRED_HINT}`);
          process.exit(1);
        }
        if (sub.code !== 0 || !sub.data?.replies?.length) break;
        for (const s of sub.data.replies) {
          const skey = String(s.rpid);
          if (seenSub.has(skey)) continue;
          seenSub.add(skey);
          full.push({
            uname: s.member.uname,
            upName: s.parent === root.rpid ? root.uname
                  : (full.find(x => x.rpid === s.parent)?.uname || root.uname),
            rpid: s.rpid,
            message: s.content.message,
            like: s.like,
          });
        }
        if (sub.data.replies.length < 10 || seenSub.size >= root.rcnt) break;
        pn++;
        await sleep(options.delayMs);
      }
      if (full.length) {
        root.embedded = full.map(({ rpid, ...rest }) => rest);
        expandedRoots++;
      }
      await sleep(options.delayMs);
    }
    console.log(`      完整展开了 ${expandedRoots} 条主评论的楼中楼`);
  } else {
    console.log('[5/5] 未开启 --replies，楼中楼仅保留每条主评论自带的 2 条预览');
  }

  // 输出（头部统计与正文严格一致：只统计实际写出的部分）
  const outRoots = roots.slice(0, options.count);
  const totalEmbedded = outRoots.reduce((n, r) => n + r.embedded.length, 0);
  const fmtTime = ts => new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });

  let output = `B站视频：${v.title}\n`;
  output += `URL: ${options.videoUrl}\n`;
  output += `BV号: ${options.bvid} / av${aid}\n`;
  output += `UP主: ${v.owner.name} (mid=${v.owner.mid})\n`;
  output += `分区: ${v.tname} | 发布时间: ${new Date(v.pubdate * 1000).toLocaleString('zh-CN', { hour12: false })}\n`;
  output += `官方评论数(含楼中楼): ${v.stat.reply} | 接口 all_count: ${allCount}\n`;
  output += `抓取时间: ${new Date().toLocaleString('zh-CN', { hour12: false })}\n`;
  output += `排序方式: 按${options.sortLabel}\n`;
  output += `本次抓取: ${outRoots.length} 条主评论 + ${totalEmbedded} 条楼中楼\n`;
  if (roots.length > outRoots.length) {
    output += `（另已采集但未写入 ${roots.length - outRoots.length} 条主评论，受 --count 限制）\n`;
  }
  output += `停止原因: ${stopReason}\n`;
  output += `${'='.repeat(80)}\n\n`;

  outRoots.forEach((r, i) => {
    output += `【评论 #${i + 1}】${r.uname} (Lv${r.level})  👍${r.like}  ${fmtTime(r.ctime)}  楼中楼:${r.rcnt}\n\n`;
    output += `${r.message}\n`;
    for (const s of r.embedded) {
      output += `\n  └─ ${s.uname} 回复 @${s.upName}（👍${s.like}）：${s.message.replace(/\n/g, ' ')}`;
    }
    output += `\n\n${'─'.repeat(80)}\n\n`;
  });

  const safeTitle = v.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 30);
  const outPath = options.outputFile || resolve(process.cwd(), `B站评论_${options.bvid}_${safeTitle}.txt`);
  writeFileSync(outPath, output, 'utf-8');

  console.log(`\n✅ 完成！`);
  console.log(`   主评论: 写出 ${outRoots.length} 条（共采集 ${roots.length} 条）| 楼中楼: ${totalEmbedded} 条`);
  console.log(`   大小: ${(Buffer.byteLength(output) / 1024).toFixed(1)} KB`);
  console.log(`   路径: ${outPath}`);
}

main().catch(err => {
  console.error('\n❌ 执行出错:', err.message);
  process.exit(1);
});
