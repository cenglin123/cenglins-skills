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

import { existsSync } from 'fs';
import { resolve, dirname, join } from 'path';
import { homedir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

export const SCRIPTS_DIR = resolve(__dirname, '..');       // <skill>/scripts
export const SKILL_DIR = resolve(SCRIPTS_DIR, '..');       // <skill>

export const EXTERNAL_HOME =
  process.env.ZHIHU_EXTRACTOR_HOME || join(homedir(), '.zhihu-answer-extractor');
export const EXTERNAL_COOKIE = join(EXTERNAL_HOME, 'www.zhihu.com_cookies.txt');
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
  return LEGACY_COOKIE;
}

/** 依赖是否已就位（scripts/node_modules 存在即可——实体目录或指向外部的链接都行）。 */
export function depsReady() {
  return existsSync(LINK_PATH);
}

/** 依赖缺失时的统一提示 + 退出。 */
export function requireDepsOrExit() {
  if (depsReady()) return;
  console.error('❌ 依赖未安装（node_modules 缺失，可能是 cc-switch 重装清空了 skill 目录）。');
  console.error('   请先运行:  node "' + join(SCRIPTS_DIR, 'setup.mjs') + '"');
  process.exit(2);
}
