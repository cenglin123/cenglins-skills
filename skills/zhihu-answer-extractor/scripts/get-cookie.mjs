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

import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ═══════════════════════════════════════════════════════════════
// ▼▼▼ 配置区 ▼▼▼
// ═══════════════════════════════════════════════════════════════

const OUTPUT_FILE = resolve(__dirname, 'www.zhihu.com_cookies.txt');
const LOGIN_URL = 'https://www.zhihu.com/signin';
const HOME_URL = 'https://www.zhihu.com';
const CHECK_INTERVAL = 2000;  // 每 2 秒检查一次登录状态
const MAX_WAIT = 300000;      // 最长等待 5 分钟

// ═══════════════════════════════════════════════════════════════
// ▲▲▲ 配置区结束 ▲▲▲
// ═══════════════════════════════════════════════════════════════

puppeteer.use(StealthPlugin());

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

  for (const c of cookies) {
    // 只保留 zhihu.com 相关的 cookie
    if (!c.domain.includes('zhihu.com')) continue;

    const domain = c.domain.startsWith('.') ? c.domain : '.' + c.domain;
    const includeSubdomains = c.domain.startsWith('.') ? 'TRUE' : 'FALSE';
    const path = c.path || '/';
    const secure = c.secure ? 'TRUE' : 'FALSE';
    const expires = c.expires ? Math.floor(c.expires) : 0;
    const name = c.name;
    const value = c.value;

    lines.push(`${domain}\t${includeSubdomains}\t${path}\t${secure}\t${expires}\t${name}\t${value}`);
  }

  return lines.join('\n');
}

// 检测是否已登录（只检查 cookie，不检查 URL）
async function isLoggedIn(page) {
  try {
    const cookies = await getCookiesFromCDP(page);
    const hasZ_c0 = cookies.some(c => c.name === 'z_c0' && c.domain.includes('zhihu.com'));
    return hasZ_c0;
  } catch {
    return false;
  }
}

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
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
  console.log(`   当前页面: ${await page.title()}`);

  // 4. 提取 Cookie
  console.log('\n[3/3] 提取 Cookie...');
  const allCookies = await getCookiesFromCDP(page);
  const zhihuCookies = allCookies.filter(c => c.domain.includes('zhihu.com'));

  console.log(`   共 ${allCookies.length} 个 Cookie，其中 ${zhihuCookies.length} 个属于 zhihu.com`);

  // 检查关键 Cookie
  const keyCookies = ['z_c0', 'd_c0', '__zse_ck', '_xsrf'];
  for (const name of keyCookies) {
    const found = zhihuCookies.some(c => c.name === name);
    console.log(`   ${found ? '✅' : '❌'} ${name}`);
  }

  // 5. 保存
  const netscapeContent = toNetscapeFormat(allCookies);
  writeFileSync(OUTPUT_FILE, netscapeContent, 'utf-8');

  console.log(`\n✅ Cookie 已保存到: ${OUTPUT_FILE}`);
  console.log('   有效期约 6 个月，过期后需重新获取');
  console.log('');
  console.log('   现在可以关闭浏览器，使用 extract.mjs 抓取回答了');

  // 保持浏览器打开，让用户确认
  console.log('\n   按 Enter 关闭浏览器...');
  await new Promise(resolve => {
    process.stdin.once('data', resolve);
  });

  await browser.close();
}

main().catch(err => {
  console.error('\n❌ 执行出错:', err.message);
  process.exit(1);
});
