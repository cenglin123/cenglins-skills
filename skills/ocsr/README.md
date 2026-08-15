# OCSR

OCSR（OpenCode Subagents Run）把 headless `opencode run` 用作独立于宿主框架的子代理执行后端，适合跨厂商异构模型、廉价批处理 worker 和 fresh-context 对抗评审；亦提供层级指挥模式（planner / orchestrator / worker 分层编排，见 SKILL.md §十）。

## 快速开始

前置条件：Windows PowerShell、Python 3，以及已配置模型 provider 的 OpenCode CLI。

```powershell
opencode models
opencode run "请完成一个自包含任务" -m deepseek/deepseek-v4-flash
python scripts/verify_ocsr_skill.py
```

运行规则、prompt 六要素、产物回收和模型分工以 [SKILL.md](SKILL.md) 为准。

## 包结构

- `SKILL.md`：技能入口与核心工作流
- `scripts/`：派发器、确定性 spec 运行器与离线验证器
- `refs/`：派发模式、模型池、失败模式、层级指挥及框架对接参考
- `tests/`：不调用模型的核心回归测试
- `DEVELOPMENT.md`：脱敏后的开发沿革与本次导入边界

## 验证

```powershell
python scripts/verify_ocsr_skill.py
python -m pytest tests -q
```

本包的核心产物是 skill 文档和机械验证脚本，不提供安全沙箱、常驻服务或通用运行时 wrapper。旧开发仓库的 Git 元数据、审计现场和机器状态未随包分发；当前仓库中的导入提交是新的历史起点，详见 [DEVELOPMENT.md](DEVELOPMENT.md)。
