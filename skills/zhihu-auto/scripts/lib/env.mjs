/**
 * 共享环境路径解析（零依赖）
 *
 * 为什么存在：cc-switch 从 GitHub 重装 skill 时会**整体替换 skill 目录**，
 * 而 node_modules、Cookie 这类文件被 .gitignore 排除、不入库，于是被一并清空
 * （2026-09 实际发生过一次）。解决办法是把「秘密与依赖」放到 skill 目录之外：
 *
 *   外部家目录（默认 ~/.zhihu-auto；优先级 ZHIHU_AUTO_HOME > ZHIHU_EXTRACTOR_HOME）：
 *     www.zhihu.com_cookies.txt   知乎登录 Cookie（跨重装存活）
 *     deps/node_modules           依赖（通过 junction/symlink 链回 scripts/node_modules）
 *
 * 依赖通过脚本目录下的 `node_modules` 软链/junction 暴露给 Node 的模块解析，
 * 因此 cc-switch 重装后只需重跑 setup.mjs 重建链接即可（无需重新下载依赖）。
 */

import { existsSync, mkdirSync, symlinkSync, writeFileSync, chmodSync } from 'fs';
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
 * 外部家目录加固：创建并（POSIX 上）设为 0700。
 * 该目录存放 Cookie 等密钥；Windows 的 chmod 基本无效，仍调用以保持一致。
 */
export function ensureExternalHome() {
  mkdirSync(EXTERNAL_HOME, { recursive: true });
  try {
    chmodSync(EXTERNAL_HOME, 0o700);
  } catch {
    /* Windows / 无权限：忽略 */
  }
}

/** 写入含密钥的文件（Cookie、注入代码等）：父目录 0700、文件 0600（POSIX）。 */
export function writeSecretFile(filePath, data) {
  const dir = dirname(filePath);
  mkdirSync(dir, { recursive: true });
  try {
    chmodSync(dir, 0o700);
  } catch {
    /* Windows / 无权限：忽略 */
  }
  writeFileSync(filePath, data, { encoding: 'utf-8', mode: 0o600 });
  try {
    chmodSync(filePath, 0o600);
  } catch {
    /* Windows / 无权限：忽略 */
  }
}

/**
 * 解析 Netscape Cookie 文本（get-cookie / Cookie-Editor 导出格式）。
 * 处理 `#HttpOnly_` 前缀：带该前缀的行是 httpOnly cookie，不能当注释跳过，
 * 解析时须保留 httpOnly=true（否则往返注入会把 z_c0 降级成页面脚本可读）。
 * 返回统一的 cookie 对象；调用方按需补 sameSite 等字段。
 */
export function parseNetscapeCookieText(text) {
  const out = [];
  for (let line of String(text).split(/\r?\n/)) {
    let httpOnly = false;
    if (line.startsWith('#HttpOnly_')) {
      httpOnly = true;
      line = line.slice('#HttpOnly_'.length);
    } else if (line.startsWith('#')) {
      continue;
    }
    if (!line.trim()) continue;
    const parts = line.split('\t');
    if (parts.length < 7) continue;
    const [domain, includeSub, path, secure, expires, name, ...valueParts] = parts;
    const hostOnly = (includeSub || '').toUpperCase() === 'FALSE';
    // 以 Netscape 第 2 列（host-only 标志）为准，让 domain 与该标志自洽：
    // host-only → 去前导点；域 cookie → 补前导点。避免「点号与第 2 列不一致」时作用域歧义。
    const scopedDomain = hostOnly
      ? domain.replace(/^\./, '')
      : domain.startsWith('.') ? domain : '.' + domain;
    out.push({
      name,
      value: valueParts.join('\t'),
      domain: scopedDomain,
      hostOnly,
      path: path || '/',
      secure: secure === 'TRUE',
      httpOnly,
      expires: expires === '0' ? -1 : parseInt(expires, 10) || -1,
    });
  }
  return out;
}

/** 判断域是否属于知乎（严格后缀匹配，避免 `zhihu.com.evil.tld` 之类被误放行）。 */
export function isZhihuDomain(domain) {
  const d = String(domain || '').replace(/^\./, '').toLowerCase();
  return d === 'zhihu.com' || d.endsWith('.zhihu.com');
}

/**
 * 只挑「对指定主机真正生效」的 cookie 拼成 Cookie 请求头：遵守域/子域/host-only 与过期时间。
 * 修复：不再把 Cookie 文件里**其它域**的条目一并发往知乎。
 * 注意：这是**主机级**匹配，未实现 RFC 6265 的 path 级作用域（对本技能的 /api/v4 请求足够）。
 */
