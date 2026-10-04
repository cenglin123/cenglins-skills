#!/usr/bin/env node
/**
 * 评论层抓取（争议题采样分析 · 可选第三步）
 *
 * 抓取「回答下面的热评」，作为与回答区**分开报告**的第二观察层（评论是回答的附属、非独立样本）。
 *
 * 为什么要单独一层（而不是并进答案立场统计）：
 *   - 回答区容易被排序机制压成同质化（经验：争议题高赞区常 ~85-90% 一边倒），
 *     而反驳/质疑/反例往往沉在评论区——评论是「异议暴露层」。
 *   - 但评论是**挂在具体回答下的、非独立的**样本：同意靠点赞、反对才留言，
 *     评论区会系统性放大分歧、压低共识，且更易被刷。
 *   - 所以回答区给「立场分布」，评论区给「反应/争议点分布」，二者不可混算。
 *
 * 复用 answers 评论 API（纯 fetch，无浏览器），answer_id 来自 census/ledger：
 *   - 列表：GET /api/v4/answers/{aid}/comments?limit=20&offset=N&order_by=score
 *   - 楼中楼：GET /api/v4/comments/{cid}/child_comments?limit=20
 * 注意评论的赞数字段是 vote_count（不是 answers 的 voteup_count），
 * 作者在 author.member.name。
 *
 * 用法：
 *   # 只抓分层抽样那批回答的热评（推荐，请求量可控）
 *   node fetch-comments.mjs --census <qid>_census.json --ledger <qid>_ledger.json \
 *        --only-sampled --per-answer 10
 *   # 抓全量回答的热评（按赞同降序，可用 --top / --max-answers 限流）
 *   node fetch-comments.mjs --census <qid>_census.json --per-answer 5 --top 100
 *   node fetch-comments.mjs --help
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { resolveCookieFile, parseNetscapeCookieText, sanitizeQid, cookiesToHeader, stripCtrl } from './lib/env.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const COOKIE_FILE = resolveCookieFile();
const API_HOST = 'https://www.zhihu.com';
const API_LIMIT = 20;
const USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function printHelp() {
  console.log(`用法：
  node fetch-comments.mjs --census <census.json> [选项]

必填：
  --census <file>      strat-sample.mjs 产出的 census.json（提供 answer_id / 赞同 / 作者）

选择抓取范围（二选一）：
  --only-sampled       只抓 --ledger 里被抽中的回答（推荐；请求量最小）
  --top <n>            抓赞同最高的前 n 条回答（0=全部，默认 0）

选项：
  --ledger <file>      --only-sampled 时必填（strat-sample 产出的 ledger.json）
  --per-answer <k>     每条回答取前 k 条热评，默认 10
  --replies <k>        每条热评再抓前 k 条楼中楼，默认 0（0=不抓；每加 1 会显著增加请求量）
  --order-by <mode>    热评排序 score（默认，按赞）或 time（按时间）
  --out-dir <dir>      输出目录，默认当前目录
  --throttle <ms>      请求基础间隔，默认 1100
  --max-answers <n>    安全上限：最多抓多少条回答，默认 0（不限）
  --help

输出：
  <qid>_comments.json   结构化评论（含楼中楼）
  <qid>_comments.txt    可读版（供 agent 精读）

成本提示（约数，视网络）：每条约 1 个请求（--replies 0）；抽样 165 条约数分钟，
全量 1500 条约数十分钟。楼中楼每加一条会按「热评数」成倍增加请求。`);
}

function parseArgs(argv) {
  const values = new Map();
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--help' || token === '-h') return { help: true };
    if (!token.startsWith('--')) throw new Error(`未知参数: ${token}`);
    const eq = token.indexOf('=');
    const key = eq >= 0 ? token.slice(2, eq) : token.slice(2);
    const flag = ['only-sampled', 'help'].includes(key);
    const val = flag ? true : eq >= 0 ? token.slice(eq + 1) : argv[++i];
    if (!flag && (val === undefined || (val.startsWith('--') && eq < 0))) {
      throw new Error(`参数 --${key} 缺少值`);
    }
    values.set(key, val);
  }
  if (!values.get('census')) throw new Error('缺少 --census');
  if (values.get('only-sampled') && !values.get('ledger')) {
    throw new Error('--only-sampled 需要同时提供 --ledger');
  }
  return {
    help: false,
    census: values.get('census'),
    ledger: values.get('ledger') || '',
    onlySampled: !!values.get('only-sampled'),
    top: Number(values.get('top') || 0),
    perAnswer: Number(values.get('per-answer') || 10),
    replies: Number(values.get('replies') || 0),
    orderBy: values.get('order-by') || 'score',
    outDir: values.get('out-dir') || process.cwd(),
    throttle: Number(values.get('throttle') || 1100),
    maxAnswers: Number(values.get('max-answers') || 0),
  };
}

function loadCookieHeader(path) {
  if (!existsSync(path)) {
    throw new Error(`Cookie 文件不存在: ${path}\n请先运行 get-cookie.mjs 获取，或从备份恢复`);
  }
  return cookiesToHeader(parseNetscapeCookieText(readFileSync(path, 'utf-8')), 'www.zhihu.com');
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

function readJson(path) {
  const raw = JSON.parse(readFileSync(resolve(path), 'utf-8'));
  return Array.isArray(raw) ? { rows: raw } : raw;
}

async function apiGet(url, cookieHeader) {
  const res = await fetch(url, {
    redirect: 'manual',
    headers: {
      'User-Agent': USER_AGENT,
      Referer: `${API_HOST}/`,
      Cookie: cookieHeader,
      Accept: 'application/json, text/plain, */*',
      'Accept-Language': 'zh-CN,zh;q=0.9',
    },
  });
  if (res.status >= 300 && res.status < 400) {
    throw new Error('HTTP ' + res.status + ' 重定向（带 Cookie 的请求不应跳转）');
  }
  if (res.status === 403 || res.status === 401) {
    throw new Error('HTTP ' + res.status + '（Cookie 可能失效）');
  }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

