#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocsr_dispatch.py — ocsr (OpenCode Subagents Run) 派发后端（库内执行层，项目无关版）

把"模板生成→launcher→脱管启动→双监视看门狗→产物验证→遥测"整链固化为脚本。
脚本不做编排判断（选模型/选模式/prompt 内容仍由 agent 决定），仅提供可复用的执行层。

用法:
  # 预先生成 prompt 文件（UTF-8），然后派发
  python scripts/ocsr_dispatch.py dispatch \\
    --worker prompt-r1.txt|model-id|R1 \\
    --output-dir ./evidence \\
    --watch --timeout 15

  # 冒烟测试
  python scripts/ocsr_dispatch.py selftest

  # 查看遥测摘要
  python scripts/ocsr_dispatch.py telemetry

遥测日志: ~/.ocsr/dispatch-log.jsonl（本机共享，预期行为）
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ─── Windows UTF-8 ───────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# ─── 常量 ────────────────────────────────────────────────────────────
DEFAULT_STAGGER = 5       # 秒，多 worker 错峰间隔
DEFAULT_TIMEOUT = 15      # 分钟，单个 worker 看门狗阈值
DISPATCH_LOG = Path.home() / ".ocsr" / "dispatch-log.jsonl"
RETRY_DELAY_DB_LOCK = 30  # 秒，database is locked 后延迟重试
CONVERGE_LEDGER_NAME = "ocsr-dispatch-ledger.jsonl"  # 入库账本（随收敛证据一起提交）
# dispatch 退出码契约（单一事实源见 refs/dispatch-patterns.md §退出码契约）
#   0 = 全部 worker 落盘
#   1 = 看门狗超时（既有语义）
#   2 = 至少一个 worker 确定性失败（launcher error / opencode 非零退出 /
#       opencode 退出码为 0 但期望产物未落盘）
#   3 = 路径碰撞（既有语义）
# 混合结局优先级：3 > 1 > 2 > 0
EXIT_DETERMINISTIC_FAILURE = 2  # 至少一个 worker 确定性失败
EXIT_PATH_COLLISION = 3   # 既有文件被非预期覆盖时的退出码
COST_CACHE_PATH = Path.home() / ".ocsr" / "model-cost-cache.json"
COST_CACHE_TTL_SEC = 86400  # 24 小时

# dispatch-log 角色 enum（值域迁移：历史值 ocsr-dispatch 视为 legacy）
ROLE_EXECUTOR = "executor"
ROLE_REVIEWER = "reviewer"
ROLE_RELEASE_EXECUTOR = "release-executor"
ROLE_ULTRAVERGE_INITIAL = "ultraverge-initial"
ROLE_OUTER_REVIEWER = "outer-reviewer"
ROLE_BLIND_REVIEWER = "blind-reviewer"
ROLE_DESIGN_REVIEWER = "design-reviewer"
ROLE_ARBITER = "arbiter"
ROLE_FRESH_VERIFIER = "fresh-verifier"
ROLE_ORCHESTRATOR = "orchestrator"
ROLE_PLANNER = "planner"
ROLE_COMMANDER = "commander"
ROLE_WORKER = "worker"
ROLE_LEGACY = "legacy"
ROLE_VALUES = frozenset({
    ROLE_EXECUTOR, ROLE_REVIEWER, ROLE_RELEASE_EXECUTOR,
    ROLE_ULTRAVERGE_INITIAL, ROLE_OUTER_REVIEWER, ROLE_BLIND_REVIEWER,
    ROLE_DESIGN_REVIEWER, ROLE_ARBITER,
    ROLE_FRESH_VERIFIER, ROLE_ORCHESTRATOR, ROLE_PLANNER,
    ROLE_COMMANDER, ROLE_WORKER,
})

# scope enum（与 budget_gate ROLE_CONSUMES 对齐）
SCOPE_TASK_ENVELOPE = "task-envelope"
SCOPE_OUTER = "outer"
SCOPE_BLIND = "blind"
SCOPE_ULTRAVERGE = "ultraverge"
SCOPE_NONE = "none"
SCOPE_VALUES = frozenset({SCOPE_TASK_ENVELOPE, SCOPE_OUTER, SCOPE_BLIND,
                          SCOPE_ULTRAVERGE, SCOPE_NONE})

# 超时策略
TIMEOUT_POLICY_LEAF_KILL = "leaf_kill"        # 到期自动 kill 进程
TIMEOUT_POLICY_HIERARCHICAL_REPORT = "hierarchical_report"  # 层级 orchestrator：到期报告/alive，留进程供 commander 裁决
TIMEOUT_POLICY_AUTO = "auto"                  # 默认：按角色自动解析
TIMEOUT_POLICY_VALUES = frozenset({TIMEOUT_POLICY_LEAF_KILL, TIMEOUT_POLICY_HIERARCHICAL_REPORT, TIMEOUT_POLICY_AUTO})

# 层级角色：auto 策略下这些角色自动解析为 hierarchical_report
_HIERARCHICAL_ROLE_PREFIXES = ("orchestrator", "planner", "commander", "ultraverge-initial", "arbiter")

# 遥测字段集合（供验证脚本引用，与 _append_telemetry 写出的字段保持一致）
# "required" = 始终存在, "optional" = 仅条件存在
TELEMETRY_FIELDS: dict[str, str] = {
    "ts": "required",
    "model": "required",
    "role": "required",
    "harness": "required",
    "channel": "required",
    "outcome": "required",
    "wall_min": "required",
    "artifact_bytes": "required",
    "task_id": "required",
    "plan_ref": "required",
    "scope": "required",
    "prompt_size_bytes": "required",
    "response_size_bytes": "required",
    "model_cost_input": "required",
    "model_cost_output": "required",
    "cost_estimate": "required",
    "blocking_chain": "required",
    "outcome_detail": "required",
    "failure_retry_index": "required",
    "label": "optional",
    "note": "optional",
    "timeout_policy_requested": "optional",
    "timeout_policy_resolved": "optional",
    "forbid_paths": "optional",
    "read_audit": "optional",
}

# 字节→token 近似系数（英文约 4 bytes/token，中文约 2-3 bytes/token）
# 保守低估，对作绝对量级与趋势判断足够，不作百分比门禁判定
BYTES_PER_TOKEN = 4

# 用户主目录前缀（路径隐私用）
_USER_HOME = str(Path.home())
_USER_HOME_FORWARD = _USER_HOME.replace("\\", "/")

# ─── 模型白名单 ───────────────────────────────────────────────────────
ALLOWED_MODELS = frozenset({
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "xiaomi/mimo-v2.5",
    "xiaomi/mimo-v2.5-pro",
})


def _validate_model_allowed(model: str) -> None:
    """Validate model is in the OCSR allowlist. Raises ValueError with clear error."""
    if model not in ALLOWED_MODELS:
        allowed = ", ".join(sorted(ALLOWED_MODELS))
        raise ValueError(
            f"Model '{model}' is not in the OCSR allowlist.\n"
            f"Allowed models: {allowed}\n"
            f"Run 'opencode models --verbose' to check "
            f"whether any allowed model is configured locally."
        )


def _check_model_calls_disabled() -> None:
    """Fail-fast tripwire: if OCSR_DISABLE_MODEL_CALLS=1, exit before any model call."""
    if os.environ.get("OCSR_DISABLE_MODEL_CALLS") == "1":
        print(
            "OCSR_DISABLE_MODEL_CALLS=1: model calls disabled. "
            "Set to 0 or unset to allow.",
            file=sys.stderr,
        )
        sys.exit(1)


def _sanitize_path(val: str) -> str:
    """Replace user-home prefix with <user-home> for path privacy in Git-tracked files."""
    if val.startswith(_USER_HOME):
        return "<user-home>" + val[len(_USER_HOME):]
    if val.startswith(_USER_HOME_FORWARD):
        return "<user-home>" + val[len(_USER_HOME_FORWARD):]
    return val


def _sanitize_ledger_row(row: dict) -> dict:
    """Sanitize path-like string values in a ledger row."""
    PATH_KEYS = {"prompt_file", "expected_output", "work_dir", "output", "detail"}
    result = {}
    for k, v in row.items():
        if isinstance(v, str) and k in PATH_KEYS:
            result[k] = _sanitize_path(v)
        elif isinstance(v, str) and k == "note":
            result[k] = v
        else:
            result[k] = v
    return result


def _resolve_timeout_policy(policy: str, role: str) -> str:
    """Resolve auto policy to concrete policy based on role.

    - Auto resolves to hierarchical_report for orchestrator-like roles,
      leaf_kill for all others.
    - Explicit policies (leaf_kill, hierarchical_report) pass through unchanged.
    """
    if policy != TIMEOUT_POLICY_AUTO:
        return policy
    role_lower = role.lower()
    if any(role_lower.startswith(p) for p in _HIERARCHICAL_ROLE_PREFIXES):
        return TIMEOUT_POLICY_HIERARCHICAL_REPORT
    return TIMEOUT_POLICY_LEAF_KILL


# ─── 工具 ────────────────────────────────────────────────────────────
def _pwsh_code(text: str) -> str:
    """生成 PowerShell launcher 脚本内容，含模型调用防护。"""
    return textwrap.dedent(f"""\
        if ($env:OCSR_DISABLE_MODEL_CALLS -eq '1') {{
            "launcher blocked: OCSR_DISABLE_MODEL_CALLS=1" | Set-Content "$PSScriptRoot/error.log"
            exit 1
        }}
        "pwsh started $(Get-Date -Format o)" | Set-Content "$PSScriptRoot/start.marker"
        try {{
          $prompt = Get-Content "$PSScriptRoot/prompt.txt" -Raw -Encoding UTF8
          opencode run $prompt {text} *> "$PSScriptRoot/run.log"
          "exit=$LASTEXITCODE" | Add-Content "$PSScriptRoot/start.marker"
        }} catch {{
          $_ | Out-File "$PSScriptRoot/error.log"
        }}
    """)


def _write_utf8(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


PID_FILE_NAME = "pid.txt"


def _launch_command(wd: Path) -> str:
    """生成拉起 worker 的 PowerShell 命令：`-PassThru` 捕获 PID 并写入 <wd>/pid.txt。

    **两个拉起调用点（初始派发、DB 锁重派）必须共用本函数。**
    重派若不刷新 pid.txt，看门狗到期时会对已死的旧 PID 执行 taskkill，
    而重派出的新 worker 继续存活消耗模型调用——正是本函数要消灭的失效模式。
    """
    p = wd.as_posix()
    return (
        f'$proc = Start-Process pwsh -ArgumentList '
        f'"-NoProfile","-ExecutionPolicy","Bypass","-File","{p}/launcher.ps1" '
        f'-WindowStyle Hidden -PassThru; '
        f'Set-Content -Path "{p}/{PID_FILE_NAME}" -Value $proc.Id -Encoding ascii'
    )


def _read_pid(wd: Path) -> int | None:
    """读取 <wd>/pid.txt。返回 None 表示 PID 不可用（未捕获/文件损坏）。"""
    try:
        raw = (wd / PID_FILE_NAME).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, AttributeError):
        return None
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else None


