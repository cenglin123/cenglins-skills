/**
 * 统一登录态（反向）：把 playwright MCP 浏览器里的 zhihu cookies 导回 Cookie 文件。
 *
 * 与 cookie-for-browser.mjs（file → MCP）配对，实现**双向统一**：
 * 无论你是在 get-cookie.mjs（puppeteer）还是 MCP 浏览器里登录，同步一次两边即一致。
 *
 * 用法：
 *   1) 在 MCP 里取出 zhihu cookies（JSON）：
 *      browser_run_code_unsafe({ code: "async (page) => (await page.context().cookies()).filter(c => (c.domain||'').includes('zhihu.com'))" })
 *   2) 把该 JSON 存成文件（如 <tmp>/zhihu-cookies.json）
 *   3) node <SKILL_DIR>/scripts/cookie-from-browser.mjs --in <tmp>/zhihu-cookies.json
 *      → 覆写 ~/.zhihu-auto/www.zhihu.com_cookies.txt（Netscape 格式）
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { resolve } from 'path';
import { EXTERNAL_HOME, EXTERNAL_COOKIE } from './lib/env.mjs';

const args = process.argv.slice(2);
const i = args.indexOf('--in');
const inPath = i >= 0 ? args[i + 1] : '';
if (!inPath) {
  console.error('用法: node cookie-from-browser.mjs --in <MCP 导出的 cookies JSON 文件>');
  process.exit(2);
}
if (!existsSync(inPath)) {
  console.error('❌ 找不到输入文件: ' + inPath);
  process.exit(2);
}

let cookies;
try {
  cookies = JSON.parse(readFileSync(resolve(inPath), 'utf-8'));
} catch (e) {
  console.error('❌ 解析 JSON 失败: ' + e.message);
  process.exit(2);
}
if (!Array.isArray(cookies)) {
  console.error('❌ JSON 顶层必须是数组（Playwright cookies 列表）。');
  process.exit(2);
}

cookies = cookies.filter((c) => c && typeof c.name === 'string' && (c.domain || '').includes('zhihu.com'));
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
for (const c of cookies) {
  const domain = c.domain.startsWith('.') ? c.domain : '.' + c.domain;
  const includeSubdomains = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
  const path = c.path || '/';
  const secure = c.secure ? 'TRUE' : 'FALSE';
  const expires = c.expires && c.expires > 0 ? Math.floor(c.expires) : 0;
  lines.push([domain, includeSubdomains, path, secure, expires, c.name, c.value].join('\t'));
}

mkdirSync(EXTERNAL_HOME, { recursive: true });
writeFileSync(EXTERNAL_COOKIE, lines.join('\n'), 'utf-8');
const hasZc0 = cookies.some((c) => c.name === 'z_c0');
console.log(`✅ 已从 MCP 导出 ${cookies.length} 条 zhihu cookie → ${EXTERNAL_COOKIE}（含 z_c0: ${hasZc0}）`);
