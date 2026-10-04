/**
 * 统一登录态（反向，**已弃用/不在受支持流程内**）：把 MCP 浏览器里的 zhihu cookies 写成 Netscape 文件。
 *
 * ⚠️ 为什么弃用：playwright MCP 沙箱没有 fs，浏览器里的 cookie 只能经
 * `browser_run_code_unsafe` 的**返回值**传出——等于把含 `z_c0` 的完整凭据明文送进模型上下文与会话日志，
 * 这是**无法在代码层消除**的凭证暴露。需要登录态时，一律用 **get-cookie.mjs 本地扫码**取得唯一登录源。
 *
 * 仅当调用者**明确知情并接受上述风险**时，才手动把 cookie JSON 喂入本脚本：
 *   node <SKILL_DIR>/scripts/cookie-from-browser.mjs --in <JSON 文件>
 *   node <SKILL_DIR>/scripts/cookie-from-browser.mjs --in -        # 从 stdin 读，避免中间文件
 *   → 覆写 ~/.zhihu-auto/www.zhihu.com_cookies.txt（Netscape 格式；只接受知乎域、拒绝含控制字符/分号的条目）
 *
 * 安全门：过滤后若不含**非空** z_c0，默认**拒绝覆盖**唯一登录源（exit 1）；确需写入请显式加 `--allow-no-zc0`。
 */

import { readFileSync } from 'fs';
import { resolve } from 'path';
import { EXTERNAL_COOKIE, ensureExternalHome, writeSecretFile, isZhihuDomain, stripCtrl } from './lib/env.mjs';

const args = process.argv.slice(2);
const i = args.indexOf('--in');
const inPath = i >= 0 ? args[i + 1] : '';
if (!inPath) {
  console.error('用法: node cookie-from-browser.mjs --in <MCP 导出的 cookies JSON 文件>');
  console.error('      --in -  从标准输入读取 JSON（避免中间文件落盘）');
  process.exit(2);
}

let rawText;
try {
  rawText = inPath === '-' ? readFileSync(0, 'utf-8') : readFileSync(resolve(inPath), 'utf-8');
} catch (e) {
  console.error('❌ 读取失败: ' + stripCtrl(e.message));
  process.exit(2);
}

let cookies;
try {
  cookies = JSON.parse(rawText);
} catch (e) {
  console.error('❌ 解析 JSON 失败（为免泄露，不回显原始内容或错误详情）。');
  process.exit(2);
}
if (!Array.isArray(cookies)) {
  console.error('❌ JSON 顶层必须是数组（Playwright cookies 列表）。');
  process.exit(2);
}

cookies = cookies.filter((c) => c && typeof c.name === 'string' && typeof c.value === 'string' && isZhihuDomain(c.domain));
if (!cookies.length) {
  console.error('❌ 没有 zhihu.com 的 cookie。');
  process.exit(1);
}

const lines = [
  '# Netscape HTTP Cookie File',
  '# https://curl.haxx.se/rfc/cookie_spec.html',
  '# This is a generated file! Do not edit.',
  '',
];
let skippedCtrl = 0;
let acceptedZc0 = false;
for (const c of cookies) {
  const name = String(c.name);
  const value = String(c.value);
  const dom = String(c.domain);
  const path = String(c.path || '/');
  // 拒绝含控制字符的条目：name/value 还不得含分号；domain/path 不得含控制字符（会破坏列/行格式）
  const bad = /[\u0000-\u001f\u007f-\u009f;]/;
  const badNoSemi = /[\u0000-\u001f\u007f-\u009f]/;
  if (bad.test(name) || bad.test(value) || badNoSemi.test(dom) || badNoSemi.test(path)) {
    skippedCtrl++;
    continue;
  }
  const domain = dom;                                 // 原样保留：带点=域 cookie，不带点=host-only
  const includeSubdomains = dom.startsWith('.') ? 'TRUE' : 'FALSE';
  const secure = c.secure ? 'TRUE' : 'FALSE';
  const expires = c.expires && c.expires > 0 ? Math.floor(c.expires) : 0;
  const prefix = c.httpOnly ? '#HttpOnly_' : '';
  lines.push([prefix + domain, includeSubdomains, path, secure, expires, name, value].join('\t'));
  if (name === 'z_c0' && value.trim().length > 0) acceptedZc0 = true;
}
if (skippedCtrl) console.error(`⚠️ 跳过 ${skippedCtrl} 条含控制字符的 cookie（异常输入）`);

const written = lines.length - 4;
const allowNoZc0 = process.argv.slice(2).includes('--allow-no-zc0');
if (written <= 0) {
  console.error('❌ 过滤后没有可写入的 cookie（异常输入）。');
  process.exit(1);
}
if (!acceptedZc0 && !allowNoZc0) {
  console.error('❌ 未包含 z_c0（登录态不完整），拒绝覆盖唯一登录源；确需写入请显式加 --allow-no-zc0。');
  process.exit(1);
}
ensureExternalHome();
writeSecretFile(EXTERNAL_COOKIE, lines.join('\n'));
console.log(`✅ 已从 MCP 导出 ${written} 条 zhihu cookie → ${EXTERNAL_COOKIE}（含 z_c0: ${acceptedZc0}）`);
