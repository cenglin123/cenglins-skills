#!/usr/bin/env node
/**
 * 环境自愈脚本（幂等，可反复运行）
 *
 * 场景：cc-switch / 其他管理器从 GitHub 重装本 skill 时，会整体替换 skill 目录，
 * 导致被 .gitignore 排除的 node_modules 与 Cookie 丢失。本脚本把这两样放到
 * skill 目录之外（~/.zhihu-auto），并在 skill 目录被重建后一键恢复：
 *
 *   1. Cookie 迁移：若外部家目录无 Cookie 但 skill 内有旧 Cookie，则迁移过去
 *   2. 依赖安装：把 package.json/lock 复制到外部 deps 目录并 npm install（仅缺失时）
 *   3. 建立链接：scripts/node_modules → 外部 deps/node_modules（Windows junction / POSIX symlink）
 *
 * 用法：
 *   node setup.mjs              # 常规：缺什么补什么
 *   node setup.mjs --force      # 强制用外部 junction 替换 scripts/node_modules 实体目录
 *   node setup.mjs --reinstall  # 强制重装外部依赖
 *   node setup.mjs --no-install # 只做 Cookie 迁移与建链，不跑 npm（离线/调试）
 *   node setup.mjs --check      # 只检查状态并退出（非 0 表示有缺失）
 *   node setup.mjs --allow-relaxed-install   # 锁安装两次都失败时，显式退到 npm install 放宽版本
 *                                  #（不写 .pkg-hash；下次 --check 报 Hash: missing 并重装）
 */

import {
  existsSync, mkdirSync, copyFileSync, lstatSync, rmSync, symlinkSync, readdirSync, readFileSync, writeFileSync, chmodSync,
} from 'fs';
import { join } from 'path';
import { spawnSync } from 'child_process';
import { createHash } from 'crypto';
import {
  SCRIPTS_DIR, EXTERNAL_HOME, EXTERNAL_COOKIE, LEGACY_COOKIE, LEGACY_EXTERNAL_COOKIE,
  EXTERNAL_DEPS, DEPS_NODE_MODULES, LINK_PATH, ensureExternalHome, stripCtrl, parseNetscapeCookieText,
} from './lib/env.mjs';

const argv = process.argv.slice(2);
const FORCE = argv.includes('--force');
const REINSTALL = argv.includes('--reinstall');
const NO_INSTALL = argv.includes('--no-install');
const CHECK = argv.includes('--check');

const log = (s) => console.log(s);
let problems = [];

function isLink(p) {
  try {
    return lstatSync(p).isSymbolicLink();
  } catch {
    return false;
  }
}

/**
 * 外部 Cookie 是否是**可用的登录态**：必须含非空 z_c0。
 * 复用 lib/env.mjs 的 parseNetscapeCookieText（已处理 #HttpOnly_ 前缀、列数校验、\r?\n），
 * 不自己手写正则 —— 手写会漏 #HttpOnly_ 前缀行，而 z_c0 常常正好带该前缀。
 */
function externalCookieUsable() {
  if (!existsSync(EXTERNAL_COOKIE)) return false;
  try {
    return parseNetscapeCookieText(readFileSync(EXTERNAL_COOKIE, 'utf-8'))
      .some((c) => c.name === 'z_c0' && String(c.value || '').trim().length > 0);
  } catch {
    return false;
  }
}

function cookieStatus() {
  if (existsSync(EXTERNAL_COOKIE)) return externalCookieUsable() ? 'external' : 'external-invalid';
  if (existsSync(LEGACY_EXTERNAL_COOKIE)) return 'legacy-external';
  if (existsSync(LEGACY_COOKIE)) return 'legacy';
  return 'missing';
}

function depsStatus() {
  if (!existsSync(DEPS_NODE_MODULES)) return 'missing';
  try {
    return readdirSync(DEPS_NODE_MODULES).length ? 'present' : 'missing';
  } catch {
    return 'missing';
  }
}

function linkStatus() {
  if (!existsSync(LINK_PATH)) return 'absent';
  return isLink(LINK_PATH) ? 'link' : 'realdir';
}

function fileHash(p) {
  try {
    return createHash('sha256').update(readFileSync(p)).digest('hex');
  } catch {
    return null;
  }
}
function wantPkgHash() {
  return [fileHash(join(SCRIPTS_DIR, 'package.json')), fileHash(join(SCRIPTS_DIR, 'package-lock.json'))].join(':');
}

