# 模型默认池与分工 — 详细参考

> 本表从 SKILL.md §八 下沉（数据层，随模型换代更新，对标 converge refs/model-tiers.md）。主文件保留选模型规则、遥测与翻转门槛。
>
> OCSR 可选模型严格限于 `deepseek/deepseek-v4-flash`、`deepseek/deepseek-v4-pro`、`xiaomi/mimo-v2.5`、`xiaomi/mimo-v2.5-pro` 四个 qualified ID。`xiaomi/mimo-v2.5-pro-ultraspeed` 及任何未列出的模型不可选。

按**角色**分工，不按任务表面难度：

| 角色 | 默认模型 | cost input/output ($/M tok) | 说明 |
|------|---------|---|---|
| 批量执行 worker（机械杂活：转换、摘要、矫正、抽取） | `deepseek/deepseek-v4-flash` | 0.14 / 0.28 | 便宜、快，配合 §四/§五 的约束与验证使用 |
| 轻量判断（外部文档摘要/非关键 evd 生成） | `xiaomi/mimo-v2.5` | 0.14 / 0.28 | MiMo 轻量档 |
| 判断密集角色（评审、verdict、语义审查、meta 判断） | `xiaomi/mimo-v2.5-pro` **或** `deepseek/deepseek-v4-pro`（并列共同默认，异构评议时错开 family） | 0.435 / 0.87（MiMo Pro）；以 `opencode models --verbose` 为准 | MiMo 完整 ID：`xiaomi/mimo-v2.5`（轻量判断）/ `xiaomi/mimo-v2.5-pro`（默认）；deepseek-v4-pro 为 thinking 系，盲审实证质量扎实。传给 `-m` 时用完整 ID |
