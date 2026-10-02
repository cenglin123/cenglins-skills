# 渲染收割配方：让 Foxmail 自己解密附件

## 原理

Foxmail 7.2 的本地邮件文件（`Mails/`）为加密存储，但**无需解密**——
Foxmail 每次渲染（打开/预览）邮件时会自行解密并把产物写到临时缓存：

```
%APPDATA%\Roaming\Foxmail7\Temp-<pid>-<yyyymmddhhmmss>\Attach\
├── fox(<时间戳>).html        # 渲染出的邮件正文
└── <附件原名>                 # 解密后的附件明文（doc/xls/pdf/jpg 原样落盘）
```

`foxgrep.py extract` 已内置该缓存的被动收割（`_temp_cache_find`）：附件被渲染过一次即可直接提取。
本配方处理的是**缓存未命中**的情况：附件从未被渲染过，需要主动触发一次渲染。

## 配方步骤（agent 用 cua-driver 执行，全程可验证）

1. **确认 Foxmail 在运行**：`list_apps` 找 Foxmail；没在运行就 `launch_app` 启动，等主窗口出现。
   （Foxmail 窗口位置可能记在已断开的副屏坐标上导致"消失"：用 `set_window_frame` 拉回主屏。）
2. **记录基线**：列出 `Roaming\Foxmail7\Temp-*\Attach\` 现有文件（或直接记住当前最新 Temp 目录）。
3. **定位邮件**：在 Foxmail 主窗口用 UIA 找搜索框（UIA 树里有 MenuItem「收取」「写邮件」的工具栏即为正确窗口），
   输入目标邮件主题并回车；结果列表出现后**单击**该邮件。
   - 单击选中 = 触发渲染 = 解密落缓存。不需要双击打开新窗口。
   - 只需 2~3 个 GUI 动作，不要做任何多余操作（不拖窗口、不改设置、不点菜单）。
4. **等待收割**：轮询当前 `Temp-<pid>-*\Attach\`（最多 ~10 秒），出现 `fox(*).html` 新文件即渲染完成；
   附件文件随之落盘。
5. **重试提取**：`fg.cmd --live extract <编号>` —— 这次缓存收割会命中。
6. **验证**：检查提取出的文件魔数（xls=`D0CF11E0`、xlsx/docx=`PK`、pdf=`%PDF`）。

## 纪律

- 只执行配方的最小动作集；配方外的窗口操作可能破坏 Foxmail 运行状态。
- 若 Foxmail 异常退出：直接重启 `Foxmail.exe` 即可（本工具全程只读， Foxmail 数据无损）。
- 缓存目录按 Foxmail 进程生命周期创建（`Temp-<pid>-<启动时间戳>`），Foxmail 重启后注意换目录。
- 缓存文件 Foxmail 退出后仍保留——被动收割随时可用。
- 该缓存是**本机明文**，收割后按邮件隐私同等对待：不外发、不入分发包。
