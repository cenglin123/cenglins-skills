/**
 * 知乎浏览器打开模式（可视化，非 headless）
 *
 * 用途：手动浏览知乎页面（已注入 Cookie + 反检测）
 * 浏览器窗口会在脚本退出后保持打开（通过 browser.disconnect()）
 *
 * 使用方法：
 *   1. 修改下方 TARGET_URL
 *   2. 运行：node open.mjs
 *   3. 如需关闭浏览器，手动关闭窗口或在任务管理器结束 chrome.exe
 */

import { readFileSync, existsSync } from 'fs';
import { resolve, dirname, join } from 'path';
import { fileURLToPath } from 'url';
import { resolveCookieFile, loadPuppeteer, EXTERNAL_HOME, parseNetscapeCookieText, isZhihuDomain, stripCtrl } from './lib/env.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ═══════════════════════════════════════════════════════════════
// ▼▼▼ 配置区 - 修改目标 URL ▼▼▼
// ═══════════════════════════════════════════════════════════════

const TARGET_URL = 'https://www.zhihu.com/question/XXXXXXXX';  // 目标页面 URL

// ═══════════════════════════════════════════════════════════════
// ▲▲▲ 配置区结束 ▲▲▲
// ═══════════════════════════════════════════════════════════════

const COOKIE_FILE = resolveCookieFile();
const PROFILE_DIR = join(EXTERNAL_HOME, 'chrome-profile'); // 放 skill 之外，避免重装清空

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
    throw new Error(`Cookie 文件不存在: ${path}`);
  }
  return parseNetscapeCookieText(readFileSync(path, 'utf-8'))
    .filter((c) => isZhihuDomain(c.domain))
    .map((c) => ({ ...c, sameSite: 'Lax' }));
}

async function main() {
  const { puppeteer, StealthPlugin } = await loadPuppeteer();
  puppeteer.use(StealthPlugin());

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
console.log(`标题: ${stripCtrl(title)}`);

const content = await page.content();
console.log(content.includes('40362') ? '❌ 被拦截（40362）' : '✅ 加载成功');

browser.disconnect();
console.log('浏览器窗口保持打开，可手动操作');
}

main().catch((err) => {
  console.error('❌ ' + stripCtrl(err && err.message ? err.message : err));
  process.exit(1);
});
