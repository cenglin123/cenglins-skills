# feedback/ — 版本适配反馈产出目录

本目录是**下游分发机**向上游回馈研究成果的约定位置。

## 何时使用

当 foxgrep 在本机的 Foxmail 版本上无法使用（目录结构不认识、解析大面积失败、
提取结果为空等），按 `refs/research-methodology.md` 完成版本勘察后，
把产出物放在本目录下，随后整目录交给分发来源（上游维护者）。

## 目录约定

每个研究任务建一个子目录，命名：`YYYYMMDD-<foxmail版本号>-<简述>`，例如：

```
feedback/
└── 20260924-7.0.1-Storage结构初探/
    ├── report.md            # 研究报告（按 refs/research-report-template.md 填写）
    ├── foxgrep.py.diff      # 针对 foxgrep.py 的 unified diff（如有代码修正）
    └── appendix/            # 可选：hexdump、测试输出原文等证据
        └── ...
```

## 硬性要求

- 报告与附录中的路径、账号、服务器地址一律占位化（`<foxmail-root>`、`<account>` 等）
- **任何产出物不得包含真实邮件内容、用户密码**；邮件样本只引用主题/结构，不复制原文
- diff 必须能保持 foxgrep.py 单文件、纯标准库、只读特性，且不破坏已有模式
  （corpus / --live / --v6 回归通过）

## 上游处理

上游收到反馈目录后：评审报告与 diff → 合并进 foxgrep.py / refs 格式档案 →
归档至上游 skill 目录的 `_archive/feedback/` 留存。
