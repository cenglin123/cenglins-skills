# 知乎回答批量抓取工具

批量抓取知乎问题下的回答，保存为结构化 txt 文档。

使用 puppeteer-extra + stealth 插件绕过知乎反爬检测（40362 错误码）。

## 快速开始

### 1. 安装依赖

```bash
cd scripts
npm install
```

### 2. 获取 Cookie

#### 方式一：一键获取（推荐）

```bash
node scripts/get-cookie.mjs
```

脚本会自动打开浏览器，你只需：
1. 在弹出的浏览器中登录知乎（扫码/验证码/密码均可）
2. 登录成功后脚本自动检测并导出 Cookie
3. Cookie 保存到 `scripts/www.zhihu.com_cookies.txt`

> ⚠️ **风险提示**：导出的 Cookie 包含你的知乎登录凭证（`z_c0`），任何获得此文件的人都可以以你的身份访问知乎。请妥善保管，不要分享给他人或上传到公共仓库。

#### 方式二：手动导出

1. 用 Chrome 浏览器登录 [zhihu.com](https://www.zhihu.com)
2. 安装 Cookie 导出扩展：
   - [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)（推荐）
   - [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
3. 在知乎页面点击扩展图标 → 导出 → 选择 **Netscape HTTP Cookie File** 格式
4. 保存为 `scripts/www.zhihu.com_cookies.txt`

### 3. 运行

```bash
node scripts/extract.mjs --url "https://www.zhihu.com/question/XXXXXXXX" --count 50 --output "回答.txt"
```

`--count` 默认为 50；省略 `--output` 时保存到脚本目录。`--max-wait` 可调整最长加载等待秒数，默认 180。

## 三种模式

### Cookie 获取（get-cookie.mjs）

打开浏览器，用户手动登录，自动导出 Cookie：

```bash
node scripts/get-cookie.mjs
```

### 抓取模式（extract.mjs）

headless 运行，速度快，输出 txt 文件：

```bash
node scripts/extract.mjs --url "https://www.zhihu.com/question/XXXXXXXX" --count 50
```

### 浏览模式（open.mjs）

打开可视化 Chrome 窗口，手动操作：

```bash
node scripts/open.mjs
```

修改 `open.mjs` 顶部的 `TARGET_URL` 即可。

## 输出示例

```
知乎问题：为什么体制内至今仍不鼓励用人工智能？
URL: https://www.zhihu.com/question/2042649810709239000
提问者: 张三
话题: 人工智能, 体制, 科技政策
关注者: 12,345
被浏览: 1,234,567
2,345 个回答
抓取时间: 2026/8/10 16:56:05
本次抓取: 50 条回答
目标数量: 50 条回答
停止原因: 达到目标数量 50
================================================================================

【题干】
问题描述正文内容...

────────────────────────────────────────────────────────────────────────────────

【回答 #1】张三 (某领域优秀答主)
赞同: 2520

正文内容...

────────────────────────────────────────────────────────────────────────────────

【回答 #2】李四
赞同: 640

正文内容...
```

## 分析工作流（双子代理独立总结）

当需要对抓取结果进行总结分析时，推荐使用**双子代理独立总结**模式，以避免单一视角的偏误：

1. **主代理**完成抓取，获得 txt 文件
2. **同时 spawn 2 个子代理**，各自独立读取同一份 txt 文件进行总结
3. 两个子代理使用相同的 prompt，不共享分析过程
4. **主代理等待两个子代理返回**后，对比两份报告：
   - 提取共识点（两个子代理都识别出的观点/趋势）
   - 标注分歧点（仅一方识别的内容）
   - 合并去重，形成最终结论

这种方法的优势：
- 单一代理可能因初始倾向忽略少数派观点
- 两个独立分析的共识更可靠
- 分歧点本身也是有价值的发现（说明社区存在争议）

详见 `SKILL.md` 中的子代理 Prompt 模板。

## 原理

### 为什么不用浏览器自动化工具（Selenium/Playwright）？

知乎检测到 WebDriver 标志会返回 40362 错误。本工具使用：

- **puppeteer-extra**：Puppeteer 的增强版，支持插件系统
- **stealth 插件**：自动隐藏 20+ 个自动化检测点（WebDriver、navigator.plugins、语言列表等）
- **evaluateOnNewDocument**：在每个页面加载前注入反检测代码
- **启动参数**：`--disable-blink-features=AutomationControlled` 禁用 Blink 引擎的自动化检测

### Cookie 获取原理

`get-cookie.mjs` 通过 Chrome DevTools Protocol (CDP) 直接从浏览器提取 Cookie：

1. 使用 `Network.getAllCookies` 命令获取浏览器中所有 Cookie
2. 过滤出 `zhihu.com` 相关的 Cookie
3. 转换为 Netscape HTTP Cookie File 格式保存

这比手动安装扩展导出更方便，且不需要额外的浏览器扩展。

### 滚动加载机制

知乎回答采用无限滚动加载。脚本通过以下方式确保加载足够数量：

1. 优先定位包含回答列表的独立滚动容器，同时用页面滚动兜底
2. 定位最后一条回答并继续下移，触发懒加载
3. 自动展开折叠正文并点击“加载更多/查看更多回答”等按钮
4. 使用多组回答选择器，并持续对照已加载数量和页面报告总数
5. 达到目标数量、确认无更多内容，或在滚动末端持续无变化/超过最长等待时间后停止
6. 在控制台和输出文件中记录停止原因，数量不足时给出警告

## 常见问题

### Q: 返回 40362 错误

Cookie 过期或被检测。解决：
1. 运行 `node scripts/get-cookie.mjs` 重新获取 Cookie
2. 或手动重新导出 Cookie

### Q: 重定向到登录页

Cookie 无效。解决：
- 运行 `node scripts/get-cookie.mjs` 重新获取 Cookie

### Q: 回答数量不够

正常现象。可能原因：
- 该问题回答数本身就不足目标数量
- 部分回答被折叠且无法展开
- 网络或知乎接口响应超过 `--max-wait`
- 知乎页面结构发生变化，备用选择器也未匹配

先查看输出文件中的“停止原因”和终端显示的实际抓取数量。若是超时，可增大 `--max-wait` 后重试；若页面显示的回答数明显更多但仍提前结束，应保留日志并更新加载按钮或回答选择器。

### Q: 窗口模式下浏览器关闭了

进程退出时 Chrome 子进程被回收。解决：
```powershell
# Windows：用 Start-Process 后台启动
Start-Process -FilePath "node" -ArgumentList "scripts\open.mjs" -WindowStyle Normal
```

## Cookie 有效期

- `z_c0`（登录凭证）：约 6 个月
- `d_c0`（设备标识）：约 1 年
- 其他 cookie：多数为会话级或短期

如果突然无法抓取，优先检查 `z_c0` 是否过期。运行 `get-cookie.mjs` 可重新获取。

## 文件结构

```
zhihu-answer-extractor/
├── SKILL.md                    # Agent 技能定义
├── README.md                   # 本文档
└── scripts/
    ├── package.json            # npm 依赖声明
    ├── get-cookie.mjs          # Cookie 一键获取脚本
    ├── extract.mjs             # 批量抓取脚本（headless）
    ├── open.mjs                # 浏览器打开模式（可视化）
    └── www.zhihu.com_cookies.txt  # Cookie 文件（由 get-cookie.mjs 生成）
```

## 环境要求

- Node.js 18+
- Chrome 浏览器（脚本会自动检测路径）
- Windows / macOS / Linux

## 许可

MIT
