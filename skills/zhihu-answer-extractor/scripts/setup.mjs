#!/usr/bin/env node
/**
 * 环境自愈脚本（幂等，可反复运行）
 *
 * 场景：cc-switch / 其他管理器从 GitHub 重装本 skill 时，会整体替换 skill 目录，
 * 导致被 .gitignore 排除的 node_modules 与 Cookie 丢失。本脚本把这两样放到
 * skill 目录之外（~/.zhihu-answer-extractor），并在 skill 目录被重建后一键恢复：
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
  existsSync, mkdirSync, copyFileSync, lstatSync, rmSync, symlinkSync, readdirSync,
} from 'fs';
import { join } from 'path';
import { spawnSync } from 'child_process';
import {
  SCRIPTS_DIR, EXTERNAL_HOME, EXTERNAL_COOKIE, LEGACY_COOKIE,
  EXTERNAL_DEPS, DEPS_NODE_MODULES, LINK_PATH,
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

if (CHECK) {
  const c = cookieStatus(), d = depsStatus(), l = linkStatus();
  log(`Cookie : ${c} (${c === 'external' ? EXTERNAL_COOKIE : c === 'legacy' ? LEGACY_COOKIE : '无'})`);
  log(`Deps   : ${d}`);
  log(`Link   : ${l}`);
  const ok = c === 'external' && d === 'present' && l === 'link';
  log(ok ? 'OK：环境就绪' : 'NEEDS SETUP：请运行 node setup.mjs');
  process.exit(ok ? 0 : 1);
}

log('=== zhihu-answer-extractor 环境自愈 ===');
log(`外部家目录: ${EXTERNAL_HOME}`);
log('');

// ── 1. 外部家目录 ──
mkdirSync(EXTERNAL_HOME, { recursive: true });

// ── 2. Cookie 迁移 ──
let cs = cookieStatus();
if (cs === 'legacy' && !existsSync(EXTERNAL_COOKIE)) {
  copyFileSync(LEGACY_COOKIE, EXTERNAL_COOKIE);
  log(`[cookie] 已从 skill 内旧路径迁移到外部: ${EXTERNAL_COOKIE}`);
  cs = 'external';
} else if (cs === 'external') {
  log(`[cookie] 外部已存在: ${EXTERNAL_COOKIE}`);
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
if (NO_INSTALL) {
  log(`[deps] 跳过安装（--no-install）；当前状态: ${ds}`);
} else if (ds === 'present' && !REINSTALL) {
  log('[deps] 外部依赖已安装，跳过');
} else {
  const useCi = existsSync(join(EXTERNAL_DEPS, 'package-lock.json'));
  const cmd = useCi ? ['ci'] : ['install'];
  log(`[deps] 安装到 ${EXTERNAL_DEPS}（npm ${cmd[0]}）...`);
  const npmCmd = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const r = spawnSync(npmCmd, cmd, { cwd: EXTERNAL_DEPS, stdio: 'inherit' });
  if (r.status !== 0) {
    log('[deps] ⚠️ npm 安装失败——请检查网络/代理后重试');
    problems.push('npm');
  } else {
    log('[deps] 安装完成');
  }
  ds = depsStatus();
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
    log(`[link] ⚠️ 创建链接失败: ${err.message}`);
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
