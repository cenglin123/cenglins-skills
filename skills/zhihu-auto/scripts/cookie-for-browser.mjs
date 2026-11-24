/**
 * 统一登录态：把 API 通道的 Cookie 文件转成一段「注入代码」，供 playwright MCP 复用。
 *
 * playwright MCP 的代码沙箱**没有 fs / require / 动态 import**，读不了文件；但能调用
 * `page.context().addCookies(...)`。所以这里把 Cookie 内联进一段函数，写成文件，
 * 再让 `browser_run_code_unsafe({ filename: <该文件> })` 执行。
 *
 * 用法（浏览器通道任务开始时）：
 *   node <SKILL_DIR>/scripts/cookie-for-browser.mjs
 *   # → 生成 ~/.zhihu-auto/browser-login-code.js（含凭证，0600，勿入库）
 *   browser_run_code_unsafe({ filename: "<外部家目录>/browser-login-code.js" })
 *   # → 返回 { ok:true, count:N }；随后 browser_navigate 到 zhihu.com 即已登录
 *
 * 单一登录源：`~/.zhihu-auto/www.zhihu.com_cookies.txt`（get-cookie.mjs 写）。
 * 只要该文件有效（约 6 个月），edit-answer / publish-column 就**不必每会话扫码**。
 */

import { readFileSync, existsSync, rmSync } from 'fs';
import { join } from 'path';
import { resolveCookieFile, EXTERNAL_HOME, writeSecretFile, parseNetscapeCookieText, isZhihuDomain } from './lib/env.mjs';

const OUT = join(EXTERNAL_HOME, 'browser-login-code.js');

// --rm：删除已生成的注入代码（含凭证），用完清理用
if (process.argv.slice(2).includes('--rm')) {
  if (existsSync(OUT)) { rmSync(OUT, { force: true }); console.log('🗑️ 已删除注入代码：' + OUT); }
  else console.log('（无注入代码可删）');
  process.exit(0);
}

const file = resolveCookieFile();
if (!existsSync(file)) {
  console.error('❌ 未找到 Cookie 文件。先运行 get-cookie.mjs（或 setup.mjs）生成。');
  process.exit(1);
}

// 只注入知乎域 cookie（域原样保留：带前导点=域 cookie，不带点=host-only）；sameSite 仅补缺省 Lax
const cookies = parseNetscapeCookieText(readFileSync(file, 'utf-8'))
  .filter((c) => isZhihuDomain(c.domain))
  .map((c) => ({ ...c, sameSite: 'Lax' }));

if (!cookies.length) {
  console.error('❌ Cookie 文件里没有知乎域的条目。');
  process.exit(1);
}

const code =
  'async (page) => { await page.context().addCookies(' +
  JSON.stringify(cookies) +
  '); return { ok: true, count: ' +
  cookies.length +
  ' }; }';

writeSecretFile(OUT, code);
console.log(`✅ 已生成注入代码：${OUT}`);
console.log(`   来源 Cookie：${file}（${cookies.length} 条）`);
console.log('   下一步：browser_run_code_unsafe({ filename: "' + OUT + '" })');
console.log('   提示：该文件含登录凭证，用完删除：node <本脚本> --rm');
