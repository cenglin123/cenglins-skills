/**
 * 知乎问题回答批量抓取脚本
 *
 * 使用 puppeteer-extra + stealth 绕过知乎反爬检测
 *
 * 回答加载机制（2026-09 实测）：
 *   - 首屏静态渲染前几条回答，更多回答通过「查看剩余 N 条回答」按钮分页加载
 *   - 该按钮常是普通 div + cursor:pointer（React pointer 事件驱动），
 *     页内 el.click() 合成事件不可靠，必须在 Node 侧用 elementHandle.click()
 *     发送真实 CDP 鼠标事件（isTrusted）
 *   - 点击展开后可能切换为无限滚动，故保留滚动作为兜底
 *
 * 使用方法：
 *   1. 确保已安装依赖：npm install（在 scripts/ 目录下）
 *   2. 准备 Cookie 文件：www.zhihu.com_cookies.txt（Netscape 格式）
 *   3. 运行：node extract.mjs --url <URL> --count 50 --output <FILE>
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const DEFAULT_COUNT = 50;
const DEFAULT_MAX_WAIT_SECONDS = 180;
const ANSWER_SELECTORS = [
  '.List-item .AnswerItem',
  '.AnswerItem',
  '[data-za-detail-view-path-module="AnswerItem"]',
  '.List-item [itemprop="answer"]',
  'article[itemprop="answer"]',
];

// 「查看剩余 N 条回答」等加载按钮的完整文本匹配
// 注意：必须是 ^...$ 全匹配——外层容器 innerText 可能包含标签外的其他内容
const LOAD_MORE_PATTERN = /^(?:查看剩余\s*\d+\s*条回答|加载更多(?:回答)?|查看更多(?:回答)?|查看全部回答|更多回答|展开更多回答|下一页)(?:\s*[（(]?\d+[）)]?)?$/;

function printHelp() {
  console.log(`用法：
  node extract.mjs --url <知乎问题 URL> [--count 50] [--output <文件>] [--max-wait 180]

参数：
  --url       必填，知乎问题 URL
  --count     目标回答数量，默认 ${DEFAULT_COUNT}
  --output    输出 txt 路径；省略时保存到脚本目录
  --max-wait  最长加载等待秒数，默认 ${DEFAULT_MAX_WAIT_SECONDS}
  --help      显示帮助`);
}

function parseCliArgs(argv) {
  const values = new Map();
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--help' || token === '-h') return { help: true };
    if (!token.startsWith('--')) throw new Error(`未知参数: ${token}`);

    const equalAt = token.indexOf('=');
    const key = equalAt >= 0 ? token.slice(2, equalAt) : token.slice(2);
    const value = equalAt >= 0 ? token.slice(equalAt + 1) : argv[++i];
    if (!['url', 'count', 'output', 'max-wait'].includes(key)) {
      throw new Error(`未知参数: --${key}`);
    }
    if (!value || value.startsWith('--')) throw new Error(`参数 --${key} 缺少值`);
    values.set(key, value);
  }

  const questionUrl = values.get('url');
  if (!questionUrl) throw new Error('缺少必填参数 --url');

  let parsedUrl;
  try {
    parsedUrl = new URL(questionUrl);
  } catch {
    throw new Error(`无效 URL: ${questionUrl}`);
  }
  if (!/(^|\.)zhihu\.com$/i.test(parsedUrl.hostname) || !/^\/question\/\d+/.test(parsedUrl.pathname)) {
    throw new Error('--url 必须是 https://www.zhihu.com/question/<数字> 格式的问题链接');
  }

  const answersNeeded = Number(values.get('count') || DEFAULT_COUNT);
  if (!Number.isSafeInteger(answersNeeded) || answersNeeded < 1) {
    throw new Error('--count 必须是大于 0 的整数');
  }

  const maxWaitSeconds = Number(values.get('max-wait') || DEFAULT_MAX_WAIT_SECONDS);
  if (!Number.isFinite(maxWaitSeconds) || maxWaitSeconds < 10) {
    throw new Error('--max-wait 必须是不小于 10 的秒数');
  }

  return {
    help: false,
    questionUrl: parsedUrl.href,
    answersNeeded,
    outputFile: values.has('output') ? resolve(process.cwd(), values.get('output')) : '',
    maxWaitMs: maxWaitSeconds * 1000,
  };
}

function parseReportedAnswerCount(text) {
  if (!text) return null;
  const match = text.replace(/,/g, '').match(/([\d.]+)\s*(万)?\s*(?:个|条)?回答/);
  if (!match) return null;
  const value = Number(match[1]) * (match[2] ? 10000 : 1);
  return Number.isFinite(value) ? Math.floor(value) : null;
}

let options;
try {
  options = parseCliArgs(process.argv.slice(2));
} catch (error) {
  console.error(`❌ ${error.message}\n`);
  printHelp();
  process.exit(2);
}
if (options.help) {
  printHelp();
  process.exit(0);
}

const {
  questionUrl: QUESTION_URL,
  answersNeeded: ANSWERS_NEEDED,
  outputFile: OUTPUT_FILE,
  maxWaitMs: MAX_WAIT_MS,
} = options;

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

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── 页内辅助（在 page.evaluate 中执行）───────────────────────

// 统计当前已加载的回答数
async function countAnswers(page) {
  return page.evaluate(selectors => {
    const rawItems = Array.from(document.querySelectorAll(selectors.join(',')));
    const items = rawItems.map(item =>
      item.matches('.AnswerItem') ? item :
        item.querySelector('.AnswerItem') || item.closest('.AnswerItem') || item,
    );
    return new Set(items).size;
  }, ANSWER_SELECTORS);
}

// 关闭可能遮挡点击的弹窗（登录墙除外——那是要上报的状态）
async function dismissOverlays(page) {
  await page.evaluate(() => {
    document.querySelectorAll(
      '[class*="Modal-closeButton"], button[aria-label="关闭"], .Modal-closeButton',
    ).forEach(btn => {
      try { btn.click(); } catch { /* ignore */ }
    });
  });
}