def _parse_frontmatter(path: Path) -> dict | None:
    """读取 markdown 文件 YAML frontmatter。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    try:
        import yaml
        return yaml.safe_load(m.group(1))
    except Exception:
        return None


# 模块级变量：由 dispatch 子命令在启动时设置，供 _append_telemetry 使用
harness_tag: str = "cli"

# cost 缓存：模块级惰性加载
_cost_cache: dict | None = None
_cost_cache_loaded: float = 0.0


def _load_cost_cache() -> dict:
    global _cost_cache, _cost_cache_loaded
    _cost_cache_loaded = time.time()
    try:
        if COST_CACHE_PATH.is_file():
            data = json.loads(COST_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _cost_cache = data
                return data
    except Exception:
        pass
    _cost_cache = {}
    return _cost_cache


def _refresh_cost_cache(force: bool = False) -> dict:
    global _cost_cache, _cost_cache_loaded
    _check_model_calls_disabled()
    now = time.time()
    if not force and _cost_cache is not None and (now - _cost_cache_loaded) < COST_CACHE_TTL_SEC:
        return _cost_cache
    try:
        proc = subprocess.run(
            ["opencode", "models", "--verbose"],
            capture_output=True, text=True, timeout=30,
        )
        raw = (proc.stdout or "") + (proc.stderr or "")
        # 解析多段 JSON 输出：每段以模型 ID 行开头，紧接 JSON 行
        cache: dict = {}
        lines = raw.strip().split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line and not line.startswith("{"):
                model_id = line
                i += 1
                json_lines = []
                while i < len(lines) and not lines[i].startswith(" ") and not lines[i].startswith("{"):
                    i += 1
                while i < len(lines):
                    jl = lines[i].strip()
                    if jl.startswith("{") and jl.endswith("}"):
                        json_lines.append(jl)
                        break
                    elif jl.startswith("{"):
                        json_lines.append(jl)
                    else:
                        if json_lines:
                            break
                    i += 1
                if json_lines:
                    full = "".join(json_lines)
                    try:
                        obj = json.loads(full)
                        if isinstance(obj, dict):
                            cost = obj.get("cost", {})
                            if isinstance(cost, dict):
                                inp = cost.get("input", 0)
                                out = cost.get("output", 0)
                                if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
                                    cache[model_id] = {"input": float(inp), "output": float(out)}
                    except json.JSONDecodeError:
                        pass
            i += 1
        COST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COST_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        _cost_cache = cache
        _cost_cache_loaded = time.time()
        return cache
    except Exception:
        return _cost_cache or {}


def _lookup_model_cost(model: str) -> dict:
    cache = _refresh_cost_cache()
    entry = cache.get(model, {})
    if not isinstance(entry, dict):
        entry = {}
    return {
        "input": float(entry.get("input", 0)),
        "output": float(entry.get("output", 0)),
    }


def _normalize_role(role_val: str) -> str:
    if role_val in ROLE_VALUES:
        return role_val
    return ROLE_LEGACY


def _estimate_cost(prompt_bytes: int, response_bytes: int, cost_input: float, cost_output: float) -> float:
    return (cost_input * prompt_bytes + cost_output * response_bytes) / BYTES_PER_TOKEN / 1_000_000


def _resolve_prompt_size(prompt_path: str | None, inline_text: str | None = None) -> int:
    if prompt_path:
        try:
            return os.path.getsize(prompt_path)
        except OSError:
            pass
    if inline_text is not None:
        return len(inline_text.encode("utf-8"))
    return 0


def _parse_outcome_detail(outcome: str, exit_code: int | None = None, log_text: str = "") -> str:
    if outcome == "killed" and "timeout" in log_text.lower():
        return "killed:harness-timeout"
    if outcome == "killed":
        return "killed:unknown"
    if outcome == "stall" and "no progress" in log_text.lower():
        return "stall:no-progress"
    if outcome == "stall" and "database is locked" in log_text.lower():
        return "stall:database-locked"
    if outcome == "stall":
        return "stall:watchdog-timeout"
    if outcome == "error" and exit_code is not None:
        return f"error:exit_code_{exit_code}"
    if outcome == "error":
        return "error:unknown"
    if outcome == "success":
        return "success:completed"
    return f"{outcome}:unknown"


def _append_telemetry(
    model: str,
    role: str,
    channel: str,
    outcome: str,
    wall_min: float,
    artifact_bytes: int,
    note: str = "",
    task_id: str = "",
    label: str = "",
    plan_ref: str = "",
    scope: str = "",
    prompt_size_bytes: int = 0,
    response_size_bytes: int = 0,
    model_cost_input: float = 0.0,
    model_cost_output: float = 0.0,
    cost_estimate: float | None = None,
    blocking_chain: list[str] | None = None,
    outcome_detail: str = "",
    failure_retry_index: int = 0,
    timeout_policy_requested: str = "",
    timeout_policy_resolved: str = "",
    forbid_paths: int = 0,
    read_audit: str = "",
) -> None:
    now_ts = datetime.datetime.now().astimezone().isoformat()
    row = {
        "ts": now_ts,
        "model": model,
        "role": role,
        "harness": harness_tag,
        "channel": channel,
        "outcome": outcome,
        "wall_min": wall_min,
        "artifact_bytes": artifact_bytes,
        "task_id": task_id or f"dispatch_{int(time.time())}",
        "plan_ref": plan_ref or "",
        "scope": scope or "",
        "prompt_size_bytes": prompt_size_bytes,
        "response_size_bytes": response_size_bytes,
        "model_cost_input": model_cost_input,
        "model_cost_output": model_cost_output,
        "cost_estimate": cost_estimate if cost_estimate is not None else 0.0,
        "blocking_chain": blocking_chain or [],
        "outcome_detail": outcome_detail or _parse_outcome_detail(outcome),
        "failure_retry_index": failure_retry_index,
    }
    if label:
        row["label"] = label
    if note:
        row["note"] = note
    if timeout_policy_requested:
        row["timeout_policy_requested"] = timeout_policy_requested
    if timeout_policy_resolved:
        row["timeout_policy_resolved"] = timeout_policy_resolved
    if forbid_paths:
        row["forbid_paths"] = forbid_paths
    if read_audit:
        row["read_audit"] = read_audit
    DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DISPATCH_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─── 派发账本 + 路径碰撞检测 ─────────────────────────────────────────

def _snapshot_dir(path: Path) -> dict[str, tuple[int, int]]:
    """快照目录顶层文件的 (size, mtime_ns)，供事后比对非预期变更。"""
    snap: dict[str, tuple[int, int]] = {}
    try:
        for p in path.iterdir():
            if p.is_file():
                st = p.stat()
                snap[p.name] = (st.st_size, st.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        pass
    return snap


def _converge_ledger_path(output_dir: Path, explicit: str | None) -> Path | None:
    """定位派发账本。仅支持显式 --ledger-dir；不执行自动路径探测。"""
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d / CONVERGE_LEDGER_NAME
    return None


def _append_dispatch_ledger(ledger: Path | None, row: dict) -> None:
    """向收敛目录的派发账本追加一行（append-only，失败不阻断派发）。"""
    if ledger is None:
        return
    sanitized = _sanitize_ledger_row(row)
    payload = {"ts": datetime.datetime.now().astimezone().isoformat(), **sanitized}
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ 派发账本写入失败 ({ledger}): {e}", file=sys.stderr)


def _collision_report(
    output_dir: Path,
    before: dict[str, tuple[int, int]],
    expected: set[str],
    ledger: Path | None,
) -> bool:
    """比对派发前后快照。返回 True 表示**既有文件被非预期覆盖**（真实损失）。"""
    after = _snapshot_dir(output_dir)
    ignore = expected | {CONVERGE_LEDGER_NAME}
    overwritten = sorted(
        n for n, meta in after.items()
        if n in before and before[n] != meta and n not in ignore
    )
    unexpected_new = sorted(n for n in after if n not in before and n not in ignore)

    if overwritten:
        print(f"[ocsr] ❌ {len(overwritten)} 个既有文件被非预期覆盖：", file=sys.stderr)
        for n in overwritten:
            print(f"        {n}  {before[n][0]}B → {after[n][0]}B", file=sys.stderr)
        print("        子代理写到了 --output-pattern 之外的路径。"
              "首查 prompt 的输出路径是否含未解析占位符。", file=sys.stderr)
    if unexpected_new:
        print(f"[ocsr] ⚠️ {len(unexpected_new)} 个非预期新增文件："
              f"{', '.join(unexpected_new[:5])}", file=sys.stderr)

    if overwritten or unexpected_new:
        _append_dispatch_ledger(ledger, {
            "event": "path_anomaly",
            "overwritten": overwritten,
            "unexpected_new": unexpected_new,
        })
        _append_telemetry("-", "ocsr-dispatch", "detached",
                          "path_collision" if overwritten else "unexpected_write",
                          0, 0, f"overwritten={overwritten} new={unexpected_new}",
                          outcome_detail=f"{'overwritten' if overwritten else 'unexpected_write'}:path_anomaly")
    return bool(overwritten)


# ─── monitor 工具 ────────────────────────────────────────────────────

def _dir_stall_check(path: Path, stall_minutes: int) -> tuple[bool, float]:
    now = time.time()
    newest = 0.0
    try:
        if not path.is_dir():
            return (True, -1.0)
        for p in path.rglob("*"):
            if p.is_file():
                mtime = p.stat().st_mtime_ns / 1e9
                if mtime > newest:
                    newest = mtime
    except (FileNotFoundError, PermissionError):
        return (True, -1.0)
    if newest == 0.0:
        return (True, -1.0)
    elapsed = (now - newest) / 60
    return (elapsed > stall_minutes, elapsed)


def _is_process_running(process_name: str) -> bool:
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
                capture_output=True, text=True, timeout=15,
            )
            return len(proc.stdout.strip().split("\n")) > 1
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    else:
        try:
            proc = subprocess.run(
                ["pgrep", process_name],
                capture_output=True, timeout=15,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


# ─── 评审锚定污染对治：--forbid-paths ────────────────────────────────

def _build_forbid_block(forbid_paths: list[str]) -> str:
    """生成禁止读取块（追加到 prompt 副本末尾，不改动原 prompt 文件）。"""
    norm = [fp.replace("\\", "/").rstrip("/") for fp in forbid_paths]
    listed = "\n".join(f"  - {p}" for p in norm)
    return (
        "【边界与禁区：禁止读取】\n"
        "- 以下路径及其子路径下的任何内容一律禁止读取（Read/Grep/Glob/搜索等一切方式均不允许）：\n"
        f"{listed}\n"
        "- 若意外接触到上述路径的内容，不得将其结论、措辞或结构纳入本报告。\n"
        "\n"
        "【执行证据】报告的执行证据段必须包含结构化顶层 YAML 列表 `reads:`，逐项列出本次实际读取的全部文件路径，格式示例：\n"
        "reads:\n"
        "  - C:/path/to/file-a.md\n"
        "  - C:/path/to/file-b.py\n"
    )


def _normalize_audit_path(p: str) -> str:
    """审计用路径归一化：去引号/反引号、正斜杠、去尾斜杠、大小写不敏感。"""
    return p.strip().strip("`").strip('"').strip("'").replace("\\", "/").rstrip("/").casefold()


def _parse_reads_list(text: str) -> list[str] | None:
    """宽松解析顶层 `reads:` 列表：找到行首 reads: 行后收集后续 `- ` 列表项。

    容忍缩进与 Windows 路径（条目中的反斜杠/盘符冒号不影响解析）。
    找不到 reads: 行返回 None（审计判 unavailable）。
    """
    reads: list[str] | None = None
    for line in text.split("\n"):
        if reads is None:
            m = re.match(r"^\s*reads\s*:\s*(.*)$", line)
            if m:
                rest = m.group(1).strip()
                if rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1].strip()
                    reads = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                else:
                    reads = []
            continue
        if line.strip() == "":
            continue  # 容忍列表项之间的空行
        m = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if m:
            reads.append(m.group(1))
        else:
            break
    return reads


def _audit_output_reads(output: Path, forbid_paths: list[str]) -> tuple[str, str]:
    """读路径审计：产物 reads 列表逐条与禁止路径对照（子路径算命中）。

    返回 (状态, 违规路径)；状态 ∈ clean / violated / unavailable。
    审计是报告机制，不影响退出码——裁决归 orchestrator。
    """
    try:
        text = output.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ("unavailable", "")
    reads = _parse_reads_list(text)
    if reads is None:
        return ("unavailable", "")
    forbids = [_normalize_audit_path(fp) for fp in forbid_paths]
    for entry in reads:
        norm = _normalize_audit_path(entry)
        if not norm:
            continue
        for fp in forbids:
            if norm == fp or norm.startswith(fp + "/"):
                return ("violated", entry.strip().strip("`"))
    return ("clean", "")


# ─── 子命令 ──────────────────────────────────────────────────────────

def _dispatch_batch(
    workers: list[dict],
    *,
    output_dir: Path,
    work_dir: Path | None = None,
    stagger: int = DEFAULT_STAGGER,
    timeout_min: int = DEFAULT_TIMEOUT,
    timeout_policy: str = TIMEOUT_POLICY_AUTO,
    watch: bool = False,
    progress: bool = False,
    ledger_dir: str | None = None,
    forbid_paths: list[str] | None = None,
    role: str = "ocsr-dispatch",
    task_id: str = "",
    plan_ref: str = "",
    scope: str = "",
    blocking_chain: list[str] | None = None,
    converge_invocation_id: str = "",
) -> int:
    """派发内核：**已解析完毕**的 worker 批次 → 退出码。

    这是 `dispatch` 子命令与 `run` 的 dispatch 步骤**共用**的唯一执行路径
    （设计 D9）。它不做 CLI 参数解析、不读 argparse Namespace，
    因此 `run` 可在**进程内**调用，无需起子进程。

    worker 字典契约（IMP-2，权威定义 —— 两个调用方必须按此构造）::

        {
          "prompt_file": str,          # 必填：已存在的 prompt 文件路径
          "model": str,                # 必填：OCSR 白名单内的 qualified ID
          "label": str,                # 必填：worker 标识（用于 work-dir 名与遥测）
          "output": Path,              # 必填：期望产物路径
          "prompt_size_bytes": int,    # 可选：遥测用，缺省 0
        }

    入口重校验（S1）：`--validate` 是**离线干跑**，与真正执行之间存在窗口期
    （prompt 可能被删、模型配置可能变更、输出目录可能消失）。
    因此本函数在产生任何副作用前**重新校验**模型白名单、prompt 存在性与输出目录，
    **不以「上游已经校验过」为由跳过**。

    返回值遵循 `refs/dispatch-patterns.md` §退出码契约：0/1/2/3，优先级 3 > 1 > 2 > 0。
    """
    _check_model_calls_disabled()
    forbid_paths = forbid_paths or []
    blocking_chain = blocking_chain or []

    if not workers:
        print("❌ _dispatch_batch: worker 列表为空", file=sys.stderr)
        return 1
    for i, w in enumerate(workers):
        for key in ("prompt_file", "model", "label", "output"):
            if not w.get(key):
                print(f"❌ _dispatch_batch: worker[{i}] 缺少必填字段 `{key}`", file=sys.stderr)
                return 1
        try:
            _validate_model_allowed(w["model"])
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if not Path(w["prompt_file"]).is_file():
            print(f"❌ prompt 文件不存在: {w['prompt_file']}", file=sys.stderr)
            return 1
        w.setdefault("prompt_size_bytes", _resolve_prompt_size(w["prompt_file"]))
    if not output_dir.is_dir():
        print(f"❌ 输出目录不存在: {output_dir}", file=sys.stderr)
        return 1

    parsed = workers
    ledger = _converge_ledger_path(output_dir, ledger_dir)
    snapshot_before = _snapshot_dir(output_dir)
    expected_names = {Path(p["output"]).name for p in parsed}
    if progress and ledger is not None:
        print(f"[ocsr] 派发账本: {ledger}")

    # 创建工作目录（加 uuid4 后缀防同秒碰撞）
    import uuid
    base = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", "/tmp"))
    batch_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    batch_dir = base / f"ocsr_dispatch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    # 为每个 worker 生成 launcher
    work_dirs: list[Path] = []
    for i, p in enumerate(parsed):
        wd = batch_dir / str(p["label"]).replace("/", "-").replace(" ", "_")
        wd.mkdir(parents=True, exist_ok=True)
        work_dirs.append(wd)

        # 复制 prompt（注入输出路径占位符）
        prompt_content = Path(p["prompt_file"]).read_text(encoding="utf-8")
        prompt_content = prompt_content.replace("{{OUTPUT_PATH}}", str(p["output"]))
        prompt_content = prompt_content.replace("{{OUTPUT_NAME}}", Path(p["output"]).name)
        prompt_content = prompt_content.replace("{{OUTPUT_DIR}}", str(output_dir))
        # 注入禁止读取块（写入 work-dir 下的 prompt 副本，不改动原 prompt 文件）
        if forbid_paths:
            prompt_content = prompt_content.rstrip("\n") + "\n\n" + _build_forbid_block(forbid_paths)
        _write_utf8(wd / "prompt.txt", prompt_content)

        # 生成 launcher
        safe_label = re.sub(r'[^\w一-鿿-]', '_', str(p['label']))[:30]
        model_arg = f'-m {p["model"]} --title "ocsr-{safe_label}"'
        _write_utf8(wd / "launcher.ps1", _pwsh_code(model_arg))

        if progress:
            print(f"[ocsr] [{i+1}/{len(parsed)}] 已就绪: {p['label']} ({p['model']}) → {p['output']}")

    # 脱管启动（带 stagger）
    print(f"[ocsr] 开始脱管启动 ({len(parsed)} workers, stagger={stagger}s, timeout={timeout_min}min)")
    start_times: list[float] = []
    for i, wd in enumerate(work_dirs):
        if i > 0 and stagger > 0:
            if progress:
                print(f"[ocsr] 等待 {stagger}s（错峰启动）...")
            time.sleep(stagger)
        cmd = _launch_command(wd)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                _append_telemetry(parsed[i]["model"], _normalize_role(role), "detached", "error", 0, 0,
                                  f"powershell Start-Process failed: {proc.stderr[:200]}",
                                  prompt_size_bytes=parsed[i].get("prompt_size_bytes", 0),
                                  task_id=task_id, label=parsed[i]["label"],
                                  plan_ref=plan_ref,
                                  scope=scope, blocking_chain=blocking_chain)
                if progress:
                    print(f"⚠️ [{parsed[i]['label']}] Start-Process 返回非零: {proc.stderr[:200]}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            _append_telemetry(parsed[i]["model"], _normalize_role(role), "detached", "error", 0, 0,
                              "powershell Start-Process timed out",
                              prompt_size_bytes=parsed[i].get("prompt_size_bytes", 0),
                              task_id=task_id, label=parsed[i]["label"],
                              plan_ref=plan_ref,
                              scope=scope, blocking_chain=blocking_chain)
        start_times.append(time.time())
        # 注：这行父进程写入是既有行为，本次拆分**刻意保持不变**。
        # 删除它是 20260809 计划 Phase 2 的 B7 项（marker 应仅由 launcher 产生），
        # 那是独立的行为变更、需走自己的评审，不在本次机械拆分范围内 ——
        # 否则「测试全绿」就不再能证明这次拆分是纯重构。
        (wd / "start.marker").write_text(
            f"pwsh started {datetime.datetime.now().astimezone().isoformat()}\n", encoding="utf-8")
        parsed[i]["work_dir"] = wd
        launched_row: dict[str, object] = {
            "event": "launched",
            "batch_id": batch_id,
            "label": parsed[i]["label"],
            "model": parsed[i]["model"],
            "harness": harness_tag,
            "prompt_file": str(parsed[i]["prompt_file"]),
            "expected_output": str(parsed[i]["output"]),
            "work_dir": str(wd),
        }
        if converge_invocation_id:
            launched_row["converge_invocation_id"] = converge_invocation_id
        if forbid_paths:
            launched_row["forbid_paths"] = len(forbid_paths)
        _append_dispatch_ledger(ledger, launched_row)

    print("[ocsr] 全部 worker 已启动，等待产物落盘...")

    if watch:
        requested_policy = timeout_policy
        resolved_policy = _resolve_timeout_policy(timeout_policy, role)
        rc = _watch_loop(parsed, start_times, timeout_min, progress, ledger,
                         task_id=task_id, role=role, plan_ref=plan_ref,
                         scope=scope, blocking_chain=blocking_chain,
                         snapshot_before=snapshot_before,
                         timeout_policy=resolved_policy,
                         timeout_policy_requested=requested_policy,
                         converge_invocation_id=converge_invocation_id,
                         forbid_paths=forbid_paths)
        if _collision_report(output_dir, snapshot_before, expected_names, ledger):
            return EXIT_PATH_COLLISION
        return rc

    if progress:
        print("[ocsr] 未启用 --watch：跳过产物回收与路径碰撞检测（仅记 launched）")
    return 0


def cmd_dispatch(args) -> int:
    """`dispatch` 子命令的**薄 CLI 包装**：解析参数 → 构造 worker 列表 → 调 `_dispatch_batch`。

    执行逻辑全部在内核里（D9）；本函数只负责 argparse Namespace 到 worker 契约的转换，
    以及面向命令行的早期报错（返回 1，不抛 traceback）。
    """
    _check_model_calls_disabled()
    global harness_tag
    harness_tag = args.harness or "cli"
    workers = args.worker
    output_dir = Path(args.output_dir)
    stagger = args.stagger if args.stagger is not None else DEFAULT_STAGGER
    timeout_min = args.timeout if args.timeout is not None else DEFAULT_TIMEOUT
    timeout_policy = args.timeout_policy or TIMEOUT_POLICY_AUTO
    watch = args.watch
    progress = args.progress
    output_pattern = args.output_pattern or "{label}.md"
    date_str = datetime.date.today().strftime("%Y-%m-%d")

    if not workers:
        print("❌ 至少需要一个 --worker", file=sys.stderr)
        return 1

    # 解析 --meta 元数据
    meta: dict[str, str] = {}
    if hasattr(args, "meta") and args.meta:
        for kv in args.meta:
            if "=" not in kv:
                print(f"⚠️ --meta 格式错误（忽略）: {kv}", file=sys.stderr)
                continue
            k, v = kv.split("=", 1)
            meta[k.strip()] = v.strip()
    task_id = meta.get("task_id", "")
    role = meta.get("role", "ocsr-dispatch")
    plan_ref = meta.get("plan_ref", "")
    scope = meta.get("scope", "")
    bc_raw = meta.get("blocking_chain", "")
    blocking_chain = [x.strip() for x in bc_raw.split(",") if x.strip()] if bc_raw else []
    converge_invocation_id = meta.get("converge-invocation-id", "")

    # 解析 --forbid-paths（评审锚定污染对治）
    forbid_paths = [fp.strip() for fp in (getattr(args, "forbid_paths", None) or [])
                    if fp and fp.strip()]

    # 解析 workers（分隔符 | 避免与 Windows 盘符 C: 及模型 ID 中的 / 冲突）
    # 格式：PROMPT_PATH|MODEL|LABEL
    parsed = []
    for w in workers:
        parts = w.split("|", 2)
        if len(parts) != 3:
            print(f"❌ worker 格式错误: `{w}`（应为 PROMPT_PATH|MODEL|LABEL，| 分隔）", file=sys.stderr)
            return 1
        prompt_file, model, label = parts
        try:
            _validate_model_allowed(model)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if not Path(prompt_file).is_file():
            print(f"❌ prompt 文件不存在: {prompt_file}", file=sys.stderr)
            return 1
        output_name = output_pattern.format(date=date_str, label=label, model=model.replace("/", "-"))
        prompt_size = _resolve_prompt_size(prompt_file)
        parsed.append({"prompt_file": prompt_file, "model": model, "label": label,
                       "output": output_dir / output_name, "prompt_size_bytes": prompt_size})
    # 检查 output 路径冲突
    outputs = [p["output"] for p in parsed]
    if len(outputs) != len(set(str(o) for o in outputs)):
        print("❌ output 路径冲突（output-pattern 产生了重复文件名）", file=sys.stderr)
        return 1

    # 查模型 cost
    cost_input = 0.0
    cost_output = 0.0
    if parsed:
        cost_info = _lookup_model_cost(parsed[0]["model"])
        cost_input = cost_info["input"]
        cost_output = cost_info["output"]

    return _dispatch_batch(
        parsed,
        output_dir=output_dir,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        stagger=stagger,
        timeout_min=timeout_min,
        timeout_policy=timeout_policy,
        watch=watch,
        progress=progress,
        ledger_dir=getattr(args, "ledger_dir", None),
        forbid_paths=forbid_paths,
        role=role,
        task_id=task_id,
        plan_ref=plan_ref,
        scope=scope,
        blocking_chain=blocking_chain,
        converge_invocation_id=converge_invocation_id,
    )


def _read_start_marker_exit(marker: Path) -> int | None:
    """从 start.marker 读最后的 exit= 行，返回退出码或 None（进程仍在运行）。"""
    try:
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^exit=(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _kill_worker(label: str, wd: Path) -> bool:
    """按 PID 终止 worker 的 pwsh 进程树。返回 True 仅当 taskkill 报告成功。

    历史缺陷（2026-08-09 审计）：旧实现按 `WINDOWTITLE eq ocsr-*<label>*` 过滤，
    但代码从未设置过 pwsh 的窗口标题——`--title` 是传给 opencode 的**会话标题**，
    且 launcher 以 `-WindowStyle Hidden` 启动，隐藏进程在 tasklist 中窗口标题为 N/A。
    过滤器匹配不到任何进程，且函数无条件 return True，调用点也不检查返回值：
    看门狗「到期 kill」这条止损纪律在驱动器里实为空操作。

    现按 `_launch_command` 捕获的真实 PID 终止，并校验 taskkill 退出码。
    `/T` 连带终止子进程（launcher pwsh → opencode）；只杀目标 PID，
    不使用 `taskkill /IM`——那会连带杀死正在正常工作的兄弟 worker（SKILL.md §五）。
    """
    pid = _read_pid(wd)
    if pid is None:
        print(f"[ocsr] ⚠️ {label} 无可用 PID（{PID_FILE_NAME} 缺失或损坏），无法按 PID 终止",
              file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        print(f"[ocsr] ⚠️ {label} taskkill PID={pid} 异常: {e}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()[:200]
        print(f"[ocsr] ⚠️ {label} taskkill PID={pid} 失败 (rc={proc.returncode}): {detail}",
              file=sys.stderr)
        return False
    return True


def _check_output_landed(
    output: Path,
    snapshot_before: dict[str, tuple[int, int]] | None,
) -> tuple[bool, dict]:
    """检查产物是否有效落盘。"""
    if not output.is_file():
        return (False, {"pre_existed": False, "change": False})
    st = output.stat()
    before_meta = snapshot_before.get(output.name) if snapshot_before else None
    if before_meta is None:
        landed = st.st_size > 0
        return (landed, {"pre_existed": False, "change": landed})
    else:
        changed = (st.st_size, st.st_mtime_ns) != before_meta
        meta = {
            "pre_existed": True,
            "change": changed,
            "size_before": before_meta[0],
            "mtime_ns_before": before_meta[1],
        }
        landed = st.st_size > 0 and changed
        return (landed, meta)


def _watch_loop(
    parsed: list[dict],
    start_times: list[float],
    timeout_min: int,
    progress: bool,
    ledger: Path | None = None,
    task_id: str = "",
    role: str = "ocsr-dispatch",
    plan_ref: str = "",
    scope: str = "",
    blocking_chain: list[str] | None = None,
    snapshot_before: dict[str, tuple[int, int]] | None = None,
    timeout_policy: str = TIMEOUT_POLICY_LEAF_KILL,
    timeout_policy_requested: str = "",
    converge_invocation_id: str = "",
    forbid_paths: list[str] | None = None,
) -> int:
    """双监视：产物落盘 + 进程存活（start.marker exit 行）。

    结案语义（plan A1）：每个 worker 最终落入且仅落入三个集合之一——
    `landed`（产物有效落盘）/ `failed`（确定性失败，已结案）/ `timed_out`（看门狗到期）。
    循环在三者之和覆盖全部 worker 时结束。**`failed` 不再与 `landed` 混用同一集合**：
    历史缺陷是失败分支执行 `landed.add(i)`，致使循环末尾判定「全部落盘」、
    打印成功横幅并返回 0——失败对外表现为成功。

    退出码（与 `refs/dispatch-patterns.md` 的退出码契约同源，混合结局优先级 3 > 1 > 2 > 0）：
    本函数返回 0/1/2；路径碰撞(3) 由 `cmd_dispatch` 在收口时覆盖。
    """
    landed: set[int] = set()
    failed: set[int] = set()
    timed_out: set[int] = set()
    warned_stall: set[int] = set()
    # 每 worker 独立 deadline（口径已定：不是全局 max(start_times) 重算）。
    # DB 锁重派后按该 worker 的新起点顺延它自己的 deadline，其余 worker 不受影响；
    # 若沿用全局 deadline，重派出的 worker 会被陈旧 deadline 秒杀，
    # 使 DB 锁重试机制在实践上失效。
    deadlines: list[float] = [st + timeout_min * 60 for st in start_times]
    check_interval = 10  # 秒
    retried: set[int] = set()          # 已 DB 锁重派过的 worker（控制流，布尔语义）
    retry_count: dict[int, int] = {}   # worker idx → 重试次数（仅供 failure_retry_index 遥测）

    def _settled() -> int:
        return len(landed) + len(failed) + len(timed_out)

    while True:
        now = time.time()

        for i, p in enumerate(parsed):
            if i in landed or i in failed or i in timed_out:
                continue

            wd = p.get("work_dir")
            if wd is None:
                # 缺少 work_dir 则无法做双监视，产物无从验证——归 failed，不得记为成功。
                # 真实派发路径不会触达（cmd_dispatch 恒设 work_dir），属既有过度声明。
                _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                  0, 0, "work_dir missing, cannot verify artifact",
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  task_id=task_id, label=p.get("label", ""),
                                  plan_ref=plan_ref, scope=scope,
                                  blocking_chain=blocking_chain,
                                  outcome_detail="error:work_dir_missing")
                print(f"[ocsr] ❌ {p['label']} 缺少 work_dir，无法验证产物")
                failed.add(i)
                continue

            output = p["output"]
            log_file = wd / "run.log"
            error_file = wd / "error.log"
            start_marker = wd / "start.marker"

            # 检查产物（新文件要求存在+size>0；预存文件要求内容变化）
            is_landed, land_meta = _check_output_landed(output, snapshot_before)
            if is_landed:
                elapsed = (now - start_times[i]) / 60
                verdict = ""
                fm = _parse_frontmatter(output)
                if fm and fm.get("verdict"):
                    verdict = f", verdict={fm['verdict']}"
                # 读路径审计（--forbid-paths 指定时）：报告机制，不改变退出码
                read_audit = ""
                if forbid_paths:
                    read_audit, violation = _audit_output_reads(output, forbid_paths)
                    if read_audit == "violated":
                        audit_detail = f"violated({violation})"
                    elif read_audit == "unavailable":
                        audit_detail = "unavailable(报告未含 reads 段)"
                    else:
                        audit_detail = "clean"
                    print(f"[ocsr] 读路径审计: {p['label']} {audit_detail}")
                cost_info = _lookup_model_cost(p["model"])
                ce = _estimate_cost(p.get("prompt_size_bytes", 0), output.stat().st_size,
                                    cost_info["input"], cost_info["output"])
                _append_telemetry(p["model"], _normalize_role(role or ""), "detached", "success",
                                  round(elapsed, 1), output.stat().st_size,
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  response_size_bytes=output.stat().st_size,
                                  model_cost_input=cost_info["input"],
                                  model_cost_output=cost_info["output"],
                                  cost_estimate=ce,
                                  task_id=task_id or "", label=p.get("label", ""),
                                  plan_ref=plan_ref or "",
                                  scope=scope or "", blocking_chain=blocking_chain or [],
                                  timeout_policy_requested=timeout_policy_requested,
                                  timeout_policy_resolved=timeout_policy,
                                  forbid_paths=len(forbid_paths or []),
                                  read_audit=read_audit)
                landed_row: dict[str, object] = {
                    "event": "landed", "label": p["label"], "model": p["model"],
                    "output": str(output), "bytes": output.stat().st_size,
                    "wall_min": round(elapsed, 1),
                    "pre_existed": land_meta.get("pre_existed", False),
                    "change": land_meta.get("change", True),
                }
                if converge_invocation_id:
                    landed_row["converge_invocation_id"] = converge_invocation_id
                if land_meta.get("pre_existed"):
                    landed_row["size_before"] = land_meta["size_before"]
                    landed_row["mtime_ns_before"] = land_meta["mtime_ns_before"]
                if fm and fm.get("verdict"):
                    landed_row["verdict"] = fm["verdict"]
                _append_dispatch_ledger(ledger, landed_row)
                print(f"[ocsr] ✅ {p['label']} 落盘 ({output.stat().st_size}B, {elapsed:.1f}min{verdict})")
                landed.add(i)
                continue

            # 检查启动错误
            if error_file.is_file():
                err_text = error_file.read_text(encoding="utf-8", errors="replace")[:300]
                elapsed = (now - start_times[i]) / 60
                _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                  round(elapsed, 1), 0, f"launcher error: {err_text}",
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  task_id=task_id, label=p.get("label", ""),
                                  plan_ref=plan_ref,
                                  scope=scope, blocking_chain=blocking_chain)
                _append_dispatch_ledger(ledger, {
                    "event": "failed", "reason": "launcher_error", "label": p["label"],
                    "model": p["model"], "wall_min": round(elapsed, 1), "detail": err_text[:200],
                })
                print(f"[ocsr] ❌ {p['label']} 启动失败: {err_text}")
                failed.add(i)
                continue

            # 进程存活检测（读 start.marker exit 行）
            exit_code = _read_start_marker_exit(start_marker)
            if exit_code is not None:
                elapsed = (now - start_times[i]) / 60
                log_text = log_file.read_text(encoding="utf-8", errors="replace")[:500] if log_file.is_file() else ""

                # DB 锁检测：延迟后自动重派一次（通道例外，SKILL.md §七）
                # `retried` 与 `retry_count` 职责不同、不可合并：
                #   retried    → 控制流，「是否已重派过」的确定性布尔语义
                #   retry_count→ 遥测计数，写入 failure_retry_index
                # 二者合一即重演历史上的 `retry_count[i] = 99` 哨兵式歧义。
                if "database is locked" in log_text.lower() and i not in retried:
                    launcher = wd / "launcher.ps1"
                    if launcher.is_file():
                        retry_count[i] = retry_count.get(i, 0) + 1
                        retried.add(i)
                        _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                          round(elapsed, 1), len(log_text),
                                          f"database is locked, retry #{retry_count[i]}",
                                          prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                          task_id=task_id, label=p.get("label", ""),
                                          plan_ref=plan_ref,
                                          scope=scope, blocking_chain=blocking_chain,
                                          outcome_detail="error:database-locked-retry",
                                          failure_retry_index=retry_count[i])
                        _append_dispatch_ledger(ledger, {
                            "event": "retried", "reason": "database_locked",
                            "label": p["label"], "model": p["model"],
                            "wall_min": round(elapsed, 1), "retry_index": retry_count[i],
                        })
                        print(f"[ocsr] 🔄 {p['label']} DB 锁，{RETRY_DELAY_DB_LOCK}s 后重派 "
                              f"({retry_count[i]}/1)")
                        time.sleep(RETRY_DELAY_DB_LOCK)
                        for old in (start_marker, log_file, error_file):
                            old.unlink(missing_ok=True)
                        # 与初始派发共用 _launch_command：重派会覆盖刷新 pid.txt，
                        # 否则看门狗到期时 taskkill 会打在已死的旧 PID 上。
                        subprocess.run(
                            ["powershell", "-NoProfile", "-Command", _launch_command(wd)],
                            capture_output=True, timeout=30,
                        )
                        start_times[i] = time.time()
                        # 该 worker 的 deadline 按新起点顺延；不顺延则重派出的 worker
                        # 会被陈旧 deadline 秒杀，DB 锁重试机制形同虚设。
                        deadlines[i] = start_times[i] + timeout_min * 60
                        # 本 worker 仍未结案——不加入任何结案集合，循环继续监视它。
                        continue

                # 进程已退出而产物未落盘 —— 无论退出码是否为 0 都是确定性失败。
                # `exit=0 且期望产物未落盘` 是第四条终结路径（§五 越界写入/路径碰撞的指纹）：
                # 历史上它与非零退出走同一分支却被记为 landed，是「失败表现为成功」的主要来源。
                if exit_code == 0:
                    od = "error:exit_0_no_artifact"
                    reason = "opencode_exit_0_no_artifact"
                    human = (f"[ocsr] ❌ {p['label']} opencode 正常退出 (exit=0) 但期望产物未落盘"
                             f" → 优先怀疑写入路径错误（见收口的 path_anomaly 记录）")
                else:
                    od = _parse_outcome_detail("error", exit_code=exit_code, log_text=log_text)
                    reason = f"opencode_exit_{exit_code}"
                    human = f"[ocsr] ❌ {p['label']} opencode 退出 exit={exit_code}"
                _append_telemetry(p["model"], _normalize_role(role), "detached", "error",
                                  round(elapsed, 1), len(log_text),
                                  f"opencode exit={exit_code}, log={log_text[:200]}",
                                  prompt_size_bytes=p.get("prompt_size_bytes", 0),
                                  task_id=task_id, label=p.get("label", ""),
                                  plan_ref=plan_ref,
                                  scope=scope, blocking_chain=blocking_chain,
                                  outcome_detail=od)
                _append_dispatch_ledger(ledger, {
                    "event": "failed", "reason": reason,
                    "label": p["label"], "model": p["model"], "wall_min": round(elapsed, 1),
                    "exit_code": exit_code,
                })
                print(human)
                failed.add(i)
                continue

            # 静默停滞检测
            log_size = log_file.stat().st_size if log_file.is_file() else 0
            stall_threshold = min(8, timeout_min / 2)
            stalled_min = (now - start_times[i]) / 60
            if log_size == 0 and stalled_min >= stall_threshold and i not in warned_stall:
                print(f"[ocsr] ⚠️ {p['label']} 日志 0 字节已 {stalled_min:.0f}min（疑似静默停滞）")
                warned_stall.add(i)

        # 看门狗：逐 worker 用各自的 deadline 判定（非全局 deadline）。
        # 已结案的 worker（landed / failed / timed_out）一律跳过——
        # 否则会对已结案的失败做二次 kill、二次遥测、二次账本写入。
        for i, p in enumerate(parsed):
            if i in landed or i in failed or i in timed_out:
                continue
            if now <= deadlines[i]:
                continue
            wd = p.get("work_dir")
            elapsed = (now - start_times[i]) / 60
            log_size = (wd / "run.log").stat().st_size if wd and (wd / "run.log").is_file() else 0
            if timeout_policy == TIMEOUT_POLICY_LEAF_KILL:
                killed_ok = _kill_worker(p["label"], wd) if wd else False
                if killed_ok:
                    outcome_detail_val = _parse_outcome_detail(
                        "stall", log_text=f"watchdog timeout {timeout_min}min")
                    note_text = f"watchdog timeout {timeout_min}min, killed"
                    progress_text = (f"[ocsr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                     f"→已 kill，日志 {log_size}B")
                else:
                    # `killed:failed` 的定义 = **kill 操作本身失败**，目标进程可能仍在运行；
                    # 它**不**表示「进程已被杀死」。不得降级记为普通 stall——
                    # 那会掩盖「看门狗已放弃止损、而 worker 仍在消耗模型调用」这一事实。
                    outcome_detail_val = "killed:failed"
                    note_text = (f"watchdog timeout {timeout_min}min, "
                                 f"kill FAILED (target process may still be running)")
                    progress_text = (f"[ocsr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                     f"→kill 失败，进程可能仍在运行，日志 {log_size}B")
                event_result = "failed"
                fail_reason = "watchdog_timeout"
            else:
                outcome_detail_val = "reported:alive"
                note_text = f"watchdog timeout {timeout_min}min, reported/alive"
                progress_text = (f"[ocsr] ⏰ {p['label']} 超时 ({timeout_min}min)"
                                 f"→报告/alive（进程保留），日志 {log_size}B")
                event_result = "reported"
                fail_reason = "watchdog_reported"
            _append_telemetry(p["model"], _normalize_role(role), "detached", "stall",
                              round(elapsed, 1), log_size,
                              note_text,
                              prompt_size_bytes=p.get("prompt_size_bytes", 0),
                              task_id=task_id, label=p.get("label", ""),
                              plan_ref=plan_ref,
                              scope=scope, blocking_chain=blocking_chain,
                              outcome_detail=outcome_detail_val,
                              timeout_policy_requested=timeout_policy_requested,
                              timeout_policy_resolved=timeout_policy)
            _append_dispatch_ledger(ledger, {
                "event": event_result, "reason": fail_reason, "label": p["label"],
                "model": p["model"], "wall_min": round(elapsed, 1),
                "timeout_min": timeout_min, "log_bytes": log_size,
                "timeout_policy_requested": timeout_policy_requested,
                "timeout_policy_resolved": timeout_policy,
            })
            print(progress_text)
            timed_out.add(i)

        # 结案判定：三个集合之和覆盖全部 worker 即收口
        if _settled() == len(parsed):
            break

        time.sleep(check_interval)

    # ── 收口与退出码 ────────────────────────────────────────────────
    # 混合结局优先级：看门狗超时(1) > 确定性失败(2) > 全部成功(0)。
    # 路径碰撞(3) 由 cmd_dispatch 在此之后覆盖，优先级最高。
    # 排序理由：确定性失败是「已结案的失败」，排在「未结案失联」之后，
    # 避免已记录的失败掩盖仍在消耗预算的失联进程。
    if timed_out:
        print(f"[ocsr] ❌ 看门狗超时 ({timeout_min}min)：{len(timed_out)}/{len(parsed)} 个 worker 未落盘")
        return 1
    if failed:
        print(f"[ocsr] ❌ {len(failed)}/{len(parsed)} 个 worker 确定性失败，未落盘")
        return EXIT_DETERMINISTIC_FAILURE
    print("[ocsr] ✅ 全部 worker 完成")
    return 0


def cmd_selftest(args) -> int:
    """冒烟测试：生成 trivial prompt → 派发 → 回收 → 验证。"""
    _check_model_calls_disabled()
    model = args.model or "deepseek/deepseek-v4-flash"
    try:
        _validate_model_allowed(model)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    work_dir = Path(args.work_dir or os.environ.get("TEMP", "/tmp")) / "ocsr_selftest"
    work_dir.mkdir(parents=True, exist_ok=True)

    output_dir = Path(args.output_dir) if args.output_dir else work_dir / "output"
    label = "selftest"
    prompt_path = work_dir / "prompt.txt"
    output_path = output_dir / "ocsr-selftest.md"
    output_path.unlink(missing_ok=True)

    _write_utf8(prompt_path, textwrap.dedent(f"""\
        【任务】用 Write 工具写入以下文件（UTF-8）：{output_path.absolute().as_posix()}
        内容：`selftest-ok`
        什么算完成：文件存在且内容等于 selftest-ok

        【输出】{output_path.absolute().as_posix()}

        【边界与禁区】
        - 除输出文件外禁止写入/修改任何文件
        - 不要依赖 stdout 回传。未实际写入文件的响应视为执行失败

        【执行证据】最终回复含：文件路径 + 字节数 + 内容。
    """))

    print(f"[selftest] 模型: {model}")
    print(f"[selftest] 产物预期: {output_path}")
    print(f"[selftest] 派发中...")

    worker_dir = work_dir / "worker"
    worker_dir.mkdir(exist_ok=True)
    _write_utf8(worker_dir / "prompt.txt", prompt_path.read_text(encoding="utf-8"))
    launcher = _pwsh_code(f"-m {model} --title ocsr-selftest")
    _write_utf8(worker_dir / "launcher.ps1", launcher)

    cmd = f'Start-Process pwsh -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","{worker_dir.as_posix()}/launcher.ps1" -WindowStyle Hidden'
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("[selftest] ⚠️ Start-Process 超时")

    start = time.time()
    while time.time() - start < 60:
        if output_path.is_file() and output_path.stat().st_size > 0:
            content = output_path.read_text(encoding="utf-8").strip()
            elapsed = time.time() - start
            if content == "selftest-ok":
                print(f"[selftest] ✅ 通过 ({elapsed:.0f}s, {output_path.stat().st_size}B)")
                output_path.unlink()
                return 0
            else:
                print(f"[selftest] ⚠️ 内容不符: '{content[:50]}' (期望 selftest-ok)")
                output_path.unlink()
                return 1
        time.sleep(3)

    print(f"[selftest] ❌ 超时 60s")
    return 1


def cmd_telemetry(args) -> int:
    """查看遥测摘要。"""
    if not DISPATCH_LOG.is_file():
        print("暂无遥测数据")
        return 0

    rows: list[dict] = []
    with DISPATCH_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if args.all:
        for r in rows:
            print(f"{r.get('ts','?')[:19]} {r.get('model','?'):30s} {r.get('outcome','?'):10s} "
                  f"{r.get('wall_min',0):5.1f}min {r.get('artifact_bytes',0):6d}B "
                  f"{r.get('note','')}")
        return 0

    total = len(rows)
    success = sum(1 for r in rows if r.get("outcome") == "success")
    error = sum(1 for r in rows if r.get("outcome") == "error")
    stall = sum(1 for r in rows if r.get("outcome") == "stall")
    killed = sum(1 for r in rows if r.get("outcome") == "killed")
    walls = [r["wall_min"] for r in rows if r.get("outcome") == "success" and r.get("wall_min")]

    print(f"ocsr 遥测 (since {rows[0].get('ts','?')[:10]})")
    print(f"  总派发: {total}")
    print(f"  成功:   {success}")
    print(f"  错误:   {error}")
    print(f"  停滞:   {stall}")
    print(f"  被杀:   {killed}")
    if walls:
        print(f"  成功耗时: min={min(walls):.1f} max={max(walls):.1f} avg={sum(walls)/len(walls):.1f}min")
    print(f"  日志:    {DISPATCH_LOG}")

    return 0


def cmd_summary(args) -> int:
    """按 group-by 聚合 dispatch-log 并输出汇总。"""
    if not DISPATCH_LOG.is_file():
        print("暂无 dispatch-log 数据")
        return 0

    rows: list[dict] = []
    with DISPATCH_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if args.since:
        try:
            cutoff = datetime.datetime.fromisoformat(args.since)
            if cutoff.tzinfo is None:
                cutoff = cutoff.astimezone()
            rows = [r for r in rows if r.get("ts") and datetime.datetime.fromisoformat(r["ts"]) >= cutoff]
        except (ValueError, TypeError):
            print(f"⚠️ --since 格式无效（ISO 格式）：{args.since}", file=sys.stderr)

    if not rows:
        print("无匹配条目")
        return 0

    group_key = args.group_by or "role"
    groups: dict[str, dict] = {}
    for r in rows:
        raw_key = str(r.get(group_key, "unknown")) if r.get(group_key) is not None else "unknown"
        if group_key == "role":
            if raw_key not in ROLE_VALUES:
                key = ROLE_LEGACY
            else:
                key = raw_key
        else:
            key = raw_key

        g = groups.setdefault(key, {"spawn": 0, "success": 0, "total_wall_min": 0.0, "total_cost_estimate": 0.0})
        g["spawn"] += 1
        if r.get("outcome") == "success":
            g["success"] += 1
        g["total_wall_min"] += float(r.get("wall_min", 0) or 0)
        g["total_cost_estimate"] += float(r.get("cost_estimate", 0) or 0)

    for g in groups.values():
        g["success_rate"] = round(g["success"] / g["spawn"] * 100, 1) if g["spawn"] else 0.0
        g["total_wall_min"] = round(g["total_wall_min"], 1)
        g["total_cost_estimate"] = round(g["total_cost_estimate"], 4)

    if args.format == "json":
        print(json.dumps(groups, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        import io
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([group_key, "spawn", "success_rate", "total_wall_min", "total_cost_estimate"])
        for k, g in sorted(groups.items()):
            w.writerow([k, g["spawn"], f'{g["success_rate"]}%', g["total_wall_min"], g["total_cost_estimate"]])
        print(buf.getvalue().strip())
    else:
        header = f"{'':30s} {'spawn':>7s} {'succ%':>7s} {'wall_min':>9s} {'cost_est':>9s}"
        print(f"\n dispatch-log summary (group-by: {group_key})")
        print(header)
        print("-" * len(header))
        for k, g in sorted(groups.items()):
            print(f"{k:30s} {g['spawn']:7d} {g['success_rate']:6.1f}% {g['total_wall_min']:9.1f} {g['total_cost_estimate']:9.4f}")
        print(f"\n 总条目: {len(rows)} | 日志: {DISPATCH_LOG}")

    return 0


def cmd_monitor(args) -> int:
    watch_dir = Path(args.watch_dir) if args.watch_dir else None
    process_name = args.process_name or None
    stall_minutes = args.stall_minutes
    alert_file = Path(args.alert_file) if args.alert_file else None
    once = args.once
    interval = args.interval_sec

    if not watch_dir and not process_name:
        print("❌ 至少需要 --watch-dir 或 --process-name 之一", file=sys.stderr)
        return 1

    def _check() -> int:
        has_alarm = False
        details = []

        if watch_dir:
            stalled, elapsed = _dir_stall_check(watch_dir, stall_minutes)
            if stalled:
                if elapsed < 0:
                    msg = f"dir-stall: {watch_dir} 不可访问或空目录"
                else:
                    msg = f"dir-stall: {watch_dir} 最后修改于 {elapsed:.1f} 分钟前 (阈值 {stall_minutes}min)"
                print(f"[monitor] ❌ {msg}", file=sys.stderr)
                details.append({"check": "dir-stall", "detail": msg, "stalled_min": round(elapsed, 1) if elapsed >= 0 else -1})
                has_alarm = True
            else:
                print(f"[monitor] ✅ dir-stall: {watch_dir} 正常 (最新修改 {elapsed:.1f} 分钟前)")

        if process_name:
            running = _is_process_running(process_name)
            if not running:
                msg = f"process-down: {process_name} 未运行"
                print(f"[monitor] ❌ {msg}", file=sys.stderr)
                details.append({"check": "process-down", "detail": msg, "stalled_min": -1})
                has_alarm = True
            else:
                print(f"[monitor] ✅ process: {process_name} 运行中")

        if has_alarm and alert_file:
            alert_file.parent.mkdir(parents=True, exist_ok=True)
            for d in details:
                d["ts"] = datetime.datetime.now().astimezone().isoformat()
                with alert_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

        return 1 if has_alarm else 0

    if once:
        return _check()

    alarm_seen = False
    try:
        while True:
            rc = _check()
            if rc != 0:
                alarm_seen = True
            time.sleep(interval)
    except KeyboardInterrupt:
        return 1 if alarm_seen else 0


# ─── verify-ownership 子命令 ──────────────────────────────────────────

def _parse_ownership_table(text: str) -> dict[str, str]:
    """Parse state markdown to extract file→ownership mapping.

    Supports two table formats:
    1. Six-column schema (§十):
       | Phase | Deliverable | File | Owner | Spawn Label | Status |
       File column → path; Owner starts with 'spawned' (incl. 'spawned (mimo)')
       → label = 'spawned:' + Spawn Label (first if comma-separated);
       Owner contains 'self-written' → label = 'self-written';
       File paths may have backticks → stripped.
    2. Legacy two-column Chinese:
       | 文件 | 归属 |
    Returns {file_path: label}.
    """
    ownership: dict[str, str] = {}
    fmt_six: bool = False
    fmt_two: bool = False
    in_table: bool = False

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            fmt_six = fmt_two = in_table = False
            continue

        if not in_table:
            if "File" in stripped and "Spawn Label" in stripped:
                fmt_six = True
                in_table = True
                continue
            if "文件" in stripped and "归属" in stripped:
                fmt_two = True
                in_table = True
                continue
            continue

        if stripped.startswith("|---"):
            continue

        if fmt_six:
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if len(parts) >= 5:
                file_path = parts[2].strip().strip("`")
                owner = parts[3].strip()
                spawn_label = parts[4].strip()
                if owner.startswith("spawned"):
                    first_label = spawn_label.split(",")[0].strip()
                    ownership[file_path] = f"spawned:{first_label}"
                elif "self-written" in owner:
                    ownership[file_path] = "self-written"
        elif fmt_two:
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if len(parts) >= 2 and parts[0] and parts[1]:
                ownership[parts[0]] = parts[1]

    return ownership


def _parse_ledger_records(ledger_path: Path) -> dict[str, dict]:
    """Parse ledger JSONL to extract per-label lifecycle.

    Returns {label: {launched_ts, landed_ts}}.
    """
    records: dict[str, dict] = {}
    try:
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            label = entry.get("label", "")
            if not label:
                continue
            if label not in records:
                records[label] = {"launched_ts": None, "landed_ts": None}
            if entry.get("event") == "launched":
                ts = entry.get("ts", "")
                if ts and records[label]["launched_ts"] is None:
                    records[label]["launched_ts"] = ts
            elif entry.get("event") == "landed":
                ts = entry.get("ts", "")
                if ts:
                    records[label]["landed_ts"] = ts
    except Exception:
        pass
    return records


def _parse_telemetry_records(telemetry_path: Path) -> dict[str, dict]:
    """Parse dispatch-log.jsonl telemetry to extract per-label lifecycle.

    Returns {label: {launched_ts, landed_ts}}.
    landed_ts is always None because telemetry lacks per-event granularity.
    """
    records: dict[str, dict] = {}
    try:
        text = telemetry_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return records
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = entry.get("label", "")
        if not label:
            continue
        ts = entry.get("ts", "")
        if not ts:
            continue
        if label not in records:
            records[label] = {"launched_ts": None, "landed_ts": None}
        if records[label]["launched_ts"] is None:
            records[label]["launched_ts"] = ts
    return records


def _git_status_porcelain(repo: Path) -> list[str]:
    """Run git -C <repo> status --porcelain, return list of changed file paths (relative).

    Expands directory entries (e.g. 'scripts/') to individual files
    because git may report untracked directories instead of their contents.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return []
        files: list[str] = []
        for raw_line in proc.stdout.split("\n"):
            raw_line = raw_line.rstrip("\r\n")
            if not raw_line:
                continue
            path = raw_line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if path.endswith("/"):
                # Expand directory to individual files
                dir_path = repo / path
                if dir_path.is_dir():
                    for fp in sorted(dir_path.rglob("*")):
                        if fp.is_file():
                            rel = fp.relative_to(repo).as_posix()
                            files.append(rel)
            else:
                files.append(path)
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def cmd_verify_ownership(args) -> int:
    """三查验交付物归属完整性/一致性/合理性。"""
    state_path = Path(args.state)
    ledger_path = Path(args.ledger)
    repos = args.repo

    # Input validation
    if not state_path.is_file():
        print(f"❌ State 文件不存在: {state_path}", file=sys.stderr)
        return 2
    if not repos:
        print("❌ 至少需要一个 --repo", file=sys.stderr)
        return 2

    # Parse inputs
    state_text = state_path.read_text(encoding="utf-8", errors="replace")
    ownership = _parse_ownership_table(state_text)

    telemetry_fallback = False
    telemetry_unlabeled = 0
    if not ledger_path.is_file():
        if not DISPATCH_LOG.is_file():
            print(f"❌ 既无 per-dispatch 账本（{ledger_path}），也无全局遥测（{DISPATCH_LOG}）", file=sys.stderr)
            return 2
        print(f"⚠️ per-dispatch 账本不存在（{ledger_path}），回退全局遥测（{DISPATCH_LOG}）", file=sys.stderr)
        print("   注意：全局遥测不含 mtime 窗口合理性检查所需的 landed_ts 精度", file=sys.stderr)
        ledger = _parse_telemetry_records(DISPATCH_LOG)
        telemetry_fallback = True
        try:
            raw_text = DISPATCH_LOG.read_text(encoding="utf-8", errors="replace")
            for raw_line in raw_text.split("\n"):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    if "label" not in entry or not entry.get("label"):
                        telemetry_unlabeled += 1
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    else:
        ledger = _parse_ledger_records(ledger_path)

    # Collect git changes: {relative_path: repo_base}
    all_changed: dict[str, str] = {}
    for repo_str in repos:
        repo_path = Path(repo_str).resolve()
        if not repo_path.is_dir():
            print(f"❌ Repo 目录不存在: {repo_path}", file=sys.stderr)
            return 2
        for f in _git_status_porcelain(repo_path):
            all_changed[f] = str(repo_path)

    issues: list[tuple[str, str]] = []
    suspects: list[tuple[str, str, str]] = []
    unverifiable: list[tuple[str, str, str]] = []
    exit_code = 0

    # Check 1: Completeness — every git-changed file must be in ownership table
    for changed_file, repo_base in sorted(all_changed.items()):
        abs_path = str((Path(repo_base) / changed_file).resolve())
        if changed_file not in ownership and abs_path not in ownership:
            issues.append(("completeness", f"`{changed_file}` (repo: `{repo_base}`)"))
            exit_code = 1

    # Check 2: Consistency — every spawned:label must have ledger record
    # In telemetry fallback mode, split into labeled/unlabeled:
    # - labeled ledger has records → missing label is true consistency error
    # - labeled ledger is empty → claims are unverifiable (historical data without label)
    for file_path, label in sorted(ownership.items()):
        if not label.startswith("spawned:"):
            continue
        spawn_label = label[len("spawned:"):]
        if spawn_label not in ledger:
            if telemetry_fallback and telemetry_unlabeled > 0:
                unverifiable.append((file_path, spawn_label, label))
            else:
                issues.append(("consistency", f"`{file_path}` → `{label}` (ledger 无 `{spawn_label}`)"))
                exit_code = 1

    # Check 3: Reasonableness — spawned file mtime within worker lifetime
    # Skip unverifiable entries (telemetry fallback, no label data)
    unverifiable_labels = {s for _, s, _ in unverifiable}
    for file_path, label in sorted(ownership.items()):
        if not label.startswith("spawned:"):
            continue
        spawn_label = label[len("spawned:"):]
        if spawn_label in unverifiable_labels:
            continue
        rec = ledger.get(spawn_label)
        if not rec:
            continue
        launched_ts = rec.get("launched_ts")
        landed_ts = rec.get("landed_ts")
        if not launched_ts or not landed_ts:
            continue
        try:
            launched_dt = datetime.datetime.fromisoformat(launched_ts)
            landed_dt = datetime.datetime.fromisoformat(landed_ts)
            launched_epoch = launched_dt.timestamp()
            landed_epoch = landed_dt.timestamp()
        except (ValueError, TypeError):
            continue
        # Locate the file: try repo-relative then absolute
        candidates = [Path(file_path)]
        if not Path(file_path).is_absolute():
            for repo_str in repos:
                candidates.append(Path(repo_str) / file_path)
        for candidate in candidates:
            if candidate.is_file():
                mtime = candidate.stat().st_mtime
                if mtime < launched_epoch - 1 or mtime > landed_epoch + 1:
                    mtime_str = datetime.datetime.fromtimestamp(mtime).isoformat()
                    suspects.append((
                        str(candidate), label,
                        f"mtime={mtime_str} 在窗口外 ({launched_ts[:19]} ~ {landed_ts[:19]})"
                    ))
                break

    # Generate markdown report
    report = ["# verify-ownership 报告", ""]
    if telemetry_fallback:
        report.append("> ⚠️ 数据源：全局遥测（per-dispatch 账本缺失）。合理性检查降级——所有 spawned 条目的 landed_ts 不可用，跳过窗口检查。一致性检查仅覆盖有 label 的遥测条目。")
        report.append("")
    if issues:
        report.append("## ❌ 发现问题")
        report.append("")
        report.append("| 类型 | 描述 |")
        report.append("|------|------|")
        for typ, desc in issues:
            report.append(f"| {typ} | {desc} |")
        report.append("")
    else:
        report.append("## ✅ 完整性与一致性通过")
        report.append("")

    if suspects:
        report.append("## ⚠️ 合理性存疑（启发式，不阻断）")
        report.append("")
        report.append("| 文件 | 标签 | 原因 |")
        report.append("|------|------|------|")
        for fp, lbl, reason in suspects:
            report.append(f"| `{fp}` | `{lbl}` | {reason} |")
        report.append("")
    else:
        report.append("## ✅ 合理性检查通过")
        report.append("")

    if unverifiable:
        report.append("## ⚠️ 无法核实（遥测回退模式）")
        report.append("")
        report.append("以下 spawned 条目的 label 在有 label 的遥测记录中未找到，")
        report.append("但遥测中存在无 label 字段的历史条目（2026-07-25 前的数据），")
        report.append("无法确定这些派发是否实际已记录。")
        report.append("")
        report.append("| 文件 | 标签 | 原因 |")
        report.append("|------|------|------|")
        for fp, _, lbl in sorted(unverifiable):
            report.append(
                f"| `{fp}` | `{lbl}` | "
                "遥测条目无 label 字段（2026-07-25 前的历史数据），无法核对该 spawned claim 的派发记录 |"
            )
        report.append("")

    report.append("---")
    report.append(
        f"统计: completeness={sum(1 for t,_ in issues if t=='completeness')} "
        f"consistency={sum(1 for t,_ in issues if t=='consistency')} "
        f"unverifiable={len(unverifiable)} "
        f"suspect={len(suspects)}"
    )
    print("\n".join(report))
    return exit_code