if (CHECK) {
  const c = cookieStatus(), d = depsStatus(), l = linkStatus();
  log(`Cookie : ${c} (${c.startsWith('external') ? EXTERNAL_COOKIE : c === 'legacy' ? LEGACY_COOKIE : '无'})`);
  log(`Deps   : ${d}`);
  log(`Link   : ${l}${l === 'realdir' ? '（实体目录，可运行；欲跨重装存活请 --force）' : ''}`);
  const hf = join(EXTERNAL_DEPS, '.pkg-hash');
  const hh = existsSync(hf) ? readFileSync(hf, 'utf-8').trim() : '';
  const hashOk = !!hh && hh === wantPkgHash();
  log(`Hash   : ${hashOk ? 'match' : hh ? 'stale/mismatch' : 'missing'}`);
  const injectCode = join(EXTERNAL_HOME, 'browser-login-code.js');
  if (existsSync(injectCode)) log(`⚠️ 存在含凭证的注入代码（建议删除）: ${injectCode}`);
  const profileDir = join(EXTERNAL_HOME, 'chrome-profile');
  if (existsSync(profileDir)) log(`ℹ️ 存在持久化浏览器 profile（含登录态副本）: ${profileDir}`);
  // 'external-invalid' 也必须判为不就绪：否则「外部 Cookie 无有效 z_c0」会在这里被报成 OK，
  // 与下面主流程的判定自相矛盾（同一个谓词、两个结论）。
  const ok = c !== 'missing' && c !== 'external-invalid' && (l === 'link' || l === 'realdir') && d === 'present' && hashOk;
  log(ok ? 'OK：环境就绪' : 'NEEDS SETUP：请运行 node setup.mjs');
  process.exit(ok ? 0 : 1);
}

log('=== zhihu-auto 环境自愈 ===');
log(`外部家目录: ${EXTERNAL_HOME}`);
log('');

// ── 1. 外部家目录 ──
ensureExternalHome();

// ── 2. Cookie 迁移 ──
//
// 判据是「**可用的登录态**」（含非空 z_c0），不是「文件存在」。空文件 / 半截文件若被当主副本，
// 再删掉 legacy 就是不可恢复地丢掉唯一备用凭据（与 get-cookie.mjs:198-202 的 fail-closed 对齐）。
// 外部副本无效时**先尝试用 legacy 修复**，修不好才保留 legacy —— 且修复成功必须清掉 cookie-invalid，
// 否则「修好了却仍然 exit 1」是纯误报。
const dropLegacy = () => {
  for (const legacy of [LEGACY_EXTERNAL_COOKIE, LEGACY_COOKIE]) {
    if (existsSync(legacy) && legacy !== EXTERNAL_COOKIE) {
      try { rmSync(legacy, { force: true }); log(`[cookie] 已删除旧副本: ${legacy}`); }
      catch { problems.push('cookie-legacy'); log(`[cookie] ⚠️ 未能删除旧副本，请手动清理: ${legacy}`); }
    }
  }
};

if (existsSync(EXTERNAL_COOKIE) && !externalCookieUsable()) {
  log('[cookie] ⚠️ 外部 Cookie 存在但无有效 z_c0 —— 判为不可用，保留 legacy 旧副本不删');
  problems.push('cookie-invalid');
  const repairSrc = existsSync(LEGACY_EXTERNAL_COOKIE) ? LEGACY_EXTERNAL_COOKIE
    : existsSync(LEGACY_COOKIE) ? LEGACY_COOKIE : null;
  if (!repairSrc) {
    log('[cookie] ⚠️ 无 legacy 副本可用于修复——请重跑 `node get-cookie.mjs` 重新登录');
  } else {
    try {
      copyFileSync(repairSrc, EXTERNAL_COOKIE);
      try { chmodSync(EXTERNAL_COOKIE, 0o600); } catch { /* Windows / 无权限：忽略 */ }
      log(`[cookie] 已尝试用 legacy 副本修复: ${EXTERNAL_COOKIE}  ←  ${repairSrc}`);
    } catch (err) {
      // 外部文件被占用 / 只读时 copyFileSync 会抛；不接住就会在依赖安装之前整个 abort 掉 setup。
      log(`[cookie] ⚠️ 修复失败（${stripCtrl(err.message)}）—— 外部 Cookie 保持原样，legacy 未删`);
    }
    if (externalCookieUsable()) {
      problems = problems.filter((p) => p !== 'cookie-invalid');
      log('[cookie] 修复成功：外部 Cookie 重新含有效 z_c0');
    } else {
      log('[cookie] ⚠️ 修复后仍无有效 z_c0 —— 请重跑 `node get-cookie.mjs` 重新登录');
    }
  }
}

