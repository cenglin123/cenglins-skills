/**
 * 共享环境路径解析（零依赖）
 *
 * 为什么存在：cc-switch 从 GitHub 重装 skill 时会**整体替换 skill 目录**，
 * 而 node_modules、Cookie 这类文件被 .gitignore 排除、不入库，于是被一并清空
 * （2026-09 实际发生过一次）。解决办法是把「秘密与依赖」放到 skill 目录之外：
 *
 *   外部家目录（默认 ~/.zhihu-answer-extractor，可用 ZHIHU_EXTRACTOR_HOME 覆盖）：
 *     www.zhihu.com_cookies.txt   知乎登录 Cookie（跨重装存活）
 *     deps/node_modules           依赖（通过 junction/symlink 链回 scripts/node_modules）
 *
 * 依赖通过脚本目录下的 `node_modules` 软链/junction 暴露给 Node 的模块解析，
 * 因此 cc-switch 重装后只需重跑 setup.mjs 重建链接即可（无需重新下载依赖）。
 */

import { existsSync, mkdirSync, symlinkSync } from 'fs';
import { resolve, dirname, join } from 'path';
import { homedir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

export const SCRIPTS_DIR = resolve(__dirname, '..');       // <skill>/scripts
export const SKILL_DIR = resolve(SCRIPTS_DIR, '..');       // <skill>

export const EXTERNAL_HOME =
  process.env.ZHIHU_AUTO_HOME || process.env.ZHIHU_EXTRACTOR_HOME || join(homedir(), '.zhihu-auto');
export const LEGACY_EXTERNAL_HOME = join(homedir(), '.zhihu-answer-extractor');
export const EXTERNAL_COOKIE = join(EXTERNAL_HOME, 'www.zhihu.com_cookies.txt');
export const LEGACY_EXTERNAL_COOKIE = join(LEGACY_EXTERNAL_HOME, 'www.zhihu.com_cookies.txt');
export const LEGACY_COOKIE = join(SCRIPTS_DIR, 'www.zhihu.com_cookies.txt');
export const EXTERNAL_DEPS = join(EXTERNAL_HOME, 'deps');
export const DEPS_NODE_MODULES = join(EXTERNAL_DEPS, 'node_modules');
export const LINK_PATH = join(SCRIPTS_DIR, 'node_modules');

/**
 * 解析 Cookie 文件路径，优先级：
 *   1) 环境变量 ZHIHU_COOKIE_FILE（存在时）
 *   2) 外部家目录（推荐，跨重装存活）
 *   3) skill 内旧路径（兼容；同样会被重装清空）
 * 调用方在拿到路径后应自行处理「不存在」的情形。
 */
export function resolveCookieFile() {
  if (process.env.ZHIHU_COOKIE_FILE && existsSync(process.env.ZHIHU_COOKIE_FILE)) {
    return process.env.ZHIHU_COOKIE_FILE;
  }
  if (existsSync(EXTERNAL_COOKIE)) return EXTERNAL_COOKIE;
  if (existsSync(LEGACY_EXTERNAL_COOKIE)) return LEGACY_EXTERNAL_COOKIE;
  return LEGACY_COOKIE;
}

/** 依赖是否已就位（scripts/node_modules 存在即可——实体目录或指向外部的链接都行）。 */
export function depsReady() {
  return existsSync(LINK_PATH);
}

/**
 * 自愈：把依赖软链从「外部备份」重建回 skill 目录。
 * 场景：cc-switch 等从 GitHub 重装 skill 会整体替换 skill 目录，清掉 gitignored 的
 * scripts/node_modules；而依赖本体一直备份在 `~/.zhihu-auto/deps/node_modules`（不受重装影响）。
 * 本函数在需要时把软链重建回去（不重装、不联网）。
 * 返回 { ok, action }；ok=false 通常意味着外部备份也没有（需跑 setup.mjs 重装）。
 */
export function ensureDeps() {
  if (existsSync(LINK_PATH)) return { ok: true, action: 'present' };
  if (existsSync(DEPS_NODE_MODULES)) {
    try {
      mkdirSync(dirname(LINK_PATH), { recursive: true });
      symlinkSync(DEPS_NODE_MODULES, LINK_PATH, process.platform === 'win32' ? 'junction' : 'dir');
      return { ok: true, action: 'relinked' };
    } catch (err) {
      return { ok: false, action: 'link-failed', error: err.message };
    }
  }
  return { ok: false, action: 'no-backup' };
}

/**
 * 加载 puppeteer 依赖，带自愈：若 import 因缺依赖失败，先从外部备份重建软链，再重试一次。
 * 供 extract / open / get-cookie 使用。
 */
export async function loadPuppeteer() {
  const load = () => Promise.all([
    import('puppeteer-extra'),
    import('puppeteer-extra-plugin-stealth'),
  ]);
  try {
    const [a, b] = await load();
    return { puppeteer: a.default, StealthPlugin: b.default };
  } catch (err) {
    const r = ensureDeps();
    if (!r.ok) {
      throw new Error(
        `依赖缺失，且外部备份不可用（${r.action}${r.error ? ': ' + r.error : ''}）。\n` +
          `请运行: node "${join(SCRIPTS_DIR, 'setup.mjs')}"`,
      );
    }
    const [a, b] = await load();
    return { puppeteer: a.default, StealthPlugin: b.default };
  }
}

/** 依赖缺失时的统一提示 + 退出。 */
export function requireDepsOrExit() {
  if (depsReady()) return;
  console.error('❌ 依赖未安装（node_modules 缺失，可能是 cc-switch 重装清空了 skill 目录）。');
  console.error('   请先运行:  node "' + join(SCRIPTS_DIR, 'setup.mjs') + '"');
  process.exit(2);
}
