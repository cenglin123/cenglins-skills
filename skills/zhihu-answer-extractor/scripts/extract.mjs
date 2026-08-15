/**
 * 知乎问题回答批量抓取脚本
 *
 * 使用 puppeteer-extra + stealth 绕过知乎反爬检测
 *
 * 使用方法：
 *   1. 确保已安装依赖：npm install（在 scripts/ 目录下）
 *   2. 准备 Cookie 文件：www.zhihu.com_cookies.txt（Netscape 格式）
 *   3. 修改下方配置区参数
 *   4. 运行：node extract.mjs
 */

import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ═══════════════════════════════════════════════════════════════
// ▼▼▼ 配置区 - 只需修改以下参数 ▼▼▼
// ═══════════════════════════════════════════════════════════════

const QUESTION_URL = 'https://www.zhihu.com/question/XXXXXXXX';  // 目标问题 URL
const ANSWERS_NEEDED = 50;
const OUTPUT_FILE = '';  // 留空则自动生成到脚本同目录

// ═══════════════════════════════════════════════════════════════
// ▲▲▲ 配置区结束 ▲▲▲
// ═══════════════════════════════════════════════════════════════

const COOKIE_FILE = resolve(__dirname, 'www.zhihu.com_cookies.txt');

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
  throw new Error('未找到 Chrome，请在脚本中手动指定 CHROME_PATH');
}

const CHROME_PATH = findChrome();