async function withRetry(fn, label) {
  let fails = 0;
  for (;;) {
    try {
      return await fn();
    } catch (err) {
      fails++;
      console.log(`      ${label} 失败(${fails}/4): ${stripCtrl(err.message)}`);
      if (fails >= 4) throw err;
      await sleep(2500);
    }
  }
}

function mapComment(c) {
  return {
    id: c.id,
    author: ((c.author || {}).member || {}).name || null,
    votes: c.vote_count ?? 0,
    created: c.created_time || null,
    content: stripHtml(c.content),
    reply_to: ((c.reply_author || {}).member || {}).name || null,
  };
}

async function fetchRootComments(aid, want, orderBy, cookieHeader, throttle) {
  const out = [];
  let offset = 0;
  let total = null;
  while (out.length < want) {
    const url =
      `${API_HOST}/api/v4/answers/${aid}/comments` +
      `?limit=${API_LIMIT}&offset=${offset}&order_by=${encodeURIComponent(orderBy)}&status=open`;
    const d = await withRetry(() => apiGet(url, cookieHeader), `answer ${aid} 评论`);
    total = d.paging ? d.paging.totals : total;
    const data = d.data || [];
    for (const c of data) out.push(mapComment(c));
    if (!data.length || (d.paging && d.paging.is_end) || (total !== null && out.length >= total)) break;
    offset += API_LIMIT;
    await sleep(throttle * (0.6 + Math.random() * 0.8));
  }
  return { comments: out.slice(0, want), total: total ?? out.length };
}

