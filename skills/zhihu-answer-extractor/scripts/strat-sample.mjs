#!/usr/bin/env node
/**
 * 全量枚举 + 分层随机抽样（争议题采样分析 · 第一步）
 *
 * 知乎问题页只按「赞同排序」渲染前 N 条，直接用 extract.mjs 抓前 N 条会得到
 * 「胜利者框架」——高赞被最响亮的一方垄断，少数派和噪音被排序机制滤掉。
 * 本脚本改走 answers API 做全量枚举（limit=20 分页，无浏览器），拿到每条的
 * 精确赞同数/作者/时间/正文，再按赞同数分层做随机抽样，得到可外推的立场普查样本。
 *
 * 与 extract.mjs 的分工：
 *   - extract.mjs    ：渲染页，拿「前 N 条高赞」的完整正文（含图片说明），适合总结主流观点
 *   - strat-sample.mjs：走 API 拿「全量 + 元数据」，做分层抽样，适合估计真实立场分布
 *
 * 只用 Node 18+ 全局 fetch，无需浏览器、无额外依赖；复用同一份 Netscape Cookie。
 *
 * 用法：
 *   node strat-sample.mjs --url https://www.zhihu.com/question/<qid> [选项]
 *   node strat-sample.mjs --url <qid> --census <已有census.json>   # 跳过枚举，重抽样
 *   node strat-sample.mjs --help
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { resolveCookieFile } from './lib/env.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const COOKIE_FILE = resolveCookieFile();

// API 硬上限：limit 超过 20 会返回 400（经验值，接口可能变化）
const API_LIMIT = 20;
const API_HOST = 'https://www.zhihu.com';
const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';
const INCLUDE =
  'data%5B*%5D.content%2Cvoteup_count%2Cauthor.name%2Ccreated_time%2Ccomment_count';

const DEFAULT_BANDS = '0,1,5,10,25,50,100,250,500,1000,2500';

// ── 工具函数 ─────────────────────────────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function printHelp() {
  console.log(`用法：
  node strat-sample.mjs --url <知乎问题URL|qid> [选项]

必填：
  --url <url|qid>     知乎问题链接或纯数字 qid（如 --census 已给则可省）

选项：
  --per-band <n>      每层抽样数，默认 15
  --bands <a,b,c,...> 赞同分层上界（逗号分隔，末层自动 +∞）
                      默认 ${DEFAULT_BANDS}
  --seed <n>          随机种子，默认 42（同种子同结果，可复现）
  --facets <file>     阵营关键词 JSON（可选），用于给每条样本打 auto_hint
  --out-dir <dir>     输出目录，默认当前目录
  --census <file>     复用已有 census JSON，跳过枚举
  --order-by <mode>   枚举排序，默认 default
  --throttle <ms>     分页基础间隔，默认 1200
  --max-pages <n>     分页安全上限，默认 400
  --text-cap <n>      sample.txt 中单条正文截断长度，默认 1200
  --help              显示帮助`);
}

function parseArgs(argv) {
  const values = new Map();
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--help' || token === '-h') return { help: true };
    if (!token.startsWith('--')) throw new Error(`未知参数: ${token}`);
    const eq = token.indexOf('=');
    const key = eq >= 0 ? token.slice(2, eq) : token.slice(2);
    const val = eq >= 0 ? token.slice(eq + 1) : argv[++i];
    if (val === undefined || (val.startsWith('--') && eq < 0 && key !== 'help')) {
      throw new Error(`参数 --${key} 缺少值`);
    }
    values.set(key, val);
  }
  const url = values.get('url') || '';
  let qid = '';
  const m = String(url).match(/(\d{6,})/);
  if (m) qid = m[1];
  if (!qid && !values.has('census')) throw new Error('缺少 --url（或提供 --census）');
  return {
    help: false,
    qid,
    perBand: Number(values.get('per-band') || 15),
    bands: (values.get('bands') || DEFAULT_BANDS).split(',').map((s) => Number(s.trim())),
    seed: Number(values.get('seed') || 42),
    facets: values.get('facets') || '',
    outDir: values.get('out-dir') || process.cwd(),
    census: values.get('census') || '',
    orderBy: values.get('order-by') || 'default',
    throttle: Number(values.get('throttle') || 1200),
    maxPages: Number(values.get('max-pages') || 400),
    textCap: Number(values.get('text-cap') || 1200),
  };
}

// Netscape Cookie → Cookie 请求头
function loadCookieHeader(path) {
  if (!existsSync(path)) {
    throw new Error(`Cookie 文件不存在: ${path}\n请先运行 get-cookie.mjs 获取`);
  }
  const pairs = [];
  for (const line of readFileSync(path, 'utf-8').split('\n')) {
    if (line.startsWith('#') || !line.trim()) continue;
    const p = line.split('\t');
    if (p.length < 7) continue;
    pairs.push(`${p[5]}=${p.slice(6).join('\t')}`);
  }
  return pairs.join('; ');
}

const ENTITIES = [
  ['&nbsp;', ' '], ['&amp;', '&'], ['&lt;', '<'], ['&gt;', '>'],
  ['&quot;', '"'], ['&#39;', "'"], ['&ldquo;', '“'], ['&rdquo;', '”'], ['&hellip;', '…'],
];

function stripHtml(html) {
  let h = html || '';
  h = h.replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n');
  h = h.replace(/<[^>]+>/g, '');
  for (const [a, b] of ENTITIES) h = h.split(a).join(b);
  return h.trim();
}

// 可复现 PRNG（mulberry32）
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(arr, rng) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// ── 枚举 ─────────────────────────────────────────────────────

async function fetchPage(qid, offset, orderBy, cookieHeader) {
  const url =
    `${API_HOST}/api/v4/questions/${qid}/answers` +
    `?limit=${API_LIMIT}&offset=${offset}&order_by=${orderBy}&include=${INCLUDE}`;
  const res = await fetch(url, {
    headers: {
      'User-Agent': USER_AGENT,
      Referer: `${API_HOST}/question/${qid}`,
      Cookie: cookieHeader,
      Accept: 'application/json, text/plain, */*',
      'Accept-Language': 'zh-CN,zh;q=0.9',
    },
  });
  if (res.status === 403 || res.status === 401) {
    throw new Error('HTTP ' + res.status + '（Cookie 可能失效，请重跑 get-cookie.mjs）');
  }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