// 探测「查看剩余 N 条回答」等加载按钮，返回 ElementHandle 或 null。
// 关键点：知乎的加载按钮常是 div + cursor:pointer（React pointer 事件），
// 返回 handle 后必须在 Node 侧 click() 走真实 CDP 鼠标事件。
async function findLoadMoreHandle(page) {
  const handle = await page.evaluateHandle(patternSource => {
    const pattern = new RegExp(patternSource);
    const isVisible = element => {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return rect.width > 0 && rect.height > 0 &&
        style.visibility !== 'hidden' && style.display !== 'none';
    };
    const matches = [];
    for (const el of document.querySelectorAll('button, [role="button"], a, div, span')) {
      if (el.children.length > 4) continue;
      const text = (el.innerText || el.textContent || '').trim();
      if (!pattern.test(text)) continue;
      if (!isVisible(el)) continue;
      // 取最小匹配（最内层），避免点到外层容器
      if (matches.some(prev => prev.contains(el))) continue;
      matches.push(el);
    }
    return matches.length ? matches[matches.length - 1] : null;
  }, LOAD_MORE_PATTERN.source);
  return handle.asElement();
}

// 检测是否出现登录墙（Cookie 失效时点击加载会触发）
async function hasLoginWall(page) {
  return page.evaluate(() => {
    const modal = document.querySelector('[class*="Modal-wrapper"], [class*="signFlowModal"], [id*="Modal"]');
    if (!modal) return false;
    return /登录|扫码|验证码/.test(modal.innerText || '');
  });
}

// 检测页面明确的“无更多内容”提示
async function detectExplicitEnd(page) {
  return page.evaluate(() => {
    const nodes = document.querySelectorAll('.List-end, .List-footer, [class*="List-end"]');
    for (const node of nodes) {
      const text = (node.innerText || '').trim();
      const hit = text.match(/(没有更多|已显示全部|没有更多内容|到底了)/);
      if (hit) return hit[0];
    }
    return '';
  });
}