async function fetchReplies(cid, want, cookieHeader, throttle) {
  const out = [];
  let offset = 0;
  while (out.length < want) {
    const url = `${API_HOST}/api/v4/comments/${cid}/child_comments?limit=${API_LIMIT}&offset=${offset}`;
    const d = await withRetry(() => apiGet(url, cookieHeader), `comment ${cid} 楼中楼`);
    const data = d.data || [];
    for (const c of data) out.push(mapComment(c));
    const total = d.paging ? d.paging.totals : null;
    if (!data.length || (d.paging && d.paging.is_end) || (total !== null && out.length >= total)) break;
    offset += API_LIMIT;
    await sleep(throttle * (0.5 + Math.random() * 0.5));
  }
  return out.slice(0, want);
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) return printHelp();
  const cookieHeader = loadCookieHeader(COOKIE_FILE);

  const censusRaw = readJson(opts.census);
  // 只信 census 内的 qid；不从文件路径里猜数字（避免把目录名年份等误当 qid）
  const qid = sanitizeQid(censusRaw.qid) || 'unknown';
  if (qid === 'unknown') console.log('⚠️ census 未含 qid，输出文件名将用 unknown（不从路径猜测）');
  const censusRows = censusRaw.rows || [];

  // 复现 strat-sample 的 rid 编号（按赞同降序），用于 ledger.rid → census 行 的兜底映射
  const sorted = censusRows.slice().sort(
    (a, b) => (b.votes || 0) - (a.votes || 0) || (a.id > b.id ? 1 : -1),
  );
  const byRid = new Map();
  sorted.forEach((r, i) => byRid.set(i + 1, r));

  // 可选：读 ledger，为每条回答附上 agent 判读与正文节选（使输出成为「评论区复核清单」）
  const ledgerById = new Map();
  const ledgerByRid = new Map();
  if (opts.ledger && existsSync(opts.ledger)) {
    for (const lr of (readJson(opts.ledger).rows || [])) {
      if (lr.id) ledgerById.set(lr.id, lr);
      ledgerByRid.set(lr.rid, lr);
    }
  }
  const annotate = (t) => {
    const lr = ledgerById.get(t.id) || ledgerByRid.get(t.rid);
    if (lr) {
      if (lr.verdict) t.verdict = lr.verdict;
      if (lr.text) t.answer_text = lr.text;
    }
    return t;
  };

  // 目标回答集合
  let targets = [];
  if (opts.onlySampled) {
    for (const lr of (readJson(opts.ledger).rows || [])) {
      const hit = lr.id ? { id: lr.id, votes: lr.votes, author: lr.author } : byRid.get(lr.rid);
      if (hit && hit.id) targets.push(annotate({ id: hit.id, votes: hit.votes || 0, author: hit.author || null, rid: lr.rid }));
    }
  } else {
    targets = sorted.map((r, i) => annotate({ id: r.id, votes: r.votes || 0, author: r.author || null, rid: i + 1, answer_text: r.text || null }));
    if (opts.top > 0) targets = targets.slice(0, opts.top);
  }
  if (opts.maxAnswers > 0) targets = targets.slice(0, opts.maxAnswers);
  // answer id 必须是纯数字，防止 census/ledger 里的异常 id 改变请求路径
  const skippedIds = targets.filter((t) => !/^\d+$/.test(String(t.id))).length;
  targets = targets.filter((t) => /^\d+$/.test(String(t.id)));
  if (skippedIds) console.log(`⚠️ 跳过 ${skippedIds} 条 id 非纯数字的回答（异常输入）`);
  if (!targets.length) throw new Error('没有可抓取的回答（检查 --census/--ledger 或 --top）');

  const outDir = resolve(opts.outDir);
  mkdirSync(outDir, { recursive: true });

  console.log(`目标回答: ${targets.length} 条 | 每题热评: ${opts.perAnswer} | 楼中楼: ${opts.replies} | 排序: ${opts.orderBy}`);

  const answers = [];
  let done = 0;
  for (const t of targets) {
    let got = { comments: [], total: 0 };
    try {
      got = await fetchRootComments(t.id, opts.perAnswer, opts.orderBy, cookieHeader, opts.throttle);
    } catch (err) {
      console.log(`  跳过回答 ${t.id}: ${stripCtrl(err.message)}`);
    }
    if (opts.replies > 0) {
      for (const c of got.comments) {
        if (!/^\d+$/.test(String(c.id))) {
          c.replies = [];
          continue;
        }
        try {
          c.replies = await fetchReplies(c.id, opts.replies, cookieHeader, opts.throttle);
        } catch {
          c.replies = [];
        }
      }
    }
    answers.push({
      answer_id: t.id,
      rid: t.rid,
      answer_author: t.author,
      answer_votes: t.votes,
      verdict: t.verdict || null,
      answer_text: t.answer_text || null,
      total_comments: got.total,
      comments: got.comments,
    });
    done++;
    if (done % 10 === 0 || done === targets.length) {
      const nc = answers.reduce((s, a) => s + a.comments.length, 0);
      console.log(`  进度 ${done}/${targets.length}，累计热评 ${nc} 条`);
    }
    await sleep(opts.throttle * (0.6 + Math.random() * 0.8));
  }

  const jsonOut = {
    qid,
    fetched_at: new Date().toISOString(),
    order_by: opts.orderBy,
    per_answer: opts.perAnswer,
    replies: opts.replies,
    only_sampled: opts.onlySampled,
    answers_fetched: answers.length,
    comments_fetched: answers.reduce((s, a) => s + a.comments.length, 0),
    answers,
  };
  const jsonPath = resolve(outDir, `${qid}_comments.json`);
  writeFileSync(jsonPath, JSON.stringify(jsonOut, null, 1), 'utf-8');

  // 可读版

  const L = [];
  L.push(`评论层抓取（回答下面热评）`);
  L.push(`问题 qid: ${qid}`);
  L.push(`回答 ${answers.length} 条 | 每题热评 ${opts.perAnswer} | 楼中楼 ${opts.replies} | 排序 ${opts.orderBy}`);
  L.push(`抓取时间: ${jsonOut.fetched_at}`);
  L.push('='.repeat(80));
  for (const a of answers) {
    L.push('');
    L.push(`【回答 aid=${a.answer_id} rid=${a.rid}】 ${a.answer_author || '匿名'} | 赞=${a.answer_votes} | 评论总数=${a.total_comments}`);
    const at = (a.answer_text || '').replace(/\s*\n+\s*/g, ' ').trim();
    L.push(`  判读: ${a.verdict || '(未判读)'}${at ? '   回答节选: ' + (at.length > 100 ? at.slice(0, 100) + '…' : at) : ''}`);
    if (!a.comments.length) {
      L.push('  (无评论或无权限)');
      continue;
    }
    for (const c of a.comments) {
      let txt = (c.content || '').replace(/\s*\n+\s*/g, ' ').trim();
      if (txt.length > 500) txt = txt.slice(0, 500) + ' …[截断]';
      L.push(`  ▸ ${c.author || '匿名'} | 赞 ${c.votes} | ${txt}`);
      for (const r of c.replies || []) {
        let rt = (r.content || '').replace(/\s*\n+\s*/g, ' ').trim();
        if (rt.length > 300) rt = rt.slice(0, 300) + ' …[截断]';
        L.push(`      ↳ ${r.author || '匿名'}${r.reply_to ? ' 回复 ' + r.reply_to : ''} | 赞 ${r.votes} | ${rt}`);
      }
    }
  }
  const txtPath = resolve(outDir, `${qid}_comments.txt`);
  writeFileSync(txtPath, L.join('\n'), 'utf-8');

  console.log('完成');
  console.log(`  ${jsonPath}`);
  console.log(`  ${txtPath}`);
  console.log(`  回答 ${jsonOut.answers_fetched} 条，热评 ${jsonOut.comments_fetched} 条`);
  console.log('');
  console.log('下一步：精读 _comments.txt，单独做「评论层分析」——争议点排行、评论区 vs 回答区立场对照、信息增量；');
  console.log('不要把评论并进回答区的立场统计（评论是回答的附属、非独立样本，偏向争议）。');
}

main().catch((err) => {
  console.error('\n❌ ' + stripCtrl(err && err.message || err));
  process.exit(1);
});