if (externalCookieUsable()) {
  log(`[cookie] 外部已存在且有效: ${EXTERNAL_COOKIE}`);
  // 外部已是**可用**主副本，才清理 legacy 旧副本，收口凭据
  dropLegacy();
} else if (!existsSync(EXTERNAL_COOKIE) && (existsSync(LEGACY_EXTERNAL_COOKIE) || existsSync(LEGACY_COOKIE))) {
  const src = existsSync(LEGACY_EXTERNAL_COOKIE) ? LEGACY_EXTERNAL_COOKIE : LEGACY_COOKIE;
  try {
    copyFileSync(src, EXTERNAL_COOKIE);
    try { chmodSync(EXTERNAL_COOKIE, 0o600); } catch { /* Windows / 无权限：忽略 */ }
    log(`[cookie] 已迁移 Cookie 到外部: ${EXTERNAL_COOKIE}  ←  ${src}`);
  } catch (err) {
    log(`[cookie] ⚠️ 迁移失败（${stripCtrl(err.message)}）—— legacy 旧副本已保留，未删除`);
    problems.push('cookie-migrate');
  }
  // 迁移成功后删除两个 legacy 旧副本，收口凭据（避免任一长期残留可用登录态）
  if (externalCookieUsable()) dropLegacy();
  else log('[cookie] ⚠️ 迁移过来的副本无有效 z_c0 —— 请重跑 `node get-cookie.mjs` 重新登录');
} else if (!existsSync(EXTERNAL_COOKIE)) {
  log('[cookie] ⚠️ 未找到 Cookie——请运行 `node get-cookie.mjs` 重新登录获取');
  problems.push('cookie');
}

// ── 3. 依赖安装到外部目录 ──
mkdirSync(EXTERNAL_DEPS, { recursive: true });
for (const f of ['package.json', 'package-lock.json']) {
  const src = join(SCRIPTS_DIR, f);
  if (existsSync(src)) copyFileSync(src, join(EXTERNAL_DEPS, f));
}