// 滚动兜底：优先独立滚动容器，否则滚窗口
async function scrollToBottom(page) {
  return page.evaluate(selectors => {
    const rawItems = Array.from(document.querySelectorAll(selectors.join(',')));
    const answerItems = [...new Set(rawItems.map(item =>
      item.matches('.AnswerItem') ? item :
        item.querySelector('.AnswerItem') || item.closest('.AnswerItem') || item,
    ))];
    const lastAnswer = answerItems.at(-1) || null;

    // 展开已加载回答的折叠正文
    document.querySelectorAll(
      'button.QuestionRichText-more, button.ContentItem-more, .RichContent button[class*="more"]',
    ).forEach(button => {
      if (!button.disabled) button.click();
    });

    // 找独立滚动容器
    const candidates = [];
    let ancestor = lastAnswer?.parentElement || null;
    while (ancestor && ancestor !== document.body) {
      candidates.push(ancestor);
      ancestor = ancestor.parentElement;
    }
    for (const selector of [
      '[data-za-detail-view-path-module="QuestionAnswers"]',
      '.Question-mainColumn',
      '.List',
      '[role="main"]',
      'main',
    ]) {
      document.querySelectorAll(selector).forEach(el => candidates.push(el));
    }

    let scrollContainer = null;
    let bestScore = 0;
    for (const candidate of new Set(candidates)) {
      const style = window.getComputedStyle(candidate);
      const range = candidate.scrollHeight - candidate.clientHeight;
      const containsLast = lastAnswer ? candidate.contains(lastAnswer) : false;
      const score = range + (containsLast ? 1_000_000_000 : 0);
      if (/(auto|scroll|overlay)/.test(style.overflowY) && range > 20 && score > bestScore) {
        scrollContainer = candidate;
        bestScore = score;
      }
    }

    if (scrollContainer) {
      lastAnswer?.scrollIntoView({ block: 'end', behavior: 'instant' });
      scrollContainer.scrollTop = Math.min(
        scrollContainer.scrollTop + Math.max(1500, scrollContainer.clientHeight * 0.9),
        scrollContainer.scrollHeight,
      );
      return { mode: 'container', atEnd: scrollContainer.scrollTop + scrollContainer.clientHeight >= scrollContainer.scrollHeight - 10 };
    }
    lastAnswer?.scrollIntoView({ block: 'end', behavior: 'instant' });
    window.scrollBy(0, Math.max(1500, window.innerHeight));
    const root = document.scrollingElement || document.documentElement;
    return { mode: 'window', atEnd: root.scrollTop + root.clientHeight >= root.scrollHeight - 10 };
  }, ANSWER_SELECTORS);
}

