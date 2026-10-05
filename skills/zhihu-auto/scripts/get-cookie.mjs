/**
 * 知乎 Cookie 获取工具
 *
 * 流程：
 *   1. 打开可视化 Chrome 浏览器
 *   2. 导航到知乎登录页
 *   3. 等待用户手动登录（扫码/验证码/密码均可）
 *   4. 登录成功后自动提取 Cookie 并保存为 Netscape 格式
 *
 * 使用方法：node get-cookie.mjs
 *
 * ⚠️ 风险提示：
 *   - 导出的 Cookie 文件包含你的知乎登录凭证（z_c0）
 *   - 任何获得此文件的人都可以以你的身份访问知乎
 *   - 请妥善保管 Cookie 文件，不要分享给他人或上传到公共仓库
 *   - Cookie 有效期约 6 个月，过期需重新获取
 */

import { existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import {
  EXTERNAL_COOKIE, loadPuppeteer, ensureExternalHome, writeSecretFile, isZhihuDomain, stripCtrl,
  resolveMaxWaitSeconds, waitForEnterOrExit,
} from './lib/env.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ═══════════════════════════════════════════════════════════════
// ▼▼▼ 配置区 ▼▼▼
// ═══════════════════════════════════════════════════════════════

const OUTPUT_FILE = EXTERNAL_COOKIE;  // 存到 skill 目录之外，避免重装被清空
const LOGIN_URL = 'https://www.zhihu.com/signin';
const HOME_URL = 'https://www.zhihu.com';
const CHECK_INTERVAL = 2000;  // 每 2 秒检查一次登录状态
// ── CLI（此前本文件完全不读 process.argv，连 --help 都没有；与 extract.mjs 风格不一致）──
const ARGV = process.argv.slice(2);
const KNOWN_VALUE_FLAGS = ['--max-wait'];
for (let i = 0; i < ARGV.length; i++) {
  const t = String(ARGV[i]);
  if (t === '--help' || t === '-h' || t === '--no-wait') continue;
  if (KNOWN_VALUE_FLAGS.some((f) => t === f || t.startsWith(f + '='))) {
    if (!t.includes('=')) {
      const nxt = ARGV[i + 1];
      // 缺值 / 值是另一个 flag：显式报错而非静默回退（与 extract.mjs:65 同一判定）
      if (nxt === undefined || String(nxt).startsWith('-')) {
        console.error(`❌ 参数 ${t} 缺少取值（例如：${t}=600 或 ${t} 600）`);
        process.exit(2);
      }
      i++;
    }
    continue;
  }
  console.error(`❌ 未知参数: ${t}（只接受 --no-wait / --max-wait / --help；用法见 --help）`);
  process.exit(2);
}

if (ARGV.includes('--help') || ARGV.includes('-h')) {
  console.log(`用法：node get-cookie.mjs [--no-wait] [--max-wait <秒>]

  打开浏览器让用户手动登录知乎，成功后把 Cookie 写到 ${EXTERNAL_COOKIE}

  --no-wait         保存后立即关闭浏览器退出（不等 Enter）；agent / CI / 无人值守场景必用
  --max-wait <秒>   最长等待登录的秒数，>= 60，默认 300（等价写法：--max-wait=600）
                   也可用环境变量 ZHIHU_MAX_WAIT_MS（**毫秒**，如 600000）
  --help            显示本帮助

  注意：不给 --no-wait 时会等「按 Enter 才关浏览器」，最长 30 秒；
        stdin 若是管道/被关闭（无人值守）会立即自动关闭，不会挂住。`);
  process.exit(0);
}

// 最长等待：--max-wait（秒）> ZHIHU_MAX_WAIT_MS（毫秒）> 默认 300 秒。
// 无效值**报错退出**而不是静默回退默认 —— 静默回退会让 agent 以为自己设了 10 分钟。
const __mw = resolveMaxWaitSeconds(ARGV, process.env);
if (__mw.bad.length) {
  console.error(`❌ ${__mw.bad.join('、')} 无效：最长等待必须是 >= 60 秒（不回退默认，避免静默忽略你的设置）`);
  process.exit(2);
}
const MAX_WAIT = Math.round(__mw.seconds * 1000);
const NO_WAIT = ARGV.includes('--no-wait');

// ═══════════════════════════════════════════════════════════════
// ▲▲▲ 配置区结束 ▲▲▲
// ═══════════════════════════════════════════════════════════════

// 自动检测 Chrome 路径
function findChrome() {
  const candidates = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    process.env.LOCALAPPDATA + '\\Google\\Chrome\\Application\\chrome.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  throw new Error('未找到 Chrome，请手动指定 CHROME_PATH');
}

// CDP 获取所有 Cookie
async function getCookiesFromCDP(page) {
  const client = await page.createCDPSession();
  const { cookies } = await client.send('Network.getAllCookies');
  await client.detach();
  return cookies;
}

// 将 Chrome Cookie 转为 Netscape 格式
function toNetscapeFormat(cookies) {
  const lines = [
    '# Netscape HTTP Cookie File',
    '# https://curl.haxx.se/rfc/cookie_spec.html',
    '# This is a generated file! Do not edit.',
    '',
  ];

  let written = 0;
  let skipped = 0;
  let hasZc0 = false;
  for (const c of cookies) {
    // 只保留 zhihu.com 相关的 cookie
    if (!isZhihuDomain(c.domain)) continue;
    // 与 cookie-from-browser 对称：拒绝含控制字符的条目，避免破坏 Netscape 行列结构
    if (/[\u0000-\u001f\u007f-\u009f;]/.test(String(c.name)) ||
        /[\u0000-\u001f\u007f-\u009f;]/.test(String(c.value)) ||
        /[\u0000-\u001f\u007f-\u009f]/.test(String(c.domain)) ||
        /[\u0000-\u001f\u007f-\u009f]/.test(String(c.path || '/'))) { skipped++; continue; }

    const domain = c.domain;                          // 原样保留（带点=域 cookie，不带点=host-only）
    const includeSubdomains = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
    const path = c.path || '/';
    const secure = c.secure ? 'TRUE' : 'FALSE';
    const expires = c.expires ? Math.floor(c.expires) : 0;
    const name = c.name;
    const value = c.value;
    const prefix = c.httpOnly ? '#HttpOnly_' : '';

    lines.push(`${prefix}${domain}\t${includeSubdomains}\t${path}\t${secure}\t${expires}\t${name}\t${value}`);
    written++;
    if (name === 'z_c0' && String(value).trim().length > 0) hasZc0 = true;
  }

  return { content: lines.join('\n'), written, skipped, hasZc0 };
}

// 检测是否已登录（只检查 cookie，不检查 URL）
async function isLoggedIn(page) {
  try {
    const cookies = await getCookiesFromCDP(page);
    const hasZ_c0 = cookies.some(c => c.name === 'z_c0' && String(c.value || '').trim().length > 0 && isZhihuDomain(c.domain));
    return hasZ_c0;
  } catch {
    return false;
  }
}

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
  const { puppeteer, StealthPlugin } = await loadPuppeteer();
  puppeteer.use(StealthPlugin());

  console.log('╔══════════════════════════════════════════════╗');
  console.log('║       知乎 Cookie 获取工具                  ║');
  console.log('╚══════════════════════════════════════════════╝');
  console.log('');
  console.log('⚠️  风险提示：');
  console.log('   导出的 Cookie 包含你的知乎登录凭证');
  console.log('   任何获得此文件的人都可以以你的身份访问知乎');
  console.log('   请妥善保管，不要分享给他人或上传到公共仓库');
  console.log('');

  // 1. 启动浏览器
  console.log('[1/3] 启动浏览器...');
  const browser = await puppeteer.launch({
    executablePath: findChrome(),
    headless: false,
    defaultViewport: null,
    args: ['--no-first-run', '--disable-blink-features=AutomationControlled', '--lang=zh-CN', '--window-size=1280,900'],
  });

  const page = await browser.newPage();

  // 注入反检测
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    window.chrome = { runtime: {}, loadTimes(){}, csi(){}, app: {} };
  });

  // 2. 导航到登录页
  console.log('[2/3] 请在浏览器中登录知乎...');
  console.log('      支持：扫码 / 验证码 / 密码 / 微信 / QQ');
  console.log('');
  await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 30000 });

  // 3. 等待登录
  console.log('      等待登录中（最长 5 分钟）...');
  const startTime = Date.now();
  let loggedIn = false;

  while (Date.now() - startTime < MAX_WAIT) {
    await new Promise(r => setTimeout(r, CHECK_INTERVAL));

    // 只检查 cookie，不导航页面
    if (await isLoggedIn(page)) {
      loggedIn = true;
      break;
    }

    // 显示等待时间
    const elapsed = Math.floor((Date.now() - startTime) / 1000);
    process.stdout.write(`\r      已等待 ${elapsed} 秒...`);
  }

  console.log('');

  if (!loggedIn) {
    console.log('\n❌ 等待超时，未检测到登录');
    await browser.close();
    process.exit(1);
  }

  console.log('\n✅ 检测到登录成功！');
  console.log(`   当前页面: ${stripCtrl(await page.title())}`);

  // 4. 提取 Cookie
  console.log('\n[3/3] 提取 Cookie...');
  const allCookies = await getCookiesFromCDP(page);
  const zhihuCookies = allCookies.filter(c => isZhihuDomain(c.domain));

  console.log(`   共 ${allCookies.length} 个 Cookie，其中 ${zhihuCookies.length} 个属于 zhihu.com`);

  // 检查关键 Cookie
  const keyCookies = ['z_c0', 'd_c0', '__zse_ck', '_xsrf'];
  for (const name of keyCookies) {
    const found = zhihuCookies.some(c => c.name === name);
    console.log(`   ${found ? '✅' : '❌'} ${name}`);
  }

  // 5. 保存（fail-closed：登录态不完整则拒写，避免用残缺 Cookie 覆盖唯一登录源）
  const fmt = toNetscapeFormat(allCookies);
  if (fmt.skipped) console.log(`   ⚠️ 跳过 ${fmt.skipped} 条含控制字符的 cookie`);
  if (!fmt.hasZc0) {
    console.error('\n❌ 登录态不完整（未取到 z_c0），拒绝覆盖现有 Cookie 文件；请重试登录。');
    await browser.close();
    process.exit(1);
  }
  ensureExternalHome();
  writeSecretFile(OUTPUT_FILE, fmt.content);

  console.log(`\n✅ Cookie 已保存到: ${OUTPUT_FILE}（写入 ${fmt.written} 条）`);
  console.log('   有效期约 6 个月，过期后需重新获取');
  console.log('');
  console.log('   现在可以关闭浏览器，使用 extract.mjs 抓取回答了');

  // 保持浏览器打开，让用户确认
  if (NO_WAIT) {
    console.log('\n   --no-wait：直接关闭浏览器退出。');
  } else {
    console.log('\n   按 Enter 关闭浏览器（--no-wait 可跳过；无人值守时最多等 30 秒后自动关闭）...');
    // 只监听 data 会在非交互 stdin（管道 / ignore / 被关闭）下**永不 resolve**：
    // agent 用后台任务跑会永久挂住、Chrome 变孤儿、进程常驻。waitForEnterOrExit 三路兜底。
    const closed = await waitForEnterOrExit(30000);
    if (!closed) console.log('   （stdin 无输入，已自动关闭浏览器）');
  }

  await browser.close();
}

main().catch(err => {
  console.error('\n❌ 执行出错:', stripCtrl(err && err.message || err));
  process.exit(1);
});
