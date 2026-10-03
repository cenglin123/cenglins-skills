#!/usr/bin/env node
/**
 * 立场判读的加权估计 + 置信区间 + 全量关键词交叉验证（争议题采样分析 · 第二步）
 *
 * 读取 strat-sample.mjs 产出的 ledger.json（含抽样设计）+ census.json（含总体规模），
 * 以及 agent 填好的 verdicts 文件，计算：
 *   1. 按分层设计加权的各阵营占比（点估计）
 *   2. 分层随机抽样的设计方差与 95% 置信区间（含有限总体校正 FPC）
 *   3. 未加权占比 + Wilson 区间（作为稀有类别的稳健参考）
 *   4. 对全量 census 做关键词扫描，独立交叉验证（不依赖 agent 判读）
 *
 * 设计哲学：脚本只做确定性的记账与统计；「评判每条回答属于哪个阵营」这个判断
 * 留在 agent 手里——脚本给出 auto_hint 辅助分诊，agent 给 verdict，脚本再算账。
 *
 * verdicts 文件格式（分组文本，agent 友好）：
 *   # A
 *   12 13 14
 *   # B=挺子午
 *   119
 *   # N
 *   22 33 50
 *   # O
 *   17 18 26
 *   （也接受 JSON 映射：{"12":"A","119":"B"}）
 *   # 开头为分组标题；其下为 rid 列表（空格/逗号/换行分隔）。
 *
 * 用法：
 *   node stance-estimate.mjs --ledger <qid>_ledger.json --census <qid>_census.json \
 *        --verdicts <verdicts.txt> [--output report.md]
 *   node stance-estimate.mjs --ledger ... --census ... --use-autohint --facets <facets.json>
 *   node stance-estimate.mjs --help
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve } from 'path';

const DEFAULT_CATEGORIES = {
  A: '第一阵营',
  B: '第二阵营',
  N: '中立/调和',
  O: '无关/难判',
};

function printHelp() {
  console.log(`用法：
  node stance-estimate.mjs --ledger <ledger.json> --census <census.json> [--verdicts <file>] [选项]

必填：
  --ledger <file>     strat-sample.mjs 产出的 ledger.json
  --census <file>     strat-sample.mjs 产出的 census.json（用于总体规模与关键词交叉验证）

判读来源（二选一）：
  --verdicts <file>   agent 填写的判读文件（分组文本或 JSON 映射）
  --use-autohint      无 verdicts 时，用 ledger 里的 auto_hint 粗分类（低置信，仅作预览）

选项：
  --categories "A=反X,B=挺X,N=中立,O=无关"   类别标签（默认 A/B/N/O 占位）
  --facets <file>     阵营关键词 JSON（用于关键词交叉验证；--use-autohint 时必需）
  --alt <file>        校正判读文件（如同格式）。给出后额外输出「主判读 vs 校正判读」敏感性对比
  --output <file>     把 markdown 报告写入文件

verdicts 分组文本示例：
  # A
  12 13 14 20
  # B
  119
  # N
  22 33
  # O
  17 18`);
}

function parseArgs(argv) {
  const values = new Map();
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--help' || token === '-h') return { help: true };
    if (!token.startsWith('--')) throw new Error(`未知参数: ${token}`);
    const eq = token.indexOf('=');
    const key = eq >= 0 ? token.slice(2, eq) : token.slice(2);
    const takeValue = !['use-autohint', 'help'].includes(key);
    const val = !takeValue ? true : eq >= 0 ? token.slice(eq + 1) : argv[++i];
    values.set(key, val);
  }
  if (!values.get('ledger')) throw new Error('缺少 --ledger');
  if (!values.get('census')) throw new Error('缺少 --census');
  if (!values.get('verdicts') && !values.get('use-autohint')) {
    throw new Error('需要 --verdicts 或 --use-autohint 之一');
  }
  const categories = {};
  const catArg = values.get('categories');
  const base = { ...DEFAULT_CATEGORIES };
  if (catArg) {
    for (const part of String(catArg).split(',')) {
      const [c, ...rest] = part.split('=');
      if (c && rest.length) base[c.trim()] = rest.join('=').trim();
    }
  }
  Object.assign(categories, base);
  return {
    help: false,
    ledger: values.get('ledger'),
    census: values.get('census'),
    verdicts: values.get('verdicts') || '',
    useAutoHint: !!values.get('use-autohint'),
    facets: values.get('facets') || '',
    alt: values.get('alt') || '',
    output: values.get('output') || '',
    categories,
  };
}

// 解析 verdicts：支持分组文本与 JSON 映射
function parseVerdicts(text) {
  const out = {};
  const t = text.trim();
  if (t.startsWith('{')) {
    const obj = JSON.parse(t);
    for (const [k, v] of Object.entries(obj)) out[String(k)] = String(v);
    return out;
  }
  let current = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#')) {
      current = line.slice(1).split('=')[0].trim().split(/\s+/)[0];
      continue;
    }
    if (!current) continue;
    for (const tok of line.split(/[\s,;]+/)) {
      const rid = tok.replace(/[^\d]/g, '');
      if (rid) out[rid] = current;
    }
  }
  return out;
}

function wilson(k, n, z = 1.96) {
  if (n === 0) return [0, 0];
  const p = k / n;
  const den = 1 + (z * z) / n;
  const center = (p + (z * z) / (2 * n)) / den;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / den;
  return [Math.max(0, center - half), Math.min(1, center + half)];
}

const pct = (x) => (100 * x).toFixed(2) + '%';

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) return printHelp();

  const ledger = JSON.parse(readFileSync(resolve(opts.ledger), 'utf-8'));
  const censusRaw = JSON.parse(readFileSync(resolve(opts.census), 'utf-8'));
  const censusRows = Array.isArray(censusRaw) ? censusRaw : censusRaw.rows;
  const censusTotals = Array.isArray(censusRaw) ? null : (censusRaw.totals ?? null);
  const censusTruncated = !Array.isArray(censusRaw) && !!censusRaw.truncated;

  const rows = ledger.rows || [];
  const bandLabels = ledger.bands || [...new Set(rows.map((r) => r.stratum))];

  // 每个层在总体中的规模（优先用 census 精确计算，回退 stats 无则用抽样层内计数）
  const popSize = new Map();
  for (const b of bandLabels) popSize.set(b, 0);
  for (const r of censusRows) {
    // 用与 strat-sample 完全一致的层界来判断
    const b = bandFor(r.votes || 0, ledger);
    if (popSize.has(b)) popSize.set(b, popSize.get(b) + 1);
  }
  const N = censusRows.length;
  const sampledByBand = new Map();
  for (const b of bandLabels) sampledByBand.set(b, 0);
  for (const r of rows) sampledByBand.set(r.stratum, (sampledByBand.get(r.stratum) || 0) + 1);

  // 判读来源
  let verdicts = {};
  let source = '';
  if (opts.verdicts) {
    verdicts = parseVerdicts(readFileSync(resolve(opts.verdicts), 'utf-8'));
    source = 'agent 判读 (--verdicts)';
  } else {
    let fromLedger = 0;
    let fromHint = 0;
    for (const r of rows) {
      if (r.verdict) {
        verdicts[String(r.rid)] = r.verdict;
        fromLedger++;
      } else if (opts.useAutoHint && Array.isArray(r.auto_hint) && r.auto_hint.length) {
        verdicts[String(r.rid)] = r.auto_hint[0];
        fromHint++;
      }
    }
    source = fromLedger && fromHint
      ? `ledger.verdict(${fromLedger}) + auto_hint(${fromHint})（混合）`
      : fromLedger
        ? `ledger.verdict(${fromLedger})`
        : `关键词 auto_hint(${fromHint})（低置信，仅预览）`;
  }

  const cats = opts.categories;
  const unknownCats = [...new Set(Object.values(verdicts))].filter((v) => !(v in cats));
  if (unknownCats.length) {
    throw new Error(
      `判读里出现未在 --categories 中声明的类别: ${unknownCats.join(', ')}。` +
        `请检查拼写，或用 --categories 补充（否则这些条目会被静默丢弃）。`,
    );
  }

  // 给定一份判读映射，算出加权计数、未判读、设计方差（供主判读与 --alt 复用）
  const tally = (vmap) => {
    const count = {}; // cat -> {unweighted, weighted}
    for (const c of Object.keys(cats)) count[c] = { unweighted: 0, weighted: 0 };
    const unjudged = [];
    for (const r of rows) {
      const v = vmap[String(r.rid)];
      if (!v || !(v in count)) {
        unjudged.push(r.rid);
        continue;
      }
      const nH = sampledByBand.get(r.stratum) || 1;
      const NH = popSize.get(r.stratum) || nH;
      const w = NH / nH;
      count[v].unweighted += 1;
      count[v].weighted += w;
    }
    const varOf = {};
    for (const c of Object.keys(cats)) varOf[c] = 0;
    for (const b of bandLabels) {
      const nH = sampledByBand.get(b) || 0;
      const NH = popSize.get(b) || 0;
      if (nH === 0) continue;
      const Wh = NH / N;
      for (const c of Object.keys(cats)) {
        const nHc = rows.filter((r) => r.stratum === b && vmap[String(r.rid)] === c).length;
        const pHc = nHc / nH;
        const fpc = NH > 1 ? 1 - nH / NH : 0;
        varOf[c] += Wh * Wh * ((fpc * (pHc * (1 - pHc))) / Math.max(1, nH - 1));
      }
    }
    return { count, unjudged, varOf };
  };

  const { count, unjudged, varOf } = tally(verdicts);

  // ── 报告 ──
  const L = [];
  L.push(`# 立场加权估计报告`);
  L.push('');
  L.push(`- 问题 qid: ${ledger.qid}`);
  L.push(`- 总体: ${N} 条回答；抽样: ${rows.length} 条（每层 ${ledger.per_band}，seed=${ledger.seed}）`);
  L.push(`- 判读来源: ${source}`);
  L.push(`- 未判读: ${unjudged.length} 条`);
  if (censusTruncated || (censusTotals && censusTotals > N)) {
    L.push(`- ⚠️ **census 被截断**：仅 ${N} 条，页面报告 ${censusTotals ?? '?'} 条 → 总体 N 被低估，所有占比会被系统性高估`);
  }
  L.push('');
  L.push('## 分层抽样设计');
  L.push('');
  L.push('| 层 | 总体 N_h | 抽样 n_h | 权重 N_h/n_h |');
  L.push('|---|---:|---:|---:|');
  for (const b of bandLabels) {
    const nH = sampledByBand.get(b) || 0;
    const NH = popSize.get(b) || 0;
    L.push(`| ${b} | ${NH} | ${nH} | ${nH ? (NH / nH).toFixed(2) : '-'} |`);
  }
  L.push('');
  L.push('## 阵营占比（设计加权）');
  L.push('');
  L.push('| 阵营 | 加权条数 | 加权占比 | 设计 95% CI | 未加权 n | 未加权% | Wilson 95% CI |');
  L.push('|---|---:|---:|---|---:|---:|---|');
  let sumP = 0;
  for (const [c, label] of Object.entries(cats)) {
    const wc = count[c].weighted;
    const p = wc / N;
    sumP += p;
    const se = Math.sqrt(varOf[c]);
    let ci;
    if (se > 1e-9) {
      ci = `${pct(Math.max(0, p - 1.96 * se))} ~ ${pct(Math.min(1, p + 1.96 * se))}`;
    } else {
      ci = '设计方差≈0（见下方 Wilson 参考）';
    }
    const [wl, wh] = wilson(count[c].unweighted, rows.length);
    L.push(
      `| ${label} (${c}) | ${wc.toFixed(1)} | ${pct(p)} | ${ci} | ${count[c].unweighted} | ${pct(
        count[c].unweighted / rows.length,
      )} | ${pct(wl)} ~ ${pct(wh)} |`,
    );
  }
  L.push('');
  L.push(`（加权占比之和 ${pct(sumP)}，差额为未判读部分。）`);
  L.push('');

  // 关键词交叉验证
  if (opts.facets) {
    const facets = JSON.parse(readFileSync(resolve(opts.facets), 'utf-8'));
    L.push('## 全量关键词交叉验证（独立于 agent 判读）');
    L.push('');
    L.push(`对 census 全量 ${N} 条做关键词扫描：`);
    L.push('');
    L.push('| 阵营 | 命中条数 | 命中占比 |');
    L.push('|---|---:|---:|');
    for (const [c, def] of Object.entries(facets)) {
      const markers = def.markers || [];
      const k = censusRows.filter((r) => markers.some((m) => (r.text || '').includes(m))).length;
      L.push(`| ${def.label || c} (${c}) | ${k} | ${pct(k / N)} |`);
    }
    const allMarkers = Object.values(facets).flatMap((d) => d.markers || []);
    const none = censusRows.filter((r) => !allMarkers.some((m) => (r.text || '').includes(m))).length;
    L.push(`| （未命中任何关键词） | ${none} | ${pct(none / N)} |`);
    L.push('');
    L.push('> 命中可重叠：同一回答可能同时命中多个阵营的关键词，故各行占比**不可相加**（与上方互斥的加权占比不可直接比较）。');
    L.push('');
  }

  // 敏感性：主判读 vs 校正判读（--alt）
  if (opts.alt) {
    const altVerdicts = parseVerdicts(readFileSync(resolve(opts.alt), 'utf-8'));
    const altUnknown = [...new Set(Object.values(altVerdicts))].filter((v) => !(v in cats));
    if (altUnknown.length) {
      throw new Error(`校正判读里出现未在 --categories 中声明的类别: ${altUnknown.join(', ')}（检查拼写；否则这些条目会静默消失、伪装成「结论不稳」）。`);
    }
    const altRes = tally(altVerdicts);
    L.push('## 敏感性：主判读 vs 校正判读');
    L.push('');
    L.push(`校正判读来源: ${opts.alt}（如：把反串/需复核项改判、或把边缘项移入中立后）`);
    L.push(`- 未判读：主判读 ${unjudged.length} 条，校正判读 ${altRes.unjudged.length} 条（两者差得太大通常是类别拼写/落项问题，而非真实变化）`);
    L.push('');
    L.push('| 阵营 | 主判读 | 校正判读 | 变化 |');
    L.push('|---|---:|---:|---:|');
    for (const [c, label] of Object.entries(cats)) {
      const p1 = count[c].weighted / N;
      const p2 = altRes.count[c].weighted / N;
      const d = p2 - p1;
      const sign = d >= 0 ? '+' : '';
      L.push(`| ${label} (${c}) | ${pct(p1)} | ${pct(p2)} | ${sign}${(100 * d).toFixed(2)}pp |`);
    }
    L.push('');
    L.push('若两者对结论无实质影响 → 结论稳健；若某阵营占比在被校正后垮塌（或从 0 变为非 0）→ 该结论依赖反串/模糊项，不可靠。');
    L.push('');
  }

  L.push('## 方法与局限');
  L.push('');
  L.push('- 层内为简单随机抽样，点估计按 N_h/n_h 加权；设计 CI 用分层比例方差 + 有限总体校正。');
  L.push('- 逐类别 CI 是**边际**区间、未计类别间协方差，**不支持「A 是否多于 B」式的差值检验**。');
  L.push('- 稀有类别（仅个别命中）的设计方差会偏小甚至为 0；表内「Wilson」列是**未加权**区间（非设计一致，仅供量级判断）。');
  L.push('- 若 census 被截断（见 census.truncated / stats.truncated），总体 N 被低估，所有占比会被高估——应先重抓全量。');
  L.push('- 本报告只描述「回答区」的立场分布，不等于全体访问者/事件参与者的分布。');
  L.push('- 关键词交叉验证只反映「是否出现某阵营的标志性话术」，会漏掉用词不同的同阵营回答，且命中可重叠。');
  L.push('');

  const report = L.join('\n');
  if (opts.output) {
    writeFileSync(resolve(opts.output), report, 'utf-8');
    console.log('报告已写入: ' + resolve(opts.output));
  } else {
    console.log(report);
  }
}

// 依据 ledger 记录的层界判断某赞同数属于哪一层
function bandFor(votes, ledger) {
  const labels = ledger.bands || [];
  for (const label of labels) {
    const m = label.match(/\[(\d+),(\d+|inf)\)/);
    if (!m) continue;
    const lo = Number(m[1]);
    const hi = m[2] === 'inf' ? Infinity : Number(m[2]);
    if (votes >= lo && votes < hi) return label;
  }
  return labels[labels.length - 1];
}

try {
  main();
} catch (err) {
  console.error('\n❌ ' + err.message);
  process.exit(1);
}