let ds = depsStatus();
const pkgHashFile = join(EXTERNAL_DEPS, '.pkg-hash');
const wantHash = wantPkgHash();
const haveHash = existsSync(pkgHashFile) ? readFileSync(pkgHashFile, 'utf-8').trim() : '';
const hashMatch = !!haveHash && haveHash === wantHash;
const pkgChanged = ds === 'present' && !!haveHash && haveHash !== wantHash;
let installed = false;   // 模块作用域：供下方 hash 写入判定使用（仅本轮真正装成功才置 true）
if (NO_INSTALL) {
  log(`[deps] 跳过安装（--no-install）；当前状态: ${ds}`);
} else if (ds === 'present' && hashMatch && !REINSTALL) {
  log('[deps] 外部依赖已安装且 package 声明 hash 未变，跳过');
} else {
  if (pkgChanged) log('[deps] 检测到 package.json/lock 变化，重装依赖...');
  let invalidated = true;
  try { rmSync(pkgHashFile, { force: true }); } catch { invalidated = false; problems.push('hash'); }
  if (!invalidated) {
    log('[deps] ⚠️ 无法失效旧 hash 标记（文件被占用？）——跳过本轮安装以免粘住坏状态，请重试');
  } else {
    const useCi = existsSync(join(EXTERNAL_DEPS, 'package-lock.json'));
    // 锁优先是**有意的安全姿态**：npm install 会重写 EXTERNAL_DEPS/package-lock.json，
    // 而 .pkg-hash 只哈希 skill 侧清单，install 兜底会让 --check 的 Hash:match 从此说谎。
    // 因此失败只重试 npm ci；退到 install 必须显式 --allow-relaxed-install，且**不写** .pkg-hash。
    const allowRelaxed = argv.includes('--allow-relaxed-install');
    const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
    // Windows 上 .cmd 必须经 shell 启动（默认 shell:false 会 EINVAL 直接失败）；argv 固定无注入面
    const runNpm = (sub) => spawnSync(npmCmd, [sub], {
      cwd: EXTERNAL_DEPS,
      stdio: 'inherit',
      shell: process.platform === 'win32',
      timeout: 30 * 60 * 1000,   // 兜底：npm 挂死时不至于把 setup 永久卡住
    });
    const ok = (r) => !(r.error || r.status !== 0);

    if (useCi) {
      log(`[deps] 安装到 ${EXTERNAL_DEPS}（npm ci）...`);
      let r = runNpm('ci');
      if (!ok(r) && !r.error) {
        // 只在「进程正常启动但非零退出」时重试（网络/代理抖动）；启动失败重试无意义。
        // 同步等待 3 秒：Atomics.wait 阻塞当前线程直到超时 —— 不再 spawn 一个 node 子进程
        // （内循环 R1 指出旧写法可读性差且易被误读为「起了个子进程做别的事」）。
        log('[deps] npm ci 失败，3 秒后重试一次...');
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 3000);
        r = runNpm('ci');
      }
      if (ok(r)) { log('[deps] 安装完成'); installed = true; }
      else if (allowRelaxed) {
        // 刻意**不**置 installed → 第 176 行的写 hash 条件不成立 → .pkg-hash 不推进；
        // 且第 155 行已 rmSync 掉旧 .pkg-hash，于是下次 --check 报 `Hash: missing`，
        // 并且 L150 的「已安装且 hash 未变 → 跳过」不成立 → 每次 setup 都会重新尝试按锁安装。
        log('[deps] ⚠️ --allow-relaxed-install：退到 npm install（放宽版本；.pkg-hash 不推进，下次 --check 报 Hash: missing 并重装）');
        if (ok(runNpm('install'))) log('[deps] relaxed 安装完成（.pkg-hash 未推进）');
        else { log('[deps] ⚠️ npm install 也失败'); problems.push('npm'); }
      } else {
        log('[deps] ⚠️ npm ci 失败——请检查网络/代理后重试；如确需放宽版本，显式加 --allow-relaxed-install');
        problems.push('npm');
      }
    } else {
      log(`[deps] 无 lock 文件，安装到 ${EXTERNAL_DEPS}（npm install）...`);
      if (ok(runNpm('install'))) { log('[deps] 安装完成'); installed = true; }
      else { log('[deps] ⚠️ npm install 失败'); problems.push('npm'); }
    }
    ds = depsStatus();
  }
}
// 仅在本轮**确实成功安装**后才推进 hash 标记（skip / --no-install / 失效失败 → 不写），防粘住坏状态
if (installed && ds === 'present') {
  try { writeFileSync(pkgHashFile, wantHash, 'utf-8'); } catch { log('[deps] ⚠️ 无法写入 hash 标记'); problems.push('hash'); }
}

// ── 4. 建链 scripts/node_modules → 外部 deps ──
const ls = linkStatus();
if (ls === 'link') {
  log('[link] 链接已存在，跳过');
} else if (ls === 'realdir') {
  if (FORCE) {
    log('[link] 用外部 junction 替换实体 node_modules（--force）...');
    rmSync(LINK_PATH, { recursive: true, force: true });
    createLink();
  } else {
    log('[link] ⚠️ scripts/node_modules 是实体目录（非链接）。若希望其跨重装存活，');
    log('       请运行 `node setup.mjs --force` 改为指向外部 deps 的 junction。');
  }
} else {
  createLink();
}

function createLink() {
  if (!existsSync(DEPS_NODE_MODULES)) {
    log('[link] 跳过：外部依赖尚未安装');
    problems.push('link');
    return;
  }
  const type = process.platform === 'win32' ? 'junction' : 'dir';
  try {
    symlinkSync(DEPS_NODE_MODULES, LINK_PATH, type);
    log(`[link] 已创建 ${type}: ${LINK_PATH} -> ${DEPS_NODE_MODULES}`);
  } catch (err) {
    log(`[link] ⚠️ 创建链接失败: ${stripCtrl(err.message)}`);
    log('       Windows 无管理员权限时 junction 一般仍可用；若仍失败可手动执行:');
    log(`       cmd /c mklink /J "${LINK_PATH}" "${DEPS_NODE_MODULES}"`);
    problems.push('link');
  }
}

// ── 汇总 ──
log('');
log('=== 结果 ===');
log(`Cookie : ${cookieStatus()}`);
log(`Deps   : ${depsStatus()}`);
log(`Link   : ${linkStatus()}`);
if (problems.length) {
  log(`⚠️ 仍有待处理: ${[...new Set(problems)].join(', ')}`);
  process.exit(1);
} else {
  log('✅ 环境就绪。之后每次 cc-switch 重装后重跑本脚本即可恢复。');
}