// ── Cookie 解析（Netscape 格式）──────────────────────────────
function parseCookieFile(path) {
  if (!existsSync(path)) {
    throw new Error(`Cookie 文件不存在: ${path}\n请先导出知乎 Cookie 到此路径`);
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

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
  console.log('=== 知乎回答批量抓取 ===\n');

  // 1. 加载 Cookie
  const cookies = parseCookieFile(COOKIE_FILE);
  console.log(`[1/6] 加载 ${cookies.length} 个 Cookie`);

  // 2. 启动浏览器
  console.log(`[2/6] 启动 Chrome (headless)...`);
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: true,
    defaultViewport: { width: 1920, height: 1080 },
    args: ['--no-first-run', '--disable-blink-features=AutomationControlled', '--lang=zh-CN'],
  });

  const page = await browser.newPage();

  // 3. 注入 Cookie + 反检测
  await page.setCookie(...cookies);
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    window.chrome = { runtime: {}, loadTimes(){}, csi(){}, app: {} };
  });

  // 4. 导航到问题页
  console.log(`[3/6] 加载问题页面...`);
  const resp = await page.goto(QUESTION_URL, { waitUntil: 'networkidle2', timeout: 30000 });

  // 等待页面完全加载
  await new Promise(r => setTimeout(r, 2000));

  let title;
  try {
    title = await page.title();
  } catch (e) {
    title = await page.evaluate(() => document.title);
  }
  console.log(`      状态: ${resp.status()}, 标题: ${title}`);

  if (page.url().includes('signin') || title.includes('登录')) {
    console.error('\n❌ 被重定向到登录页，请检查 Cookie 是否有效');
    console.error('   1. 确认已登录知乎');
    console.error('   2. 用 Cookie 导出插件重新导出 Netscape 格式');
    console.error(`   3. 保存到: ${COOKIE_FILE}`);
    await browser.close();
    process.exit(1);
  }

  // 5. 提取问题元信息
  console.log('[4/6] 提取问题元信息...');

  // 先展开题干全文
  await page.evaluate(() => {
    const moreBtn = document.querySelector('.QuestionRichText-more') ||
                    document.querySelector('.QuestionRichText .ContentItem-more');
    if (moreBtn) moreBtn.click();
  });
  await new Promise(r => setTimeout(r, 500));

  const questionMeta = await page.evaluate(() => {
    // 题干（问题描述）
    const descEl = document.querySelector('.QuestionRichText');
    let description = '';
    if (descEl) {
      // 获取 itemprop="text" 的内容，或者直接获取 innerText
      const textEl = descEl.querySelector('[itemprop="text"]') || descEl;
      description = textEl.innerText
        .replace(/显示全部\s*$/g, '')
        .replace(/收起\s*$/g, '')
        .replace(/\s*​\s*$/g, '')  // 移除末尾的零宽空格
        .trim();
    }

    // 提问者（知乎不总是在 header 中显示，尝试多种方式）
    const authorEl = document.querySelector('.QuestionHeader .AuthorInfo .UserLink-link') ||
                     document.querySelector('.QuestionHeader .UserLink-link');
    const author = authorEl ? authorEl.textContent.trim() : '';

    // 话题标签
    const topicEls = document.querySelectorAll('.QuestionHeader .Tag a, .QuestionHeader .TopicLink');
    const topics = Array.from(topicEls).map(el => el.textContent.trim()).filter(Boolean);

    // 关注者 & 被浏览（按顺序：第一个是关注者，第二个是被浏览）
    const boardItems = document.querySelectorAll('.NumberBoard-item');
    let followers = '', views = '';
    if (boardItems[0]) {
      followers = boardItems[0].querySelector('.NumberBoard-itemValue')?.textContent?.trim() || '';
    }
    if (boardItems[1]) {
      views = boardItems[1].querySelector('.NumberBoard-itemValue')?.textContent?.trim() || '';
    }

    // 回答总数
    const countEl = document.querySelector('.List-headerText');
    const totalCount = countEl ? countEl.textContent.trim() : '';

    return { description, author, topics, followers, views, totalCount };
  });

  console.log(`      提问者: ${questionMeta.author || '(匿名/未显示)'}`);
  console.log(`      话题: ${questionMeta.topics.length ? questionMeta.topics.join(', ') : '(无)'}`);
  console.log(`      关注: ${questionMeta.followers || '?'}, 浏览: ${questionMeta.views || '?'}`);
  if (questionMeta.description) {
    console.log(`      题干: ${questionMeta.description.substring(0, 60)}...`);
  }

  // 6. 滚动加载回答
  console.log(`[5/6] 滚动加载 ${ANSWERS_NEEDED} 条回答...`);
  let lastCount = 0;
  let noNewCount = 0;

  while (true) {
    const currentCount = await page.evaluate(() =>
      document.querySelectorAll('.List-item .AnswerItem').length
    );

    if (currentCount >= ANSWERS_NEEDED) {
      console.log(`      已加载 ${currentCount} 条，达到目标`);
      break;
    }

    if (currentCount === lastCount) {
      noNewCount++;
      if (noNewCount >= 8) {
        console.log(`      连续 ${noNewCount} 次无新内容，停止于 ${currentCount} 条`);
        break;
      }
    } else {
      noNewCount = 0;
    }
    lastCount = currentCount;

    // 展开折叠的回答
    await page.evaluate(() => {
      document.querySelectorAll('button.QuestionRichText-more').forEach(b => b.click());
    });

    // 滚动
    await page.evaluate(() => window.scrollBy(0, 1500));
    await new Promise(r => setTimeout(r, 1200));
  }

  // 7. 提取回答数据
  console.log('[6/6] 提取并保存...');
  const answers = await page.evaluate((limit) => {
    const items = document.querySelectorAll('.List-item .AnswerItem');
    const results = [];
    for (let i = 0; i < Math.min(items.length, limit); i++) {
      const item = items[i];
      const authorEl = item.querySelector('.AnswerItem-authorInfo .UserLink-link');
      const author = authorEl ? authorEl.textContent.trim() : '匿名用户';
      const bio = item.querySelector('.AnswerItem-authorInfo .AuthorInfo-badge')?.textContent?.trim() || '';
      const voteEl = item.querySelector('button[aria-label*="赞同"]') ||
                     item.querySelector('.VoteButton--up');
      const votes = voteEl ? voteEl.textContent.replace(/[^0-9]/g, '') : '0';
      const contentEl = item.querySelector('.RichContent-inner .RichText');
      const content = contentEl ? contentEl.innerText.trim() : '[内容提取失败]';
      results.push({ index: i + 1, author, bio, votes, content });
    }
    return results;
  }, ANSWERS_NEEDED);

  // 8. 格式化并保存
  const cleanTitle = title.replace(' - 知乎', '').replace(/^\(\d+ 条消息\) /, '');
  let output = `知乎问题：${cleanTitle}\n`;
  output += `URL: ${QUESTION_URL}\n`;
  if (questionMeta.author) output += `提问者: ${questionMeta.author}\n`;
  if (questionMeta.topics.length) output += `话题: ${questionMeta.topics.join(', ')}\n`;
  if (questionMeta.followers) output += `关注者: ${questionMeta.followers}\n`;
  if (questionMeta.views) output += `被浏览: ${questionMeta.views}\n`;
  if (questionMeta.totalCount) output += `${questionMeta.totalCount}\n`;
  output += `抓取时间: ${new Date().toLocaleString('zh-CN')}\n`;
  output += `本次抓取: ${answers.length} 条回答\n`;
  output += `${'='.repeat(80)}\n`;

  // 题干（问题描述）
  if (questionMeta.description) {
    output += `\n【题干】\n${questionMeta.description}\n`;
    output += `${'─'.repeat(80)}\n`;
  }

  output += `\n`;

  for (const a of answers) {
    output += `【回答 #${a.index}】${a.author}`;
    if (a.bio) output += ` (${a.bio})`;
    output += `\n赞同: ${a.votes}\n\n`;
    output += a.content;
    output += `\n\n${'─'.repeat(80)}\n\n`;
  }

  const outPath = OUTPUT_FILE || resolve(__dirname, `知乎回答_${cleanTitle.slice(0, 20)}.txt`);
  writeFileSync(outPath, output, 'utf-8');

  console.log(`\n✅ 完成！`);
  console.log(`   问题: ${cleanTitle}`);
  console.log(`   回答: ${answers.length} 条`);
  console.log(`   大小: ${(Buffer.byteLength(output) / 1024).toFixed(1)} KB`);
  console.log(`   路径: ${outPath}`);

  await browser.close();
}

main().catch(err => {
  console.error('\n❌ 执行出错:', err.message);
  process.exit(1);
});
