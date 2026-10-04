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
 */

import {
  existsSync, mkdirSync, copyFileSync, lstatSync, rmSync, symlinkSync, readdirSync, readFileSync, writeFileSync, chmodSync,
} from 'fs';
import { join } from 'path';
import { spawnSync } from 'child_process';
import { createHash } from 'crypto';
import {
  SCRIPTS_DIR, EXTERNAL_HOME, EXTERNAL_COOKIE, LEGACY_COOKIE, LEGACY_EXTERNAL_COOKIE,
  EXTERNAL_DEPS, DEPS_NODE_MODULES, LINK_PATH, ensureExternalHome, stripCtrl,
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

function cookieStatus() {
  if (existsSync(EXTERNAL_COOKIE)) return 'external';
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
  log(`Cookie : ${c} (${c === 'external' ? EXTERNAL_COOKIE : c === 'legacy' ? LEGACY_COOKIE : '无'})`);
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
  const ok = c !== 'missing' && (l === 'link' || l === 'realdir') && d === 'present' && hashOk;
  log(ok ? 'OK：环境就绪' : 'NEEDS SETUP：请运行 node setup.mjs');
  process.exit(ok ? 0 : 1);
}

log('=== zhihu-auto 环境自愈 ===');
log(`外部家目录: ${EXTERNAL_HOME}`);
log('');

// ── 1. 外部家目录 ──
ensureExternalHome();

// ── 2. Cookie 迁移 ──
const haveExternal = existsSync(EXTERNAL_COOKIE);
if (haveExternal) {
  log(`[cookie] 外部已存在: ${EXTERNAL_COOKIE}`);
  // 外部已是主副本，仍清理 legacy 旧副本，收口凭据
  for (const legacy of [LEGACY_EXTERNAL_COOKIE, LEGACY_COOKIE]) {
    if (existsSync(legacy) && legacy !== EXTERNAL_COOKIE) {
      try { rmSync(legacy, { force: true }); log(`[cookie] 已删除旧副本: ${legacy}`); }
      catch { problems.push('cookie-legacy'); log(`[cookie] ⚠️ 未能删除旧副本，请手动清理: ${legacy}`); }
    }
  }
} else if (existsSync(LEGACY_EXTERNAL_COOKIE) || existsSync(LEGACY_COOKIE)) {
  const src = existsSync(LEGACY_EXTERNAL_COOKIE) ? LEGACY_EXTERNAL_COOKIE : LEGACY_COOKIE;
  copyFileSync(src, EXTERNAL_COOKIE);
  try { chmodSync(EXTERNAL_COOKIE, 0o600); } catch { /* Windows / 无权限：忽略 */ }
  log(`[cookie] 已迁移 Cookie 到外部: ${EXTERNAL_COOKIE}  ←  ${src}`);
  // 迁移成功后删除两个 legacy 旧副本，收口凭据（避免任一长期残留可用登录态）
  for (const legacy of [LEGACY_EXTERNAL_COOKIE, LEGACY_COOKIE]) {
    if (existsSync(legacy) && legacy !== EXTERNAL_COOKIE) {
      try { rmSync(legacy, { force: true }); log(`[cookie] 已删除旧副本: ${legacy}`); }
      catch { problems.push('cookie-legacy'); log(`[cookie] ⚠️ 未能删除旧副本，请手动清理: ${legacy}`); }
    }
  }
} else {
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
    const cmd = useCi ? ['ci'] : ['install'];
    log(`[deps] 安装到 ${EXTERNAL_DEPS}（npm ${cmd[0]}）...`);
    const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
    // Windows 上 .cmd 必须经 shell 启动（默认 shell:false 会 EINVAL 直接失败）；argv 固定无注入面
    const r = spawnSync(npmCmd, cmd, { cwd: EXTERNAL_DEPS, stdio: 'inherit', shell: process.platform === 'win32' });
    if (r.error || r.status !== 0) {
      log(`[deps] ⚠️ npm 安装失败${r.error ? '（启动失败: ' + r.error.code + '）' : ''}——请检查网络/代理后重试`);
      problems.push('npm');
    } else {
      log('[deps] 安装完成');
      installed = true;
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
