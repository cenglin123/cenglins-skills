/**
 * B 站浏览器打开模式（可视化，非 headless）
 *
 * 用途：手动浏览 B 站页面（已注入 Cookie + 反检测），
 *       例如人工核对评论区内容、检查视频是否下架等。
 * 浏览器窗口会在脚本退出后保持打开（通过 browser.disconnect()）
 *
 * 使用方法：
 *   1. node open.mjs <URL>   （或修改下方 TARGET_URL 默认值后运行 node open.mjs）
 *   2. 如需关闭浏览器，手动关闭窗口或在任务管理器结束 chrome.exe
 */

import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { readFileSync, existsSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { homedir } from 'os';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ═══════════════════════════════════════════════════════════════
// ▼▼▼ 配置区 - 修改目标 URL ▼▼▼
// ═══════════════════════════════════════════════════════════════

// 默认目标页面；也可直接用命令行参数覆盖：node open.mjs <URL>
const TARGET_URL = process.argv[2] || 'https://www.bilibili.com/video/BV1Moth6CEi9';

// ═══════════════════════════════════════════════════════════════
// ▲▲▲ 配置区结束 ▲▲▲
// ═══════════════════════════════════════════════════════════════

const DATA_DIR = resolve(homedir(), '.bilibili-comment-extractor');
const COOKIE_FILE = resolve(DATA_DIR, 'www.bilibili.com_cookies.txt');
const PROFILE_DIR = resolve(DATA_DIR, '.chrome-profile');
mkdirSync(DATA_DIR, { recursive: true });

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

function parseCookieFile(path) {
  if (!existsSync(path)) {
    throw new Error(`Cookie 文件不存在: ${path}\n请先运行 get-cookie.mjs 获取`);
  }
  const text = readFileSync(path, 'utf-8');
  const cookies = [];
  for (const line of text.split('\n')) {
    if (line.startsWith('#') || line.trim() === '') continue;
    const parts = line.split('\t');
    if (parts.length < 7) continue;
    const [domain, , path, secure, expires, name, ...valueParts] = parts;
    cookies.push({
      name, value: valueParts.join('\t'),
      domain: domain.startsWith('.') ? domain : '.' + domain,
      path, secure: secure === 'TRUE', httpOnly: false,
      expires: expires === '0' ? -1 : parseInt(expires),
      sameSite: 'Lax',
    });
  }
  return cookies;
}

const cookies = parseCookieFile(COOKIE_FILE);
console.log(`加载 ${cookies.length} 个 Cookie`);

const browser = await puppeteer.launch({
  executablePath: findChrome(),
  headless: false,
  defaultViewport: null,
  userDataDir: PROFILE_DIR,
  args: ['--no-first-run', '--disable-blink-features=AutomationControlled', '--lang=zh-CN', '--window-size=1920,1080'],
});

const page = await browser.newPage();
await page.setCookie(...cookies);
await page.evaluateOnNewDocument(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => false });
  window.chrome = { runtime: {}, loadTimes(){}, csi(){}, app: {} };
});

console.log('正在加载页面...');
const resp = await page.goto(TARGET_URL, { waitUntil: 'networkidle2', timeout: 30000 });

// 等待页面完全加载
await new Promise(r => setTimeout(r, 2000));

console.log(`状态: ${resp.status()}`);
let title;
try {
  title = await page.title();
} catch (e) {
  title = await page.evaluate(() => document.title);
}
console.log(`标题: ${title}`);
console.log(resp.ok() ? '✅ 加载成功' : '❌ 加载异常');

browser.disconnect();
console.log('浏览器窗口保持打开，可手动操作');