export function cookiesToHeader(cookies, host) {
  const h = String(host || '').toLowerCase();
  const now = Date.now() / 1000;
  return cookies
    .filter((c) => {
      if (!isZhihuDomain(c.domain)) return false;   // 本技能只与知乎交互，非知乎域一律不发
      const d = String(c.domain || '').replace(/^\./, '').toLowerCase();
      if (!d) return false;
      const domainMatch = c.hostOnly ? d === h : h === d || h.endsWith('.' + d);
      if (!domainMatch) return false;
      if (typeof c.expires === 'number' && c.expires > 0 && c.expires < now) return false;
      return true;
    })
    .map((c) => `${c.name}=${c.value}`)
    .join('; ');
}

/** 从任意输入里提取合法知乎 qid（连续 6+ 位数字）；防止把 JSON 字段直接当文件名/URL 用而穿越目录。 */
export function sanitizeQid(raw) {
  const m = String(raw ?? '').match(/\d{6,}/);
  return m ? m[0] : '';
}

/** 剥掉 C0/C1/DEL 控制字符（保留 \t\n\r），供把远程内容回显终端前使用，防 ANSI/OSC 序列注入。 */
export function stripCtrl(s) {
  return String(s ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, '');
}

/**
 * 归一化「最长等待秒数」：--max-wait（**秒**）> ZHIHU_MAX_WAIT_MS（**毫秒**）> 默认。
 *
 * 两种写法都收：`--max-wait=120` 与 `--max-wait 120`（与 extract.mjs 的 parseCliArgs 一致）。
 * 纯函数、不读 process.*，因此可以脱离浏览器离线单测。
 * 返回 { seconds, bad }：bad 非空表示用户**显式**给了无效值 —— 调用方应报错退出，
 * 而不是静默回退默认（静默回退会让「我明明设了 10 分钟」变成 5 分钟，且无处可查）。
 */
export function resolveMaxWaitSeconds(argv = [], env = {}, opts = {}) {
  const def = opts.defaultSeconds ?? 300;
  const min = opts.minSeconds ?? 60;
  let flagSeconds = null;
  const eq = argv.find((a) => String(a).startsWith('--max-wait='));
  if (eq !== undefined) {
    flagSeconds = Number(String(eq).slice('--max-wait='.length));
  } else {
    const i = argv.findIndex((a) => a === '--max-wait');
    if (i >= 0) {
      const nxt = argv[i + 1];
      // 缺值 / 值是另一个 flag —— 与 extract.mjs 的 parseCliArgs 同一判定：
      // extract.mjs:65 显式 `if (!value || value.startsWith('--')) throw new Error('参数 --x 缺少值')`
      // R1 内循环发现：若只修「未知参数」不修「缺值」，`--max-wait` 单独出现会被静默回退 300 秒，
      // 这正是本函数注释里宣称已消灭的那一类。故此处必须置 bad 非空。
      if (nxt === undefined || String(nxt).startsWith('-')) {
        return { seconds: def, bad: ['--max-wait (缺少取值)'] };
      }
      flagSeconds = Number(nxt);
    }
  }
  if (flagSeconds !== null) {
    if (Number.isFinite(flagSeconds) && flagSeconds >= min) return { seconds: flagSeconds, bad: [] };
    return { seconds: def, bad: [`--max-wait=${flagSeconds}`] };
  }
  const envMs = Number(env.ZHIHU_MAX_WAIT_MS);
  if (Number.isFinite(envMs) && envMs > 0) {
    if (envMs >= min * 1000) return { seconds: envMs / 1000, bad: [] };
    return { seconds: def, bad: [`ZHIHU_MAX_WAIT_MS=${envMs}`] };
  }
  return { seconds: def, bad: [] };
}

/**
 * 等待「用户按 Enter 关闭浏览器」，但**保证一定返回**：
 *   - 收到数据（有人按了 Enter）→ resolve(true)
 *   - stdin 结束（管道 EOF / 被关闭 / 无人值守）→ resolve(false)
 *   - 超时（默认 30s）→ resolve(false)
 * 只监听 `data` 会在非交互 stdin 下**永不 resolve**（agent / CI 跑后台任务必挂）。
 * 导出为纯函数以便离线单测（可注入 input）。
 */
export function waitForEnterOrExit(timeoutMs = 30000, input = process.stdin) {
  return new Promise((resolve) => {
    const t = setTimeout(() => resolve(false), timeoutMs);
    input.once('data', () => { clearTimeout(t); resolve(true); });
    input.once('end', () => { clearTimeout(t); resolve(false); });
  });
}

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
