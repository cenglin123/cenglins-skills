# 模式 D · 释放/收口执行型（release / closeout executor）— 详细参考

> 本文件从 SKILL.md §六 下沉。主文件保留四种驱动模式的概览与模式 D 的入口指针；本文件承载 release executor 的完整职责、输入合同、输出 schema 与分工。

### 模式 D · 释放/收口执行型（release / closeout executor）

适合：功能实现或收敛循环产出可发布终态后的文档、归档、清理、staging 收尾——把这类默认可结构化的收尾工作从昂贵主 orchestrator 转移到便宜执行档模型（对应上层薄编排/converge 类 plan 中的 "release executor" 角色）。

**规模边界**：模式 D 面向批量、长收尾、成本敏感的收口工作；单文件/几行的小规模任务（含小修复、单行 CHANGELOG 更新）不走模式 D——用框架原生 executor（判据见 §一「通道选择判据」）。

**输入合同**（§四自足 prompt 模板的六要素之上，release executor 额外需要以下残差）：

- 最终 diff 或 `changed_files` 清单
- 测试摘要（命令 + exit_code + 证据路径）
- 任务合同（验收标准 / 允许路径）
- 归档状态（当前 active/done 位置、Archive Contract 是否已 `valid-v1`）
- `allowed_paths`（本任务允许改动的路径前缀清单——release executor 产出的 manifest 中任何路径都必须落在此范围内，越界即被下方门禁拒绝）

**默认职责**：

1. 在 manifest 中以 `doc_updates` 字段**提议**对 CURRENT / CHANGELOG / bugfix 类文档的描述性变更（如标题、summary 语句）；release executor **不直接修改**产品文件或文档，只产出 manifest；
2. 生成收敛 retrospective 并记入 `retrospective_path`；
3. 检查报告中的数字（如"通过数"）与实际测试结果（exit code / 计数）是否一致，记入 `digit_consistency_check`；
4. 生成精确 cleanup manifest（拟删除的临时/中间产物，逐条给出路径 + reason）；
5. 生成 staging manifest（拟提交/归档的最终产物，逐条给出路径 + reason）；
6. 执行 Archive Contract 预检（只读，见下方分工）；
7. 报告受保护路径和未清理项。

**输出**：一份 JSON manifest，Write 直写到指定路径，字段包含 `status` / `allowed_paths` / `doc_updates` / `retrospective_path` / `digit_consistency_check` / `cleanup_manifest` / `staging_manifest` / `archive_precheck` / `protected_paths_reported` / `unclean_items_reported`。`doc_updates` 及类似字段只描述拟执行变更；release executor 不自行执行删除、修改或归档操作。

**与上层驱动器的分工**（本 skill 不重复实现机械校验；权威源另在宿主侧）：

- manifest 的 JSON schema，以及"凭据模式 / `allowed_paths` 越界 / 外部临时目录 / 删除项存在性与保护路径"四项最少机械检查，由调用方（上层 orchestrator 或其适配层驱动器）执行并 fail closed——本 skill 只负责让 release executor 产出符合该 schema 的 manifest，不在 `SKILL.md` 内维护第二套校验规则。
- release executor **只生成** manifest，**不自行执行删除**；删除动作由通过四项检查后的调用方按 manifest 精确执行。校验通过前，manifest 视同未验证产物，同 §五"不采信自我报告"。
- Archive Contract 预检：release executor 调用只读 `check` 子命令，把结果原样写入 `archive_precheck` 字段，不自行判定归档是否有效、不手工编辑归档 JSON——呼应 [`SKILL.md` §五](../SKILL.md) 的证据回收原则：目录被复制到 `.converge/done`，不等于归档脚本和 manifest 契约通过。

**保护默认**：凭据文件（`.env*`、`*.pem`、路径含 `secret`/`credential`/`api_key`/`sk-` 字面量）与非本任务的临时/收敛目录（如其他 slug 的 `.converge/tmp/`、其他 slug 的收敛 active/done 目录）默认视为不可删除项，不得出现在 `cleanup_manifest` 中——即使看起来像"垃圾文件"。

**模型选择**：机械收尾（文档同步、manifest 生成、数字核对）用执行档 `deepseek/deepseek-v4-flash`（§八）；涉及需要判断的一致性核验（如"retrospective 是否准确反映实际过程"）按判断密集角色升级，不强行用执行档模型做语义判断。
