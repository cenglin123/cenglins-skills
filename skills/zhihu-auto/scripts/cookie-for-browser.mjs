/**
 * 统一登录态：把 API 通道的 Cookie 文件转成一段「注入代码」，供 playwright MCP 复用。
 *
 * playwright MCP 的代码沙箱**没有 fs / require / 动态 import**，读不了文件；但能调用
 * `page.context().addCookies(...)`。所以这里把 Cookie 内联进一段函数，写成文件，
 * 再让 `browser_run_code_unsafe({ filename: <该文件> })` 执行。
 *
 * 用法（浏览器通道任务开始时）：
 *   node <SKILL_DIR>/scripts/cookie-for-browser.mjs
 *   # → 生成 <SKILL_DIR>/scripts/.browser-login-code.js
 *   browser_run_code_unsafe({ filename: "<SKILL_DIR>/scripts/.browser-login-code.js" })
 *   # → 返回 { ok:true, count:N }；随后 browser_navigate 到 zhihu.com 即已登录
 *
 * 单一登录源：`~/.zhihu-auto/www.zhihu.com_cookies.txt`（get-cookie.mjs 写）。
 * 只要该文件有效（约 6 个月），edit-answer / publish-column 就**不必每会话扫码**。
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname, join } from 'path';
import { fileURLToPath } from 'url';
import { resolveCookieFile } from './lib/env.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(__dirname, '.browser-login-code.js');

const file = resolveCookieFile();
if (!existsSync(file)) {
  console.error('❌ 未找到 Cookie 文件。先运行 get-cookie.mjs（或 setup.mjs）生成。');
  process.exit(1);
}

const cookies = readFileSync(file, 'utf-8')
  .split('\n')
  .filter((l) => l && !l.startsWith('#'))
  .map((l) => l.split('\t'))
  .filter((p) => p.length >= 7)
  .map((p) => ({
    name: p[5],
    value: p.slice(6).join('\t'),
    domain: p[0].startsWith('.') ? p[0] : '.' + p[0],
    path: p[2] || '/',
    secure: p[3] === 'TRUE',
    httpOnly: false,
    expires: Number(p[4]) || -1,
    sameSite: 'Lax',
  }));

if (!cookies.length) {
  console.error('❌ Cookie 文件里没有可解析的条目。');
  process.exit(1);
}

const code =
  'async (page) => { await page.context().addCookies(' +
  JSON.stringify(cookies) +
  '); return { ok: true, count: ' +
  cookies.length +
  ' }; }';

writeFileSync(OUT, code, 'utf-8');
console.log(`✅ 已生成注入代码：${OUT}`);
console.log(`   来源 Cookie：${file}（${cookies.length} 条）`);
console.log('   下一步：browser_run_code_unsafe({ filename: "' + OUT + '" })');