// ── 主流程 ───────────────────────────────────────────────────
async function main() {
  console.log('=== 知乎回答批量抓取 ===\n');

  const [{ default: puppeteer }, { default: StealthPlugin }] = await Promise.all([
    import('puppeteer-extra'),
    import('puppeteer-extra-plugin-stealth'),
  ]);
  puppeteer.use(StealthPlugin());
  const chromePath = findChrome();

  // 1. 加载 Cookie
  const cookies = parseCookieFile(COOKIE_FILE);
  console.log(`[1/6] 加载 ${cookies.length} 个 Cookie`);

  // 2. 启动浏览器
  console.log(`[2/6] 启动 Chrome (headless)...`);
  const browser = await puppeteer.launch({
    executablePath: chromePath,
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
  await sleep(2000);

  let title;
  try {
    title = await page.title();
  } catch {
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
  await sleep(500);

  const questionMeta = await page.evaluate(() => {
    // 题干（问题描述）
    const descEl = document.querySelector('.QuestionRichText');
    let description = '';
    if (descEl) {
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
    const countEl = document.querySelector('.List-headerText') ||
                    document.querySelector('.QuestionAnswers-answerCount') ||
                    document.querySelector('[data-za-detail-view-path-module="QuestionAnswers"] h4') ||
                    document.querySelector('[class*="List-header"] h4');
    const totalCount = countEl ? countEl.textContent.trim() : '';

    return { description, author, topics, followers, views, totalCount };
  });

  console.log(`      提问者: ${questionMeta.author || '(匿名/未显示)'}`);
  console.log(`      话题: ${questionMeta.topics.length ? questionMeta.topics.join(', ') : '(无)'}`);
  console.log(`      关注: ${questionMeta.followers || '?'}, 浏览: ${questionMeta.views || '?'}`);
  if (questionMeta.description) {
    console.log(`      题干: ${questionMeta.description.substring(0, 60)}...`);
  }

  const reportedTotal = parseReportedAnswerCount(questionMeta.totalCount);
  if (reportedTotal !== null) {
    console.log(`      页面报告回答总数: ${reportedTotal}`);
  }

  // 6. 循环加载回答：真实点击「查看剩余」按钮为主，滚动兜底
  console.log(`[5/6] 加载回答（目标 ${ANSWERS_NEEDED} 条）...`);
  const loadingStartedAt = Date.now();
  let lastProgressAt = loadingStartedAt;
  let lastCount = -1;
  let noNewCount = 0;
  let stopReason = 'unknown';

  while (true) {
    const currentCount = await countAnswers(page);

    if (currentCount > lastCount && lastCount >= 0) {
      console.log(`      已加载 ${currentCount} 条`);
    }
    if (currentCount > lastCount) {
      lastProgressAt = Date.now();
      noNewCount = 0;
    } else {
      noNewCount++;
    }
    lastCount = currentCount;

    if (currentCount >= ANSWERS_NEEDED) {
      stopReason = `达到目标数量 ${ANSWERS_NEEDED}`;
      break;
    }
    if (reportedTotal !== null && currentCount >= reportedTotal) {
      stopReason = `已加载页面报告的全部 ${reportedTotal} 条回答`;
      break;
    }

    await dismissOverlays(page);

    // 主路径：找到加载按钮 → 真实点击
    const loadMoreEl = await findLoadMoreHandle(page);
    let action = 'none';
    if (loadMoreEl) {
      try {
        await page.evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }), loadMoreEl);
        await sleep(300);
        await loadMoreEl.click();  // 真实 CDP 鼠标事件（isTrusted）
        action = 'click';
      } catch (err) {
        console.log(`      ⚠️ 点击加载按钮失败: ${err.message.split('\n')[0]}，改用滚动`);
      } finally {
        await loadMoreEl.dispose();
      }
    }

    // 兜底：滚动（点击展开后知乎可能切换为无限滚动，每轮都滚一点无害）
    const scrollState = await scrollToBottom(page);
    if (action === 'none') action = `scroll:${scrollState.mode}`;

    if (action === 'click') {
      console.log(`      点击「查看剩余」加载按钮`);
    } else if (noNewCount > 0 && noNewCount % 5 === 0) {
      console.log(`      等待新回答：连续 ${noNewCount} 轮无变化（方式=${action}，末端=${scrollState.atEnd ? '是' : '否'}）`);
    }

    // 登录墙检测：点击加载后弹登录框说明 Cookie 失效
    if (noNewCount >= 2 && await hasLoginWall(page)) {
      stopReason = '触发登录墙（Cookie 可能失效），无法继续加载';
      break;
    }

    const explicitEnd = await detectExplicitEnd(page);
    if (explicitEnd) {
      stopReason = `页面明确提示无更多内容：${explicitEnd}`;
      break;
    }

    const now = Date.now();
    if (now - loadingStartedAt >= MAX_WAIT_MS) {
      stopReason = `达到最长等待时间 ${Math.round(MAX_WAIT_MS / 1000)} 秒`;
      break;
    }

    // 无加载按钮 + 滚动末端 + 持续无新增 → 判定到头
    if (
      noNewCount >= 10 &&
      now - lastProgressAt >= 20000 &&
      scrollState.atEnd
    ) {
      stopReason = `滚动末端且连续 ${noNewCount} 轮、至少 20 秒无新增回答（页面可能仍有未展开的回答）`;
      break;
    }

    await sleep(action === 'click' ? 2500 : 1200);
  }

  console.log(`      停止原因: ${stopReason}`);

  // 7. 提取回答数据
  console.log('[6/6] 提取并保存...');
  const answers = await page.evaluate((limit, selectors) => {
    // 「赞同 1.6 万」→ 16000；优先 aria-label，其次按钮文本
    const parseVotes = el => {
      if (!el) return 0;
      const raw = (el.getAttribute('aria-label') || el.textContent || '').trim();
      const wan = raw.match(/([\d.]+)\s*万/);
      if (wan) return Math.round(parseFloat(wan[1]) * 10000);
      const digits = raw.replace(/[^\d]/g, '');
      return digits ? Number(digits) : 0;
    };
    const rawItems = Array.from(document.querySelectorAll(selectors.join(',')));
    const items = [...new Set(rawItems.map(item =>
      item.matches('.AnswerItem') ? item :
        item.querySelector('.AnswerItem') || item.closest('.AnswerItem') || item,
    ))];
    const results = [];
    for (let i = 0; i < Math.min(items.length, limit); i++) {
      const item = items[i];
      const authorEl = item.querySelector('.AnswerItem-authorInfo .UserLink-link') ||
                       item.querySelector('.AuthorInfo-name') ||
                       item.querySelector('[itemprop="author"] [itemprop="name"]');
      const author = authorEl ? authorEl.textContent.trim() : '匿名用户';
      const bio = item.querySelector('.AnswerItem-authorInfo .AuthorInfo-badge')?.textContent?.trim() ||
                  item.querySelector('.AuthorInfo-badge')?.textContent?.trim() || '';
      const voteEl = item.querySelector('button[aria-label*="赞同"]') ||
                     item.querySelector('.VoteButton--up') ||
                     item.querySelector('[class*="VoteButton"]');
      const contentEl = item.querySelector('.RichContent-inner .RichText') ||
                        item.querySelector('.RichContent-inner') ||
                        item.querySelector('[itemprop="text"]');
      const content = contentEl ? contentEl.innerText.trim() : '[内容提取失败]';
      results.push({ index: i + 1, author, bio, votes: parseVotes(voteEl), content });
    }
    return results;
  }, ANSWERS_NEEDED, ANSWER_SELECTORS);

  // 8. 格式化并保存
  const cleanTitle = title.replace(' - 知乎', '').replace(/^\([^)]*(?:条消息|封私信)[^)]*\)\s*/, '');
  let output = `知乎问题：${cleanTitle}\n`;
  output += `URL: ${QUESTION_URL}\n`;
  if (questionMeta.author) output += `提问者: ${questionMeta.author}\n`;
  if (questionMeta.topics.length) output += `话题: ${questionMeta.topics.join(', ')}\n`;
  if (questionMeta.followers) output += `关注者: ${questionMeta.followers}\n`;
  if (questionMeta.views) output += `被浏览: ${questionMeta.views}\n`;
  if (questionMeta.totalCount) output += `${questionMeta.totalCount}\n`;
  output += `抓取时间: ${new Date().toLocaleString('zh-CN')}\n`;
  output += `本次抓取: ${answers.length} 条回答\n`;
  output += `目标数量: ${ANSWERS_NEEDED} 条回答\n`;
  output += `停止原因: ${stopReason}\n`;
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
  if (answers.length < ANSWERS_NEEDED) {
    console.warn(`   ⚠️ 实际抓取 ${answers.length}/${ANSWERS_NEEDED} 条；${stopReason}`);
  }

  await browser.close();
}

main().catch(err => {
  console.error('\n❌ 执行出错:', err.message);
  process.exit(1);
});