# ─── run 子命令（确定性步骤运行器）───────────────────────────────────

def cmd_run(args) -> int:
    """步骤运行器。当前仅实现 `--validate`（纯离线，不发起任何模型调用）。

    实现层在 `scripts/ocsr_run_spec.py`；本函数只做 CLI 组装与白名单注入，
    依赖方向单向（run_spec 不 import 本模块），避免循环依赖。
    """
    import importlib.util

    mod_path = Path(__file__).resolve().parent / "ocsr_run_spec.py"
    spec_mod_spec = importlib.util.spec_from_file_location("ocsr_run_spec", mod_path)
    if not spec_mod_spec or not spec_mod_spec.loader:
        print(f"❌ 无法加载 {mod_path}", file=sys.stderr)
        return 1
    run_spec = importlib.util.module_from_spec(spec_mod_spec)
    spec_mod_spec.loader.exec_module(run_spec)

    spec_path = Path(args.spec)

    if args.validate:
        try:
            summary = run_spec.validate_file(spec_path, allowed_models=ALLOWED_MODELS)
        except run_spec.SpecError as e:
            print(f"❌ spec 校验失败 — {e}", file=sys.stderr)
            return 1
        if args.format == "json":
            print(run_spec.summary_json(summary))
        else:
            print(run_spec.format_summary(summary))
            print()
            n = len(summary["warnings"])
            print(f"✅ spec 校验通过（{summary['step_count']} 个步骤"
                  + (f"，{n} 条启发式提示）" if n else "）"))
        return 0

    # 执行路径。dispatch 步骤在此接线（阶段 5）：把 spec 的 dispatch 步骤翻译成
    # worker 契约，**在进程内**调用 `_dispatch_batch`（D9），不起子进程。
    def _dispatch_step(step: dict, sid: str, ctx: dict, out_path: Path) -> int:
        render = run_spec.render
        meta = step.get("meta") or {}
        blocking = meta.get("blocking_chain", "")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        worker = {
            "prompt_file": str(render(step["prompt"], ctx)),
            "model": str(render(step["model"], ctx)),
            "label": sid,
            "output": out_path,
        }
        # 注意：step 的 `scope` 是 **runner 的分组计数键**（D3），
        # 与 ocsr 遥测的 `--meta scope` 是两回事，此处刻意不混用。
        return _dispatch_batch(
            [worker],
            output_dir=out_path.parent,
            work_dir=None,
            stagger=int(step.get("stagger", DEFAULT_STAGGER)),
            timeout_min=int(step.get("timeout_min", DEFAULT_TIMEOUT)),
            timeout_policy=str(step.get("timeout_policy", TIMEOUT_POLICY_AUTO)),
            watch=True,
            progress=False,
            ledger_dir=str(render(step["ledger_dir"], ctx)) if step.get("ledger_dir") else None,
            forbid_paths=[str(x) for x in render(step.get("forbid_paths") or [], ctx)],
            role=str(step.get("role") or "ocsr-dispatch"),
            task_id=str(meta.get("task_id", "")),
            plan_ref=str(meta.get("plan_ref", "")),
            scope=str(meta.get("scope", "")),
            blocking_chain=[x.strip() for x in blocking.split(",") if x.strip()],
            converge_invocation_id=str(meta.get("converge-invocation-id", "")),
        )

    answers: dict[str, str] = {}
    for kv in (args.answer or []):
        if "=" not in kv:
            print(f"❌ --answer 格式应为 <step-id>=<option>，实得: {kv}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        answers[k.strip()] = v.strip()

    try:
        return run_spec.execute_file(spec_path, resume=args.resume, answers=answers,
                                     dispatch_fn=_dispatch_step,
                                     allowed_models=ALLOWED_MODELS)
    except run_spec.SpecError as e:
        print(f"❌ spec 校验失败 — {e}", file=sys.stderr)
        return run_spec.EXIT_SPEC_INVALID
    except run_spec.RunHalt as e:
        print(f"⏹ 停机（exit={e.code}）— {e.message}", file=sys.stderr)
        return e.code


# ─── preflight 子命令 ─────────────────────────────────────────────────

def cmd_preflight(args) -> int:
    """批量探测模型通道可用性。用与真实派发相同的机制（opencode run -m <model>）。"""
    _check_model_calls_disabled()
    import uuid
    models = args.model
    for model in models:
        try:
            _validate_model_allowed(model)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    timeout_sec = args.timeout
    work_base = Path(args.work_dir or os.environ.get("TEMP", "/tmp")) / f"ocsr_preflight_{uuid.uuid4().hex[:6]}"
    work_base.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}
    for model in models:
        wd = work_base / model.replace("/", "-")
        wd.mkdir(parents=True, exist_ok=True)
        sentinel = wd / "probe-ok.marker"
        sentinel.unlink(missing_ok=True)
        probe_prompt = (
            f"Reply with exactly the text: PROBE-OK. "
            f"Do not use any tools. Do not write any files."
        )
        _write_utf8(wd / "prompt.txt", probe_prompt)
        model_arg = f'-m {model} --title "ocsr-preflight"'
        launcher = _pwsh_code(model_arg)
        _write_utf8(wd / "launcher.ps1", launcher)
        cmd = (f'Start-Process pwsh -ArgumentList '
               f'\"-NoProfile\",\"-ExecutionPolicy\",\"Bypass\",\"-File\",\"{wd.as_posix()}/launcher.ps1\" '
               f'-WindowStyle Hidden')
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.time() + timeout_sec
        exit_code = None
        log_text = ""
        while time.time() < deadline:
            exit_code = _read_start_marker_exit(wd / "start.marker")
            if exit_code is not None:
                break
            time.sleep(2)
        log_file = wd / "run.log"
        if log_file.is_file():
            log_text = log_file.read_text(encoding="utf-8", errors="replace")[:500]
        if exit_code is None:
            status = "timeout"
        elif exit_code == 0:
            status = "available"
        elif "404" in log_text or "not found" in log_text.lower():
            status = "404"
        else:
            status = f"error:exit_{exit_code}"
        results[model] = status
        print(f"[preflight] {model}: {status}")

    all_ok = all(v == "available" for v in results.values())
    if all_ok:
        print(f"[preflight] 全部 {len(models)} 个模型可用")
        return 0
    else:
        failed = [m for m, s in results.items() if s != "available"]
        print(f"[preflight] {len(failed)}/{len(models)} 个模型不可用: {failed}")
        return 1


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ocsr (OpenCode Subagents Run) 派发后端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              %(prog)s dispatch --worker "r1.txt|model-id|R1" --watch
              %(prog)s selftest
              %(prog)s telemetry
        """),
    )
    sub = parser.add_subparsers(dest="command")

    # dispatch
    p_disp = sub.add_parser("dispatch", help="派发 worker(s)")
    p_disp.add_argument("--worker", action="append", metavar="PROMPT|MODEL|LABEL",
                        help="可多次指定。PROMPT=prompt 文件路径, MODEL=opencode -m 模型 ID, LABEL=标识（| 分隔）")
    p_disp.add_argument("--output-dir", required=True, help="产物输出目录（必须显式指定）")
    p_disp.add_argument("--output-pattern", default="{label}.md",
                        help="产物文件名模式。可用 {date}, {label}, {model}")
    p_disp.add_argument("--stagger", type=int, default=DEFAULT_STAGGER, help=f"错峰间隔秒 (默认 {DEFAULT_STAGGER})")
    p_disp.add_argument("--watch", action="store_true", help="等待产物落盘（内置看门狗）")
    p_disp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"看门狗超时分钟 (默认 {DEFAULT_TIMEOUT})")
    p_disp.add_argument("--timeout-policy", choices=sorted(TIMEOUT_POLICY_VALUES),
                        default=TIMEOUT_POLICY_AUTO,
                        help=f"超时行为策略: {TIMEOUT_POLICY_AUTO}=按角色自动解析（默认），"
                             f"{TIMEOUT_POLICY_LEAF_KILL}=到期 kill 进程，"
                             f"{TIMEOUT_POLICY_HIERARCHICAL_REPORT}=报告/alive 保留进程供 commander 裁决")
    p_disp.add_argument("--progress", action="store_true", help="输出细粒度过程信息（已就绪/错峰/DB锁重试等；启动、落盘、失败、超时等关键生命周期行默认输出）")
    p_disp.add_argument("--work-dir", help="临时工作目录 (默认 $TEMP)")
    p_disp.add_argument("--harness", default="cli",
                        help="派发 harness 标识 (遥测归因用，默认 cli)")
    p_disp.add_argument("--ledger-dir",
                        help="派发账本目录；仅在显式传递时创建账本")
    p_disp.add_argument("--meta", action="append", metavar="KEY=VAL",
                        help="元数据键值对（可多次指定），如 task_id=xxx role=executor "
                             "plan_ref=path blocking_chain=a,b,c scope=outer")
    p_disp.add_argument("--forbid-paths", action="append", metavar="PATH",
                        help="禁止 worker 读取的路径（目录或文件，可多次指定）。"
                             "向 prompt 副本注入禁止块（不改原文件），"
                             "产物落盘后做读路径审计（报告机制，不影响退出码）")

    # selftest
    p_test = sub.add_parser("selftest", help="冒烟测试")
    p_test.add_argument("--model", help="测试用模型 (默认 deepseek/deepseek-v4-flash)")
    p_test.add_argument("--output-dir", help="产物落盘目录")
    p_test.add_argument("--work-dir", help="临时目录")

    # telemetry
    p_tel = sub.add_parser("telemetry", help="查看遥测")
    p_tel.add_argument("--all", action="store_true", help="显示全部条目")

    # summary
    p_sum = sub.add_parser("summary", help="汇总 dispatch-log 元数据（按 role/scope/task_id/plan_ref 分组）")
    p_sum.add_argument("--group-by", default="role", choices=["role", "scope", "task_id", "plan_ref"],
                       help="聚合字段（默认 role）")
    p_sum.add_argument("--since", help="ISO 起始时间，如 2026-07-01")
    p_sum.add_argument("--format", default="table", choices=["json", "csv", "table"],
                       help="输出格式（默认 table）")
    p_sum.set_defaults(func=cmd_summary)

    # monitor
    p_mon = sub.add_parser("monitor", help="持续监视进程/目录活性")
    p_mon.add_argument("--watch-dir", default="", help="监视此目录下文件的最近修改时间")
    p_mon.add_argument("--process-name", default="", help="监视此名称进程的存活（如 opencode.exe）")
    p_mon.add_argument("--stall-minutes", type=int, default=15, help="无修改/进程消失后触发告警的阈值分钟 (默认 15)")
    p_mon.add_argument("--alert-file", default="", help="触发告警时写入此路径（JSONL，追加）")
    p_mon.add_argument("--once", action="store_true", help="单次检测后退出")
    p_mon.add_argument("--interval-sec", type=int, default=30, help="轮询间隔秒数 (默认 30)")

    # verify-ownership
    p_vo = sub.add_parser("verify-ownership", help="三查验交付物归属的完整性/一致性/合理性")
    p_vo.add_argument("--state", required=True, help="orchestrator state 文件路径")
    p_vo.add_argument("--ledger", required=True, help="派发账本 ocsr-dispatch-ledger.jsonl 路径")
    p_vo.add_argument("--repo", required=True, action="append", help="Git 仓库路径（可多次指定）")

    # run
    p_run = sub.add_parser("run", help="确定性步骤运行器（当前仅 --validate 离线校验）")
    p_run.add_argument("--spec", required=True, help="spec 文件路径（YAML）")
    p_run.add_argument("--validate", action="store_true",
                       help="离线干跑：只校验并输出结构化摘要，不发起任何模型调用")
    p_run.add_argument("--format", default="text", choices=["text", "json"],
                       help="摘要输出格式（默认 text）")
    p_run.add_argument("--resume", action="store_true",
                       help="从既有 journal 续跑。started 无 completed 时停机(exit=11)，"
                            "禁止自动重跑")
    p_run.add_argument("--answer", action="append", metavar="STEP=OPTION",
                       help="回答 pause 步骤（可多次）。退出码 10 表示等待裁决")

    # preflight
    p_pre = sub.add_parser("preflight", help="批量探测模型通道可用性")
    p_pre.add_argument("--model", action="append", required=True,
                       metavar="MODEL_ID",
                       help="模型 ID（可多次指定）")
    p_pre.add_argument("--timeout", type=int, default=30,
                       help="单个模型探测超时秒数 (默认 30)")
    p_pre.add_argument("--work-dir", help="临时工作目录 (默认 $TEMP)")

    args = parser.parse_args()

    if args.command == "dispatch":
        return cmd_dispatch(args)
    elif args.command == "selftest":
        return cmd_selftest(args)
    elif args.command == "telemetry":
        return cmd_telemetry(args)
    elif args.command == "summary":
        return cmd_summary(args)
    elif args.command == "monitor":
        return cmd_monitor(args)
    elif args.command == "verify-ownership":
        return cmd_verify_ownership(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "preflight":
        return cmd_preflight(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