async function enumerateAll(opts, cookieHeader) {
  const rows = [];
  let offset = 0;
  let totals = null;
  let fails = 0;
  let ended = false;
  const pages = [];
  while (pages.length < opts.maxPages) {
    let data;
    try {
      const json = await fetchPage(opts.qid, offset, opts.orderBy, cookieHeader);
      totals = json.paging.totals;
      data = json.data || [];
      fails = 0;
    } catch (err) {
      fails++;
      console.log(`  分页 offset=${offset} 失败(${fails}/5): ${err.message}`);
      if (fails >= 5) throw err;
      await sleep(3000);
      continue;
    }
    for (const a of data) {
      rows.push({
        id: a.id,
        author: (a.author && a.author.name) || null,
        votes: a.voteup_count || 0,
        comments: a.comment_count ?? null,
        created: a.created_time || null,
        text: stripHtml(a.content),
      });
    }
    pages.push(offset);
    if (pages.length % 10 === 0) console.log(`  已枚举 ${rows.length}/${totals ?? '?'}`);
    if (!data.length || offset + API_LIMIT >= (totals ?? Infinity)) { ended = true; break; }
    offset += API_LIMIT;
    await sleep(opts.throttle * (0.6 + Math.random() * 0.8));
  }
  const truncated = !ended && totals != null && rows.length < totals;
  return { totals, rows, truncated };
}

// ── 分层与抽样 ───────────────────────────────────────────────

function buildBands(edges) {
  if (!edges.length || edges[0] !== 0) {
    throw new Error('分层下界必须以 0 开始，否则低赞回答会静默落入最高层、污染权重。请让 --bands 以 0 开头。');
  }
  for (let i = 1; i < edges.length; i++) {
    if (!(edges[i] > edges[i - 1])) throw new Error(`分层上界必须严格递增：${edges[i - 1]} -> ${edges[i]}`);
  }
  const out = [];
  for (let i = 0; i < edges.length; i++) {
    const lo = edges[i];
    const hi = i + 1 < edges.length ? edges[i + 1] : Infinity;
    out.push({ lo, hi, label: hi === Infinity ? `[${lo},inf)` : `[${lo},${hi})` });
  }
  return out;
}

function stratumOf(votes, bands) {
  for (const b of bands) if (votes >= b.lo && votes < b.hi) return b;
  return bands[bands.length - 1];
}

// ── 主流程 ───────────────────────────────────────────────────

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) return printHelp();

  const bandDefs = buildBands(opts.bands);
  const outDir = resolve(opts.outDir);
  mkdirSync(outDir, { recursive: true });

  let rows;
  let totals = null;
  let truncated = false;
  let censusPath = opts.census ? resolve(opts.census) : resolve(outDir, `${opts.qid}_census.json`);

  if (opts.census && existsSync(censusPath)) {
    console.log(`[1/4] 复用 census: ${censusPath}`);
    const raw = JSON.parse(readFileSync(censusPath, 'utf-8'));
    if (Array.isArray(raw)) {
      rows = raw;
    } else {
      rows = raw.rows;
      totals = raw.totals ?? null;
      truncated = !!raw.truncated;
      if (raw.qid && !opts.qid) opts.qid = raw.qid;
    }
  } else {
    console.log('[1/4] 全量枚举（answers API，limit=20 分页）...');
    const cookieHeader = loadCookieHeader(COOKIE_FILE);
    const res = await enumerateAll(opts, cookieHeader);
    rows = res.rows;
    totals = res.totals;
    truncated = res.truncated;
    console.log(`      枚举完成：${rows.length} 条（页面报告 ${totals}）`);
    if (truncated) {
      console.log(`      ⚠️ 达到 --max-pages=${opts.maxPages} 截断：仅枚举 ${rows.length}/${totals} 条，占比会被系统性高估`);
    }
  }

  if (!opts.qid) {
    throw new Error('无法确定 qid：请提供 --url，或使用本脚本生成的（含 qid 的）census 文件');
  }

  // 稳定编号：按赞同降序，rid 从 1 开始
  rows.sort((a, b) => (b.votes || 0) - (a.votes || 0) || (a.id > b.id ? 1 : -1));
  rows.forEach((r, i) => (r.rid = i + 1));
  const N = rows.length;

  if (!opts.census || !existsSync(censusPath)) {
    writeFileSync(
      censusPath,
      JSON.stringify({ qid: opts.qid, totals: totals ?? N, truncated, fetched_at: new Date().toISOString(), rows }, null, 1),
      'utf-8',
    );
    console.log(`      census 已保存: ${censusPath}`);
  }

  // 分层统计
  const byBand = new Map();
  for (const b of bandDefs) byBand.set(b.label, []);
  for (const r of rows) byBand.get(stratumOf(r.votes || 0, bandDefs).label).push(r);

  // 分层随机抽样
  const rng = mulberry32(opts.seed);
  const sampled = [];
  for (const b of bandDefs) {
    const pool = shuffle(byBand.get(b.label), rng);
    const take = pool.slice(0, Math.min(opts.perBand, pool.length));
    for (const r of take) sampled.push(r);
  }

  // 关键词自动提示（可选）
  let facets = null;
  if (opts.facets) {
    facets = JSON.parse(readFileSync(resolve(opts.facets), 'utf-8'));
  }
  const autoHint = (text) => {
    if (!facets) return [];
    const hits = [];
    for (const [code, def] of Object.entries(facets)) {
      const matched = (def.markers || []).filter((m) => text.includes(m));
      if (matched.length) hits.push({ code, label: def.label || code, matched });
    }
    return hits;
  };

  // 写 sample.txt（供 agent 精读）
  const L = [];
  L.push(`全量枚举 + 分层随机抽样`);
  L.push(`问题 qid: ${opts.qid}`);
  L.push(`总体: ${N} 条回答（页面报告 ${totals ?? '?'}）`);
  if (truncated) L.push(`⚠️ 枚举被 --max-pages 截断：仅 ${N}/${totals} 条；据此外推的占比会被高估，勿直接采信`);
  L.push(`分层: ${bandDefs.map((b) => b.label).join(' ')}`);
  L.push(`每层抽样: ${opts.perBand}   种子: ${opts.seed}   合计抽样: ${sampled.length}`);
  if (facets) L.push(`阵营关键词: ${Object.entries(facets).map(([c, d]) => `${c}=${d.label || c}`).join(', ')}`);
  L.push('='.repeat(80));
  for (const b of bandDefs) {
    const take = sampled.filter((r) => stratumOf(r.votes || 0, bandDefs).label === b.label)
      .sort((x, y) => (y.votes || 0) - (x.votes || 0));
    L.push(`层 ${b.label}：总体 ${byBand.get(b.label).length} 条，抽 ${take.length} 条`);
  }
  L.push('='.repeat(80));
  for (const b of bandDefs) {
    const take = sampled.filter((r) => stratumOf(r.votes || 0, bandDefs).label === b.label)
      .sort((x, y) => (y.votes || 0) - (x.votes || 0));
    for (const r of take) {
      let txt = (r.text || '').replace(/\s*\n+\s*/g, ' ').trim();
      if (txt.length > opts.textCap) txt = txt.slice(0, opts.textCap) + ' …[截断]';
      const hint = autoHint(r.text || '');
      const hintStr = hint.length ? ` [hint:${hint.map((h) => h.code).join('/')}]` : '';
      L.push(`[rid=${r.rid}] 层=${b.label} 赞=${r.votes || 0} 评=${r.comments ?? '-'}${hintStr} | ${txt}`);
    }
  }
  const samplePath = resolve(outDir, `${opts.qid}_sample.txt`);
  writeFileSync(samplePath, L.join('\n'), 'utf-8');

  // 写 ledger.json（agent 填 verdict；脚本不替 agent 做判断）
  const ledger = {
    qid: opts.qid,
    seed: opts.seed,
    per_band: opts.perBand,
    bands: bandDefs.map((b) => b.label),
    categories: facets
      ? Object.fromEntries(Object.entries(facets).map(([c, d]) => [c, d.label || c]))
      : null,
    rows: sampled.map((r) => ({
      rid: r.rid,
      id: r.id,
      stratum: stratumOf(r.votes || 0, bandDefs).label,
      votes: r.votes || 0,
      author: r.author,
      auto_hint: autoHint(r.text || '').map((h) => h.code),
      verdict: '',
      text: r.text,
    })),
  };
  const ledgerPath = resolve(outDir, `${opts.qid}_ledger.json`);
  writeFileSync(ledgerPath, JSON.stringify(ledger, null, 1), 'utf-8');

  // 写 stats.json
  const stats = {
    qid: opts.qid,
    population: N,
    reported_totals: totals ?? N,
    truncated,
    total_votes: rows.reduce((s, r) => s + (r.votes || 0), 0),
    sampled: sampled.length,
    bands: bandDefs.map((b) => ({
      label: b.label,
      size: byBand.get(b.label).length,
      votes: byBand.get(b.label).reduce((s, r) => s + (r.votes || 0), 0),
      sampled: sampled.filter((r) => stratumOf(r.votes || 0, bandDefs).label === b.label).length,
    })),
  };
  const statsPath = resolve(outDir, `${opts.qid}_stats.json`);
  writeFileSync(statsPath, JSON.stringify(stats, null, 1), 'utf-8');

  console.log('[4/4] 完成');
  console.log(`      样本: ${sampled.length} / ${N}`);
  console.log(`      ${samplePath}`);
  console.log(`      ${ledgerPath}`);
  console.log(`      ${statsPath}`);
  console.log('');
  console.log('下一步：精读 sample.txt，按类别判读每条 rid 的立场，写成 verdicts.txt（格式见 stance-estimate.mjs --help），');
  console.log('然后运行 stance-estimate.mjs 得到设计加权占比与置信区间。');
}

main().catch((err) => {
  console.error('\n❌ ' + err.message);
  process.exit(1);
});
