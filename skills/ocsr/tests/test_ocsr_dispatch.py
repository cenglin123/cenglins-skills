"""ocsr_dispatch.py 元数据字段与 summary 命令测试（plan 20260724-OCSR-dispatch-log-task-session）。
也包含 verify-ownership 子命令测试（plan 20260725-headless-orchestrator-resilience 修订案1）。

覆盖字段写入默认值、透传、summary 聚合、legacy 桶、cost 缓存、prompt_size_bytes 两路径、
outcome_detail 解析、verify-ownership 三查正反例。全部模拟，不真实调用模型。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest.mock as mock
from pathlib import Path

import pytest

# Fail-safe: prevent any accidental model call in tests
os.environ["OCSR_DISABLE_MODEL_CALLS"] = "1"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

# ─── 测试 1: 字段写入默认值 ────────────────────────────────────────────
class TestFieldDefaults:
    def test_minimal_call_has_all_new_fields(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="test/model",
                role="ocsr-dispatch",
                channel="detached",
                outcome="success",
                wall_min=1.5,
                artifact_bytes=100,
            )
            rows = []
            with open(mod.DISPATCH_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(json.loads(line))
            assert len(rows) == 1
            r = rows[0]
            assert "task_id" in r and r["task_id"]
            assert "plan_ref" in r and r["plan_ref"] == ""
            assert "scope" in r and r["scope"] == ""
            assert "prompt_size_bytes" in r
            assert "response_size_bytes" in r
            assert "model_cost_input" in r
            assert "model_cost_output" in r
            assert "cost_estimate" in r
            assert "blocking_chain" in r and isinstance(r["blocking_chain"], list)
            assert "outcome_detail" in r
            assert "failure_retry_index" in r

    def test_missing_fields_have_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="test/m", role="executor", channel="fg",
                outcome="success", wall_min=1.0, artifact_bytes=50,
            )
            r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
            assert r.get("blocking_chain") == []
            assert r.get("failure_retry_index") == 0
            assert r.get("response_size_bytes") == 0
            assert r.get("cost_estimate") == 0.0

# ─── 测试 2: 字段透传 ──────────────────────────────────────────────────
class TestFieldPassthrough:
    def test_task_id_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="t/m", role="executor", channel="fg",
                outcome="success", wall_min=1.0, artifact_bytes=10,
                task_id="test-slug__round1__executor",
                plan_ref="docs/plans/active/test.md",
                blocking_chain=["B-1", "B-2"],
                scope="outer",
            )
            r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
            assert r["task_id"] == "test-slug__round1__executor"
            assert r["plan_ref"] == "docs/plans/active/test.md"
            assert r["blocking_chain"] == ["B-1", "B-2"]
            assert r["scope"] == "outer"

    def test_role_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="t/m", role="legacy-role", channel="fg",
                outcome="success", wall_min=1.0, artifact_bytes=10,
            )
            r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
            assert r["role"] == "legacy-role"

    def test_prompt_response_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="t/m", role="reviewer", channel="bg",
                outcome="success", wall_min=2.0, artifact_bytes=200,
                prompt_size_bytes=512,
                response_size_bytes=1024,
            )
            r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
            assert r["prompt_size_bytes"] == 512
            assert r["response_size_bytes"] == 1024

# ─── 测试 3: summary 聚合 ──────────────────────────────────────────────
class TestSummary:
    def test_summary_by_role(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            for role in ("executor", "reviewer", "executor"):
                mod._append_telemetry(
                    model="t/m", role=role, channel="fg",
                    outcome="success", wall_min=1.0, artifact_bytes=10,
                    cost_estimate=0.005,
                )
            class FakeArgs:
                group_by = "role"
                since = None
                format = "json"
            result = mod.cmd_summary(FakeArgs())
            assert result == 0
            data = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
            assert data

    def test_summary_with_since_filter(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="t/m", role="executor", channel="fg",
                outcome="success", wall_min=1.0, artifact_bytes=10,
            )
            class FakeArgsNaive:
                group_by = "role"
                since = "2099-01-01"
                format = "json"
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                result_naive = mod.cmd_summary(FakeArgsNaive())
            assert result_naive == 0
            assert "无匹配条目" in buf.getvalue(), "naive --since 必须正确过滤（tz 归一化）"
            class FakeArgsAware:
                group_by = "role"
                since = "2099-01-01T00:00:00+08:00"
                format = "json"
            buf2 = _io.StringIO()
            with contextlib.redirect_stdout(buf2):
                result_aware = mod.cmd_summary(FakeArgsAware())
            assert result_aware == 0
            assert "无匹配条目" in buf2.getvalue()

    def test_legacy_role_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            mod._append_telemetry(
                model="t/m", role="ocsr-dispatch", channel="fg",
                outcome="success", wall_min=1.0, artifact_bytes=10,
            )
            mod._append_telemetry(
                model="t/m", role="custom-role", channel="fg",
                outcome="error", wall_min=2.0, artifact_bytes=20,
            )
            mod._append_telemetry(
                model="t/m", role="executor", channel="fg",
                outcome="success", wall_min=3.0, artifact_bytes=30,
            )
            class FakeArgs:
                group_by = "role"
                since = None
                format = "json"
            result = mod.cmd_summary(FakeArgs())
            assert result == 0

# ─── 测试 4: legacy 桶 ────────────────────────────────────────────────
class TestLegacyBucket:
    def test_unknown_role_becomes_legacy(self):
        normalized = mod._normalize_role("ocsr-dispatch")
        assert normalized == "legacy"

    def test_known_role_stays(self):
        assert mod._normalize_role("executor") == "executor"
        assert mod._normalize_role("reviewer") == "reviewer"
        assert mod._normalize_role("release-executor") == "release-executor"
        assert mod._normalize_role("arbiter") == "arbiter"

    def test_custom_role_becomes_legacy(self):
        assert mod._normalize_role("my-custom-role") == "legacy"

# ─── 测试 5: cost 缓存 ─────────────────────────────────────────────────
class TestCostCache:
    @pytest.fixture(autouse=True)
    def _mock_tripwire(self):
        with mock.patch.object(mod, "_check_model_calls_disabled"):
            yield

    def test_lookup_returns_dict(self):
        result = mod._lookup_model_cost("non-existent-model")
        assert isinstance(result, dict)
        assert "input" in result
        assert "output" in result

    def test_estimate_formula(self):
        cost = mod._estimate_cost(prompt_bytes=4000, response_bytes=1000,
                                  cost_input=1.0, cost_output=2.0)
        expected = (1.0 * 4000 + 2.0 * 1000) / 4 / 1_000_000
        assert cost == pytest.approx(expected)

# ─── 测试 6: prompt_size_bytes 两条路径 ────────────────────────────────
class TestPromptSize:
    def test_file_path_uses_getsize(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"hello world")
            f.flush()
            path = f.name
        try:
            result = mod._resolve_prompt_size(path)
            assert result == 11
        finally:
            os.unlink(path)

    def test_inline_string_uses_utf8_bytes(self):
        text = "你好世界"
        result = mod._resolve_prompt_size(None, inline_text=text)
        assert result == len(text.encode("utf-8"))

    def test_both_none_returns_zero(self):
        assert mod._resolve_prompt_size(None) == 0

# ─── 测试 7: outcome_detail 解析 ──────────────────────────────────────
class TestOutcomeDetail:
    def test_success(self):
        assert mod._parse_outcome_detail("success") == "success:completed"

    def test_killed_timeout(self):
        assert mod._parse_outcome_detail("killed", log_text="harness timeout") == "killed:harness-timeout"

    def test_error_with_exit_code(self):
        assert mod._parse_outcome_detail("error", exit_code=1) == "error:exit_code_1"

    def test_stall_watchdog(self):
        assert mod._parse_outcome_detail("stall") == "stall:watchdog-timeout"

    def test_stall_db_locked(self):
        assert mod._parse_outcome_detail("stall", log_text="database is locked") == "stall:database-locked"

# ─── 测试 8: 常量完整性 ────────────────────────────────────────────────
class TestConstants:
    def test_role_values_all_strings(self):
        for v in mod.ROLE_VALUES:
            assert isinstance(v, str)
            assert v

    def test_scope_values_all_strings(self):
        for v in mod.SCOPE_VALUES:
            assert isinstance(v, str)
            assert v


# ─── 测试 9: _dir_stall_check ─────────────────────────────────────────
class TestDirStallCheck:
    def test_empty_dir_reports_stalled(self):
        with tempfile.TemporaryDirectory() as td:
            stalled, elapsed = mod._dir_stall_check(Path(td), 15)
            assert stalled is True
            assert elapsed < 0

    def test_recent_file_reports_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.txt"
            p.write_text("hello", encoding="utf-8")
            stalled, elapsed = mod._dir_stall_check(Path(td), 15)
            assert stalled is False
            assert elapsed >= 0

    def test_old_file_reports_stalled(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "old.txt"
            p.write_text("old", encoding="utf-8")
            old = time.time() - 3600
            os.utime(p, (old, old))
            stalled, elapsed = mod._dir_stall_check(Path(td), 15)
            assert stalled is True
            assert elapsed > 15

    def test_nonexistent_path(self):
        stalled, elapsed = mod._dir_stall_check(Path("/nonexistent_path_xyz"), 15)
        assert stalled is True
        assert elapsed < 0

    def test_recursive_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            subdir = Path(td) / "sub"
            subdir.mkdir()
            p = subdir / "deep.txt"
            p.write_text("deep", encoding="utf-8")
            stalled, elapsed = mod._dir_stall_check(Path(td), 15)
            assert stalled is False
            assert elapsed >= 0


# ─── 测试 10: _is_process_running ────────────────────────────────────
class TestProcessRunning:
    def test_opencode_running(self):
        fake_stdout = "Image Name\nopencode.exe"
        with mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=fake_stdout, stderr="")):
            assert mod._is_process_running("opencode.exe") is True

    def test_opencode_not_running(self):
        fake_stdout = "Image Name"
        with mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=fake_stdout, stderr="")):
            assert mod._is_process_running("opencode.exe") is False

    def test_tasklist_timeout(self):
        with mock.patch.object(mod.subprocess, "run", side_effect=subprocess.TimeoutExpired("tasklist", 15)):
            assert mod._is_process_running("opencode.exe") is False

    def test_linux_pgrep(self):
        with mock.patch.object(mod.sys, "platform", "linux"):
            with mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(returncode=0)):
                assert mod._is_process_running("opencode") is True


# ─── 测试 11: cmd_monitor ────────────────────────────────────────────
class TestMonitorCmd:
    def test_once_mode_stalled(self):
        with mock.patch.object(mod, "_dir_stall_check", return_value=(True, 20.0)):
            class FakeArgs:
                watch_dir = "/tmp"
                process_name = ""
                stall_minutes = 15
                alert_file = ""
                once = True
                interval_sec = 30
            assert mod.cmd_monitor(FakeArgs()) == 1

    def test_once_mode_fresh(self):
        with mock.patch.object(mod, "_dir_stall_check", return_value=(False, 2.0)):
            class FakeArgs:
                watch_dir = "/tmp"
                process_name = ""
                stall_minutes = 15
                alert_file = ""
                once = True
                interval_sec = 30
            assert mod.cmd_monitor(FakeArgs()) == 0

    def test_both_modes_enabled(self):
        with mock.patch.object(mod, "_dir_stall_check", return_value=(False, 2.0)) as mock_dir:
            with mock.patch.object(mod, "_is_process_running", return_value=True) as mock_proc:
                class FakeArgs:
                    watch_dir = "/tmp"
                    process_name = "opencode.exe"
                    stall_minutes = 15
                    alert_file = ""
                    once = True
                    interval_sec = 30
                assert mod.cmd_monitor(FakeArgs()) == 0
                assert mock_dir.called
                assert mock_proc.called

    def test_neither_mode_error(self):
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            class FakeArgs:
                watch_dir = ""
                process_name = ""
                stall_minutes = 15
                alert_file = ""
                once = True
                interval_sec = 30
            assert mod.cmd_monitor(FakeArgs()) == 1
            assert "至少需要" in buf.getvalue()

    def test_alert_file_written(self):
        with tempfile.TemporaryDirectory() as td:
            af = Path(td) / "alerts.jsonl"
            with mock.patch.object(mod, "_dir_stall_check", return_value=(True, 20.0)):
                class FakeArgs:
                    watch_dir = td
                    process_name = ""
                    stall_minutes = 15
                    alert_file = str(af)
                    once = True
                    interval_sec = 30
                assert mod.cmd_monitor(FakeArgs()) == 1
                assert af.is_file()
                content = af.read_text(encoding="utf-8")
                assert "dir-stall" in content

    def test_ctrl_c_graceful(self):
        with mock.patch.object(mod, "_dir_stall_check", return_value=(False, 2.0)):
            with mock.patch.object(mod.time, "sleep", side_effect=KeyboardInterrupt()):
                class FakeArgs:
                    watch_dir = "/tmp"
                    process_name = ""
                    stall_minutes = 15
                    alert_file = ""
                    once = False
                    interval_sec = 30
                result = mod.cmd_monitor(FakeArgs())
                assert result == 0


# ─── 测试 12: cmd_monitor args ───────────────────────────────────────
class TestMonitorArgs:
    def test_minimal_args_pass(self):
        class FakeArgs:
            watch_dir = "/tmp"
            process_name = ""
            stall_minutes = 15
            alert_file = ""
            once = True
            interval_sec = 30
        args = FakeArgs()
        assert args.watch_dir == "/tmp"
        assert args.stall_minutes == 15
        assert args.interval_sec == 30
        assert args.once is True


# ─── 测试 13: verify-ownership — 辅助函数 ────────────────────────────
class TestParseOwnershipTable:
    def test_basic_table(self):
        text = """\
# State
## 交付物归属
| 文件 | 归属 |
|------|------|
| src/a.py | spawned:executor-r1 |
| docs/b.md | self-written |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"src/a.py": "spawned:executor-r1", "docs/b.md": "self-written"}

    def test_empty_table_returns_empty(self):
        assert mod._parse_ownership_table("# No table here") == {}

    def test_ignores_non_ownership_tables(self):
        text = """\
| name | value |
|------|-------|
| foo  | bar   |
"""
        assert mod._parse_ownership_table(text) == {}

    def test_partial_rows_skipped(self):
        text = """\
| 文件 | 归属 |
|------|------|
| a.py | spawned:x |
| broken
| b.py | self-written |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"a.py": "spawned:x", "b.py": "self-written"}

    # ── 六列 schema (§十) ──────────────────────────────────────────

    def test_six_column_basic_spawned(self):
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 1 | monitor cmd | scripts/ocsr_dispatch.py | spawned | ph1-monitor | done |
| 2 | E2E results | e2e-results.md | spawned | ph2-e2e | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {
            "scripts/ocsr_dispatch.py": "spawned:ph1-monitor",
            "e2e-results.md": "spawned:ph2-e2e",
        }

    def test_six_column_mimo(self):
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 2 | R1 review | ph2-review-verdict.md | spawned (mimo) | ph2-review | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"ph2-review-verdict.md": "spawned:ph2-review"}

    def test_six_column_self_written(self):
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| — | State files | _orchestrator-state.md | self-written | — | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"_orchestrator-state.md": "self-written"}

    def test_six_column_comma_label(self):
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 2 | SKILL.md §10 | SKILL.md | spawned | ph2-doc, ph2-doc-fix | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"SKILL.md": "spawned:ph2-doc"}

    def test_six_column_backtick_path(self):
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 1 | monitor | `scripts/ocsr_dispatch.py` | spawned | ph1 | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {"scripts/ocsr_dispatch.py": "spawned:ph1"}

    def test_six_column_all_variants(self):
        """Realistic state excerpt mixing spawned, mimo, self-written, commas, backticks."""
        text = """\
# Orchestrator State

## Deliverable Ownership Ledger

| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 0 | research | findings.md | spawned | ph0-research | done |
| 1 | monitor | scripts/ocsr_dispatch.py | spawned | ph1-monitor | done |
| 2 | doc | SKILL.md | spawned | ph2-doc, ph2-doc-fix | done |
| 2 | R1 review | ph2-review-verdict.md | spawned (mimo) | ph2-review | done |
| — | State files | `_orchestrator-state.md` | self-written | — | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {
            "findings.md": "spawned:ph0-research",
            "scripts/ocsr_dispatch.py": "spawned:ph1-monitor",
            "SKILL.md": "spawned:ph2-doc",
            "ph2-review-verdict.md": "spawned:ph2-review",
            "_orchestrator-state.md": "self-written",
        }

    def test_six_column_skips_unknown_owner(self):
        """Owner column without 'spawned' or 'self-written' is skipped."""
        text = """\
| Phase | Deliverable | File | Owner | Spawn Label | Status |
|-------|-------------|------|-------|-------------|--------|
| 1 | x | a.py | unknown | u1 | done |
"""
        result = mod._parse_ownership_table(text)
        assert result == {}


class TestParseLedgerRecords:
    def test_launched_and_landed(self):
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.jsonl"
            lp.write_text(
                '{"ts":"2026-07-25T10:00:00+08:00","event":"launched","label":"r1"}\n'
                '{"ts":"2026-07-25T10:05:00+08:00","event":"landed","label":"r1"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_ledger_records(lp)
            assert "r1" in rec
            assert rec["r1"]["launched_ts"] == "2026-07-25T10:00:00+08:00"
            assert rec["r1"]["landed_ts"] == "2026-07-25T10:05:00+08:00"

    def test_first_launch_wins(self):
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.jsonl"
            lp.write_text(
                '{"ts":"T1","event":"launched","label":"r1"}\n'
                '{"ts":"T2","event":"launched","label":"r1"}\n'
                '{"ts":"T3","event":"landed","label":"r1"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_ledger_records(lp)
            assert rec["r1"]["launched_ts"] == "T1"

    def test_last_landed_wins(self):
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "ledger.jsonl"
            lp.write_text(
                '{"ts":"T1","event":"launched","label":"r1"}\n'
                '{"ts":"T2","event":"landed","label":"r1"}\n'
                '{"ts":"T3","event":"landed","label":"r1"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_ledger_records(lp)
            assert rec["r1"]["landed_ts"] == "T3"

    def test_not_found_label(self):
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "empty.jsonl"
            lp.write_text("", encoding="utf-8")
            rec = mod._parse_ledger_records(lp)
            assert rec == {}

    def test_nonexistent_file(self):
        rec = mod._parse_ledger_records(Path("/nonexistent_xyz/ledger.jsonl"))
        assert rec == {}


class TestGitStatusPorcelain:
    def test_single_modified_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
            (repo / "f.txt").write_text("init", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
            (repo / "f.txt").write_text("modified", encoding="utf-8")
            files = mod._git_status_porcelain(repo)
            assert "f.txt" in files

    def test_untracked_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
            (repo / "new.py").write_text("new", encoding="utf-8")
            files = mod._git_status_porcelain(repo)
            assert "new.py" in files

    def test_clean_repo_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            files = mod._git_status_porcelain(repo)
            assert files == []

    def test_nonexistent_dir_returns_empty(self):
        files = mod._git_status_porcelain(Path("/nonexistent_xyz_repo"))
        assert files == []


# ─── 测试 14: verify-ownership — 命令集成 ──────────────────────────
def _init_repo_with_change(path: Path, filename: str = "changed.py", content: str = "change") -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), capture_output=True)
    (path / filename).write_text(content, encoding="utf-8")


def _make_ledger(path: Path, label: str = "r1",
                 launched: str = "2026-07-25T10:00:00+08:00",
                 landed: str = "2026-07-25T10:05:00+08:00") -> Path:
    lp = path / "ledger.jsonl"
    with lp.open("w", encoding="utf-8") as f:
        f.write(f'{{"ts":"{launched}","event":"launched","label":"{label}"}}\n')
        f.write(f'{{"ts":"{landed}","event":"landed","label":"{label}"}}\n')
    return lp


def _make_state(path: Path, entries: list[tuple[str, str]]) -> Path:
    sp = path / "state.md"
    lines = ["# State", "## 交付物归属", "| 文件 | 归属 |", "|------|------|"]
    for file_path, label in entries:
        lines.append(f"| {file_path} | {label} |")
    sp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sp


class TestVerifyOwnership:
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            _make_ledger(d, "r1")
            _make_state(d, [("changed.py", "spawned:r1")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expected 0, got {rc}\n{buf.getvalue()}"

    def test_completeness_missing(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            _make_ledger(d, "r1")
            _make_state(d, [])  # empty ownership
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 1, f"expected 1, got {rc}\n{buf.getvalue()}"
            assert "completeness" in buf.getvalue()

    def test_consistency_false(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            _make_ledger(d, "r1")
            _make_state(d, [("changed.py", "spawned:r1"), ("ghost.py", "spawned:nonexistent")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 1, f"expected 1, got {rc}\n{buf.getvalue()}"
            assert "consistency" in buf.getvalue()
            assert "nonexistent" in buf.getvalue()

    def test_param_no_state(self):
        class FakeArgs:
            state = "/nonexistent_state.md"
            ledger = "/nonexistent_ledger.jsonl"
            repo = ["/tmp"]
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = mod.cmd_verify_ownership(FakeArgs())
        assert rc == 2

    def test_param_no_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "nonexistent-dispatch-log.jsonl"
            try:
                sp = Path(td) / "state.md"
                sp.write_text("# state", encoding="utf-8")
                class FakeArgs:
                    state = str(sp)
                    ledger = "/nonexistent.jsonl"
                    repo = ["/tmp"]
                import io as _io, contextlib
                buf = _io.StringIO()
                with contextlib.redirect_stderr(buf):
                    rc = mod.cmd_verify_ownership(FakeArgs())
                assert rc == 2
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_param_no_repo(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "state.md"
            sp.write_text("# state", encoding="utf-8")
            lp = Path(td) / "ledger.jsonl"
            lp.write_text("{}", encoding="utf-8")
            class FakeArgs:
                state = str(sp)
                ledger = str(lp)
                repo = None
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 2

    def test_both_failures(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            _make_ledger(d, "r1")
            _make_state(d, [("ghost.py", "spawned:nonexistent")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 1
            assert "completeness" in buf.getvalue()
            assert "consistency" in buf.getvalue()

    def test_suspect_not_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            fp = repo / "changed.py"
            old_mtime = 1000000000  # far past
            os.utime(fp, (old_mtime, old_mtime))
            launched = "2000-01-01T00:00:00+00:00"
            landed = "2000-01-02T00:00:00+00:00"
            _make_ledger(d, "r1", launched=launched, landed=landed)
            _make_state(d, [("changed.py", "spawned:r1")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expect 0 (suspect non-blocking), got {rc}\n{buf.getvalue()}"
            assert "suspect" in buf.getvalue().lower() or "合理性存疑" in buf.getvalue()

    def test_no_git_changes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            _make_ledger(d, "r1")
            _make_state(d, [("unused.py", "spawned:r1")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0

    def test_multi_repo(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r1 = d / "repo1"
            r2 = d / "repo2"
            r1.mkdir()
            r2.mkdir()
            _init_repo_with_change(r1, "a.py")
            _init_repo_with_change(r2, "b.py")
            # Write both labels into same ledger (append mode)
            lp = d / "ledger.jsonl"
            with lp.open("w", encoding="utf-8") as f:
                f.write('{"ts":"2026-07-25T10:00:00+08:00","event":"launched","label":"r1"}\n')
                f.write('{"ts":"2026-07-25T10:05:00+08:00","event":"landed","label":"r1"}\n')
                f.write('{"ts":"2026-07-25T10:10:00+08:00","event":"launched","label":"r2"}\n')
                f.write('{"ts":"2026-07-25T10:15:00+08:00","event":"landed","label":"r2"}\n')
            _make_state(d, [("a.py", "spawned:r1"), ("b.py", "spawned:r2")])
            r1_str, r2_str = str(r1), str(r2)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(lp)
                repo = [r1_str, r2_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expected 0, got {rc}\n{buf.getvalue()}"

    def test_self_written_not_checked_for_consistency(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            _init_repo_with_change(repo, "changed.py")
            _make_ledger(d, "r1")
            _make_state(d, [("changed.py", "self-written")])
            repo_str = str(repo)
            class FakeArgs:
                state = str(d / "state.md")
                ledger = str(d / "ledger.jsonl")
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expected 0, got {rc}\n{buf.getvalue()}"

    def test_happy_path_six_column(self):
        """verify-ownership with §十 six-column state schema - completeness must be 0."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
            # Make an initial commit so git status shows files not directories
            (repo / ".gitkeep").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", ".gitkeep"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
            for p in ["scripts", "tests"]:
                (repo / p).mkdir()
            (repo / "scripts/ocsr_dispatch.py").write_text("change", encoding="utf-8")
            (repo / "tests/test_ocsr_dispatch.py").write_text("change", encoding="utf-8")
            lp = _make_ledger(d, "ph1-monitor")
            sp = d / "state.md"
            sp.write_text(textwrap.dedent("""\
                # Orchestrator State
                | Phase | Deliverable | File | Owner | Spawn Label | Status |
                |-------|-------------|------|-------|-------------|--------|
                | 1 | monitor cmd | scripts/ocsr_dispatch.py | spawned | ph1-monitor | done |
                | 1 | tests | tests/test_ocsr_dispatch.py | spawned | ph1-monitor | done |
            """).strip() + "\n", encoding="utf-8")
            repo_str = str(repo)
            class FakeArgs:
                state = str(sp)
                ledger = str(lp)
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expected 0, got {rc}\n{buf.getvalue()}"
            assert "completeness=0" in buf.getvalue(), f"expected completeness=0\n{buf.getvalue()}"

    def test_happy_path_six_column_with_mimo_and_self_written(self):
        """Six-column with spawned (mimo), self-written, comma labels, backtick paths."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            repo = d / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
            (repo / ".gitkeep").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", ".gitkeep"], cwd=str(repo), capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
            for p in ["scripts", "tests", "docs"]:
                (repo / p).mkdir()
            for f in ["SKILL.md", "docs/CHANGELOG.md", "scripts/ocsr_dispatch.py",
                      "tests/test_ocsr_dispatch.py", "ph2-review-verdict.md"]:
                fp = repo / f
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("change", encoding="utf-8")
            lp = d / "ledger.jsonl"
            with lp.open("w", encoding="utf-8") as f:
                f.write('{"ts":"2026-07-25T10:00:00+08:00","event":"launched","label":"ph2-doc"}\n')
                f.write('{"ts":"2026-07-25T10:05:00+08:00","event":"landed","label":"ph2-doc"}\n')
                f.write('{"ts":"2026-07-25T10:10:00+08:00","event":"launched","label":"ph1-monitor"}\n')
                f.write('{"ts":"2026-07-25T10:15:00+08:00","event":"landed","label":"ph1-monitor"}\n')
                f.write('{"ts":"2026-07-25T10:20:00+08:00","event":"launched","label":"ph2-review"}\n')
                f.write('{"ts":"2026-07-25T10:25:00+08:00","event":"landed","label":"ph2-review"}\n')
            sp = d / "state.md"
            sp.write_text(textwrap.dedent("""\
                # Orchestrator State
                | Phase | Deliverable | File | Owner | Spawn Label | Status |
                |-------|-------------|------|-------|-------------|--------|
                | 1 | monitor cmd | scripts/ocsr_dispatch.py | spawned | ph1-monitor | done |
                | 1 | monitor tests | tests/test_ocsr_dispatch.py | spawned | ph1-monitor | done |
                | 2 | SKILL.md §10 | SKILL.md | spawned | ph2-doc, ph2-doc-fix | done |
                | 2 | R1 review | ph2-review-verdict.md | spawned (mimo) | ph2-review | done |
                | 4 | CHANGELOG | docs/CHANGELOG.md | spawned | ph1-monitor | done |
                | — | State files | `_orchestrator-state.md` | self-written | — | done |
            """).strip() + "\n", encoding="utf-8")
            repo_str = str(repo)
            class FakeArgs:
                state = str(sp)
                ledger = str(lp)
                repo = [repo_str]
            import io as _io, contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.cmd_verify_ownership(FakeArgs())
            assert rc == 0, f"expected 0, got {rc}\n{buf.getvalue()}"
            assert "completeness=0" in buf.getvalue(), f"expected completeness=0\n{buf.getvalue()}"


# ─── 测试 15: 遥测 label 字段 ──────────────────────────────────────────
class TestTelemetryLabel:
    def test_telemetry_contains_label_when_provided(self):
        with tempfile.TemporaryDirectory() as td:
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            try:
                mod._append_telemetry(
                    model="t/m", role="executor", channel="fg",
                    outcome="success", wall_min=1.0, artifact_bytes=10,
                    label="r1",
                )
                r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
                assert r.get("label") == "r1"
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_telemetry_omits_label_when_not_provided(self):
        with tempfile.TemporaryDirectory() as td:
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            try:
                mod._append_telemetry(
                    model="t/m", role="executor", channel="fg",
                    outcome="success", wall_min=1.0, artifact_bytes=10,
                )
                r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
                assert "label" not in r
            finally:
                mod.DISPATCH_LOG = old_dl


# ─── 测试 16: _parse_telemetry_records ───────────────────────────────
class TestParseTelemetryRecords:
    def test_parse_telemetry_with_label(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "telemetry.jsonl"
            tp.write_text(
                '{"ts":"2026-07-25T10:00:00+08:00","label":"r1","outcome":"success"}\n'
                '{"ts":"2026-07-25T10:05:00+08:00","label":"r2","outcome":"error"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_telemetry_records(tp)
            assert "r1" in rec
            assert rec["r1"]["launched_ts"] == "2026-07-25T10:00:00+08:00"
            assert rec["r1"]["landed_ts"] is None
            assert "r2" in rec
            assert rec["r2"]["launched_ts"] == "2026-07-25T10:05:00+08:00"

    def test_parse_telemetry_first_ts_wins(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "telemetry.jsonl"
            tp.write_text(
                '{"ts":"T1","label":"r1","outcome":"success"}\n'
                '{"ts":"T2","label":"r1","outcome":"stall"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_telemetry_records(tp)
            assert rec["r1"]["launched_ts"] == "T1"

    def test_parse_telemetry_skips_entries_without_label(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "telemetry.jsonl"
            tp.write_text(
                '{"ts":"T1","outcome":"success"}\n'
                '{"ts":"T2","label":"r1","outcome":"success"}\n',
                encoding="utf-8",
            )
            rec = mod._parse_telemetry_records(tp)
            assert "r1" in rec
            assert len(rec) == 1

    def test_parse_telemetry_no_label_field_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "telemetry.jsonl"
            tp.write_text('{"ts":"T1","outcome":"success"}\n', encoding="utf-8")
            rec = mod._parse_telemetry_records(tp)
            assert rec == {}

    def test_parse_telemetry_nonexistent_file(self):
        rec = mod._parse_telemetry_records(Path("/nonexistent_xyz/telemetry.jsonl"))
        assert rec == {}


# ─── 测试 17: verify-ownership 遥测回退 ─────────────────────────────
class TestVerifyOwnershipTelemetryFallback:
    def test_fallback_on_missing_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old_dl = mod.DISPATCH_LOG
            dl_path = d / "dispatch-log.jsonl"
            mod.DISPATCH_LOG = dl_path
            try:
                dl_path.write_text(
                    '{"ts":"2026-07-25T10:00:00+08:00","label":"r1","outcome":"success"}\n',
                    encoding="utf-8",
                )
                repo = d / "repo"
                repo.mkdir()
                _init_repo_with_change(repo, "changed.py")
                _make_state(d, [("changed.py", "spawned:r1")])
                nonexistent_ledger = d / "nonexistent-ledger.jsonl"
                repo_str = str(repo)
                class FakeArgs:
                    state = str(d / "state.md")
                    ledger = str(nonexistent_ledger)
                    repo = [repo_str]
                import io as _io, contextlib
                out_buf = _io.StringIO()
                err_buf = _io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    with contextlib.redirect_stderr(err_buf):
                        rc = mod.cmd_verify_ownership(FakeArgs())
                assert rc == 0, f"expected 0, got {rc}\n{out_buf.getvalue()}"
                assert "回退全局遥测" in err_buf.getvalue()
                assert "合理性检查降级" in out_buf.getvalue()
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_fallback_with_consistency_failure(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old_dl = mod.DISPATCH_LOG
            dl_path = d / "dispatch-log.jsonl"
            mod.DISPATCH_LOG = dl_path
            try:
                dl_path.write_text(
                    '{"ts":"2026-07-25T10:00:00+08:00","label":"r1","outcome":"success"}\n',
                    encoding="utf-8",
                )
                repo = d / "repo"
                repo.mkdir()
                _init_repo_with_change(repo, "changed.py")
                _make_state(d, [("changed.py", "spawned:r1"), ("ghost.py", "spawned:nonexistent")])
                nonexistent_ledger = d / "nonexistent-ledger.jsonl"
                repo_str = str(repo)
                class FakeArgs:
                    state = str(d / "state.md")
                    ledger = str(nonexistent_ledger)
                    repo = [repo_str]
                import io as _io, contextlib
                out_buf = _io.StringIO()
                err_buf = _io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    with contextlib.redirect_stderr(err_buf):
                        rc = mod.cmd_verify_ownership(FakeArgs())
                assert rc == 1, f"expected 1, got {rc}\n{out_buf.getvalue()}"
                assert "回退全局遥测" in err_buf.getvalue()
                assert "consistency" in out_buf.getvalue()
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_fallback_all_unlabeled_exit_0(self):
        """All telemetry entries are unlabeled → no consistency error, unverifiable appears, exit 0."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old_dl = mod.DISPATCH_LOG
            dl_path = d / "dispatch-log.jsonl"
            mod.DISPATCH_LOG = dl_path
            try:
                dl_path.write_text(
                    '{"ts":"2026-07-20T10:00:00+08:00","outcome":"success"}\n'
                    '{"ts":"2026-07-20T10:05:00+08:00","outcome":"error"}\n',
                    encoding="utf-8",
                )
                repo = d / "repo"
                repo.mkdir()
                _init_repo_with_change(repo, "changed.py")
                _make_state(d, [("changed.py", "spawned:r1")])
                nonexistent_ledger = d / "nonexistent-ledger.jsonl"
                repo_str = str(repo)
                class FakeArgs:
                    state = str(d / "state.md")
                    ledger = str(nonexistent_ledger)
                    repo = [repo_str]
                import io as _io, contextlib
                out_buf = _io.StringIO()
                err_buf = _io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    with contextlib.redirect_stderr(err_buf):
                        rc = mod.cmd_verify_ownership(FakeArgs())
                assert rc == 0, f"expected 0, got {rc}\n{out_buf.getvalue()}"
                assert "回退全局遥测" in err_buf.getvalue()
                assert "无法核实" in out_buf.getvalue()
                assert "unverifiable=1" in out_buf.getvalue()
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_fallback_only_labeled_with_missing_spawn(self):
        """Telemetry has ONLY labeled entries (no historical unlabeled), spawned label missing → consistency error, exit 1."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old_dl = mod.DISPATCH_LOG
            dl_path = d / "dispatch-log.jsonl"
            mod.DISPATCH_LOG = dl_path
            try:
                dl_path.write_text(
                    '{"ts":"2026-07-25T10:00:00+08:00","label":"r1","outcome":"success"}\n',
                    encoding="utf-8",
                )
                repo = d / "repo"
                repo.mkdir()
                _init_repo_with_change(repo, "changed.py")
                _make_state(d, [("changed.py", "spawned:r1"), ("ghost.py", "spawned:nonexistent")])
                nonexistent_ledger = d / "nonexistent-ledger.jsonl"
                repo_str = str(repo)
                class FakeArgs:
                    state = str(d / "state.md")
                    ledger = str(nonexistent_ledger)
                    repo = [repo_str]
                import io as _io, contextlib
                out_buf = _io.StringIO()
                err_buf = _io.StringIO()
                with contextlib.redirect_stdout(out_buf):
                    with contextlib.redirect_stderr(err_buf):
                        rc = mod.cmd_verify_ownership(FakeArgs())
                assert rc == 1, f"expected 1, got {rc}\n{out_buf.getvalue()}"
                assert "回退全局遥测" in err_buf.getvalue()
                assert "consistency" in out_buf.getvalue()
            finally:
                mod.DISPATCH_LOG = old_dl


# ─── 测试 18: G1 _check_output_landed ──────────────────────────────────
class TestCheckOutputLanded:
    def test_new_file_exists_nonzero_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("content", encoding="utf-8")
            landed, meta = mod._check_output_landed(f, {})
            assert landed is True
            assert meta["pre_existed"] is False
            assert meta["change"] is True

    def test_new_file_zero_size_not_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("", encoding="utf-8")
            landed, meta = mod._check_output_landed(f, {})
            assert landed is False

    def test_new_file_not_exist_not_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "nonexist.md"
            landed, meta = mod._check_output_landed(f, {})
            assert landed is False
            assert meta["change"] is False

    def test_preexisting_no_change_not_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("old content", encoding="utf-8")
            st = f.stat()
            snapshot = {f.name: (st.st_size, st.st_mtime_ns)}
            landed, meta = mod._check_output_landed(f, snapshot)
            assert landed is False
            assert meta["pre_existed"] is True
            assert meta["change"] is False
            assert meta["size_before"] == st.st_size
            assert meta["mtime_ns_before"] == st.st_mtime_ns

    def test_preexisting_with_change_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("old content", encoding="utf-8")
            st = f.stat()
            snapshot = {f.name: (st.st_size, st.st_mtime_ns)}
            time.sleep(0.01)
            f.write_text("new content that is different", encoding="utf-8")
            landed, meta = mod._check_output_landed(f, snapshot)
            assert landed is True
            assert meta["pre_existed"] is True
            assert meta["change"] is True

    def test_preexisting_same_size_diff_mtime_landed(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("AAAAA", encoding="utf-8")
            st = f.stat()
            snapshot = {f.name: (st.st_size, st.st_mtime_ns)}
            time.sleep(0.01)
            f.write_text("BBBBB", encoding="utf-8")
            landed, meta = mod._check_output_landed(f, snapshot)
            assert landed is True
            assert meta["pre_existed"] is True

    def test_none_snapshot_treats_as_new_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "out.md"
            f.write_text("content", encoding="utf-8")
            landed, meta = mod._check_output_landed(f, None)
            assert landed is True
            assert meta["pre_existed"] is False


# ─── 测试 19: G2 占位符注入 ─────────────────────────────────────────────
class TestPlaceholderInjection:
    def test_output_path_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt.txt"
            prompt_file.write_text("Write to {{OUTPUT_PATH}}", encoding="utf-8")
            output = Path(td) / "result.md"
            content = prompt_file.read_text(encoding="utf-8")
            content = content.replace("{{OUTPUT_PATH}}", str(output))
            assert "{{OUTPUT_PATH}}" not in content
            assert str(output) in content

    def test_no_placeholder_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt.txt"
            original = "Just a regular prompt with no placeholders."
            prompt_file.write_text(original, encoding="utf-8")
            content = prompt_file.read_text(encoding="utf-8")
            content = content.replace("{{OUTPUT_PATH}}", str(Path(td) / "out.md"))
            assert content == original


# ─── 测试 20: G3 preflight argparse ────────────────────────────────────
class TestPreflightArgparse:
    def test_preflight_in_subcommands(self):
        old_argv = sys.argv
        sys.argv = ["ocsr_dispatch.py", "preflight", "--help"]
        try:
            mod.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    def test_preflight_no_model_errors(self):
        old_argv = sys.argv
        sys.argv = ["ocsr_dispatch.py", "preflight"]
        try:
            mod.main()
            assert False, "should have exited"
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv


# ─── 测试 21: _watch_loop 集成回归（变量名冲突防护） ──────────────────
class TestWatchLoopNoNameCollision:
    def test_watch_loop_does_not_crash_on_second_iteration(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "worker1"
            wd.mkdir(parents=True)
            parsed = [
                {"output": Path(td) / "out0.md", "label": "w0", "model": "t/m",
                 "prompt_size_bytes": 10, "work_dir": None},
                {"output": Path(td) / "out1.md", "label": "w1", "model": "t/m",
                 "prompt_size_bytes": 10, "work_dir": wd},
            ]
            start_times = [time.time(), time.time()]
            out1 = Path(td) / "out1.md"
            out1.write_text("pre-existing", encoding="utf-8")
            st = out1.stat()
            snapshot = {"out1.md": (st.st_size, st.st_mtime_ns)}
            with mock.patch.object(mod.time, "sleep", lambda x: None):
                rc = mod._watch_loop(
                    parsed, start_times, timeout_min=1, progress=False,
                    ledger=None, snapshot_before=snapshot,
                )
            assert rc == 1, f"expected rc=1 (timeout), got {rc}"


# ─── 测试 22: watchdog timeout policy ───────────────────────────────────
class TestWatchdogTimeoutPolicy:
    def test_leaf_kill_default_kills_process(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "worker1"
            wd.mkdir(parents=True)
            parsed = [
                {"output": Path(td) / "out.md", "label": "w0", "model": "t/m",
                 "prompt_size_bytes": 10, "work_dir": wd},
            ]
            start_times = [time.time() - 120]
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            kill_called = []
            def fake_kill(label, worker_dir):
                kill_called.append((label, worker_dir))
            with mock.patch.object(mod, "_kill_worker", side_effect=fake_kill):
                with mock.patch.object(mod.time, "sleep", lambda x: None):
                    try:
                        rc = mod._watch_loop(
                            parsed, start_times, timeout_min=1, progress=False,
                            timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL,
                        )
                    finally:
                        mod.DISPATCH_LOG = old_dl
            assert rc == 1
            assert len(kill_called) == 1, f"expected kill called, got {kill_called}"
            assert kill_called[0][0] == "w0"

    def test_hierarchical_report_does_not_kill(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td) / "worker1"
            wd.mkdir(parents=True)
            parsed = [
                {"output": Path(td) / "out.md", "label": "w0", "model": "t/m",
                 "prompt_size_bytes": 10, "work_dir": wd},
            ]
            start_times = [time.time() - 120]
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            kill_called = []
            telemetry_rows = []
            def fake_kill(label, worker_dir):
                kill_called.append((label, worker_dir))
            orig_telemetry = mod._append_telemetry
            def tracking_telemetry(*a, **kw):
                telemetry_rows.append((a, kw))
                orig_telemetry(*a, **kw)
            with mock.patch.object(mod, "_kill_worker", side_effect=fake_kill):
                with mock.patch.object(mod, "_append_telemetry", side_effect=tracking_telemetry):
                    with mock.patch.object(mod.time, "sleep", lambda x: None):
                        try:
                            rc = mod._watch_loop(
                                parsed, start_times, timeout_min=1, progress=False,
                                timeout_policy=mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT,
                            )
                        finally:
                            mod.DISPATCH_LOG = old_dl
            assert rc == 1
            assert len(kill_called) == 0, f"expected no kill, got {kill_called}"
            # Check telemetry outcome_detail is reported:alive not stall:watchdog-timeout
            found_reported = False
            for args, kwargs in telemetry_rows:
                od = kwargs.get("outcome_detail", "")
                if "reported:alive" in od:
                    found_reported = True
            assert found_reported, f"expected reported:alive in telemetry, got {telemetry_rows}"

    def test_default_policy_is_auto(self):
        """Default timeout policy is auto, with all three constants defined."""
        assert mod.TIMEOUT_POLICY_AUTO == "auto"
        assert mod.TIMEOUT_POLICY_LEAF_KILL == "leaf_kill"
        assert mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT == "hierarchical_report"
        assert mod.TIMEOUT_POLICY_AUTO in mod.TIMEOUT_POLICY_VALUES

    def test_selftest_compatible_with_default_policy(self):
        """Selftest still works with default auto policy (integration smoke)."""
        assert mod.DEFAULT_TIMEOUT == 15


# ─── 测试 23: 模型白名单 ────────────────────────────────────────────
class TestModelAllowlist:
    """ALLOWED_MODELS 与 _validate_model_allowed 确定性测试（不启动模型）。"""

    @pytest.fixture(autouse=True)
    def _mock_tripwire(self):
        with mock.patch.object(mod, "_check_model_calls_disabled"):
            yield

    ALLOWED = [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2.5-pro",
    ]
    DISALLOWED = [
        "deepseek/deepseek-v3",
        "deepseek/deepseek-r1",
        "xiaomi/mimo-v2.5-pro-ultraspeed",
        "gpt-4o",
        "claude-sonnet-4-20250514",
    ]

    def test_all_allowed_ids_accepted(self):
        for model in self.ALLOWED:
            mod._validate_model_allowed(model)  # must not raise

    def test_deepseek_not_in_allowlist_rejected(self):
        for model in self.DISALLOWED:
            with pytest.raises(ValueError, match="not in the OCSR allowlist"):
                mod._validate_model_allowed(model)

    def test_mimo_ultraspeed_rejected(self):
        with pytest.raises(ValueError, match="not in the OCSR allowlist"):
            mod._validate_model_allowed("xiaomi/mimo-v2.5-pro-ultraspeed")

    def test_allowed_models_is_frozenset(self):
        assert isinstance(mod.ALLOWED_MODELS, frozenset)
        assert len(mod.ALLOWED_MODELS) == 4

    def test_allowed_models_exact_set(self):
        assert mod.ALLOWED_MODELS == frozenset(self.ALLOWED)

    def test_dispatch_rejects_before_launcher(self):
        """dispatch must reject disallowed model before creating any launcher."""
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt.txt"
            prompt_file.write_text("test", encoding="utf-8")
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            class FakeArgs:
                pass
            args = FakeArgs()
            args.worker = [f"{prompt_file}|{self.DISALLOWED[2]}|test"]
            args.output_dir = str(out_dir)
            args.output_pattern = "{label}.md"
            args.stagger = 0
            args.timeout = 15
            args.timeout_policy = mod.TIMEOUT_POLICY_LEAF_KILL
            args.watch = False
            args.progress = False
            args.work_dir = td
            args.harness = "test"
            args.meta = []
            args.ledger_dir = None
            rc = mod.cmd_dispatch(args)
            assert rc == 1, f"expected rc=1 for rejected model, got {rc}"
            # No launcher files should exist
            launcher_dirs = list(Path(td).rglob("launcher.ps1"))
            assert len(launcher_dirs) == 0, \
                f"launchers created despite rejected model: {launcher_dirs}"

    def test_selftest_rejects_disallowed(self):
        """selftest --model must reject disallowed model before any work_dir setup."""
        class FakeArgs:
            model = self.DISALLOWED[2]
            output_dir = None
            work_dir = None
        rc = mod.cmd_selftest(FakeArgs())
        assert rc == 1, f"expected rc=1 for rejected model, got {rc}"

    def test_preflight_rejects_disallowed(self):
        """preflight --model must reject disallowed model before any probe."""
        class FakeArgs:
            model = self.DISALLOWED[:1]
            timeout = 30
            work_dir = None
        rc = mod.cmd_preflight(FakeArgs())
        assert rc == 1, f"expected rc=1 for rejected model, got {rc}"


# ─── 测试 24: 模型调用 tripwire ─────────────────────────────────────────
class TestModelCallsTripwire:
    """OCSR_DISABLE_MODEL_CALLS=1 必须阻止 dispatch/selftest/preflight 在副作用前退出。"""

    def test_tripwire_env_var_checked_at_dispatch_entry(self):
        with mock.patch.object(mod, "_check_model_calls_disabled") as mock_check:
            mock_check.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                mod.cmd_dispatch(mock.Mock(worker=["a|b|c"], output_dir="/tmp"))
            mock_check.assert_called_once()

    def test_tripwire_env_var_checked_at_selftest_entry(self):
        with mock.patch.object(mod, "_check_model_calls_disabled") as mock_check:
            mock_check.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                mod.cmd_selftest(mock.Mock(model="deepseek/deepseek-v4-flash"))
            mock_check.assert_called_once()

    def test_tripwire_env_var_checked_at_preflight_entry(self):
        with mock.patch.object(mod, "_check_model_calls_disabled") as mock_check:
            mock_check.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                mod.cmd_preflight(mock.Mock(model=["deepseek/deepseek-v4-flash"], timeout=30))
            mock_check.assert_called_once()

    def test_tripwire_prints_message(self):
        with mock.patch.object(mod.sys, "stderr") as mock_stderr:
            with mock.patch.dict(os.environ, {"OCSR_DISABLE_MODEL_CALLS": "1"}, clear=False):
                with pytest.raises(SystemExit):
                    mod._check_model_calls_disabled()
                assert mock_stderr.write.called

    def test_tripwire_allows_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mod._check_model_calls_disabled()


# ─── 测试 25: launcher 级模型调用防护 ────────────────────────────────
class TestLauncherDefense:
    """Launcher script must block model execution when OCSR_DISABLE_MODEL_CALLS=1.

    Even if a test bypasses the entry guard in _check_model_calls_disabled,
    the generated .ps1 launcher provides a second layer of defense.
    """

    def test_launcher_contains_env_var_check(self):
        launcher = mod._pwsh_code("-m test/model --title test")
        assert "$env:OCSR_DISABLE_MODEL_CALLS" in launcher
        assert "exit 1" in launcher
        assert "launcher blocked" in launcher.lower()

    def test_launcher_env_check_before_opencode(self):
        launcher = mod._pwsh_code("-m test/model --title test")
        env_check_pos = launcher.find("$env:OCSR_DISABLE_MODEL_CALLS")
        opencode_pos = launcher.find("opencode run")
        assert env_check_pos >= 0, "env var check not found"
        assert opencode_pos >= 0, "opencode run not found"
        assert env_check_pos < opencode_pos, \
            f"env check at {env_check_pos} must come before opencode run at {opencode_pos}"

    def test_launcher_writes_error_log_when_blocked(self):
        """The blocked branch must write to error.log."""
        launcher = mod._pwsh_code("-m test/model --title test")
        assert "error.log" in launcher
        assert "Set-Content" in launcher


# ─── 测试 26: timeout_policy 遥测记录 ──────────────────────────────────
class TestTimeoutPolicyTelemetry:
    """_append_telemetry and _append_dispatch_ledger must record timeout_policy fields."""

    def test_telemetry_records_timeout_policy_when_provided(self):
        with tempfile.TemporaryDirectory() as td:
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            try:
                mod._append_telemetry(
                    model="t/m", role="executor", channel="fg",
                    outcome="success", wall_min=1.0, artifact_bytes=10,
                    timeout_policy_requested=mod.TIMEOUT_POLICY_AUTO,
                    timeout_policy_resolved=mod.TIMEOUT_POLICY_LEAF_KILL,
                )
                r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
                assert r.get("timeout_policy_requested") == mod.TIMEOUT_POLICY_AUTO
                assert r.get("timeout_policy_resolved") == mod.TIMEOUT_POLICY_LEAF_KILL
            finally:
                mod.DISPATCH_LOG = old_dl

    def test_telemetry_omits_timeout_policy_when_empty(self):
        with tempfile.TemporaryDirectory() as td:
            old_dl = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
            try:
                mod._append_telemetry(
                    model="t/m", role="executor", channel="fg",
                    outcome="success", wall_min=1.0, artifact_bytes=10,
                )
                r = json.loads(open(mod.DISPATCH_LOG, encoding="utf-8").readline())
                assert "timeout_policy_requested" not in r
                assert "timeout_policy_resolved" not in r
            finally:
                mod.DISPATCH_LOG = old_dl


# ─── 测试 27: 路径隐私 ─────────────────────────────────────────────────
class TestPathPrivacy:
    """_sanitize_path must replace user-home prefix with <user-home>."""

    def test_sanitize_replaces_user_home(self):
        home = mod._USER_HOME
        test_path = f"{home}\\some\\deep\\path.txt"
        result = mod._sanitize_path(test_path)
        assert result.startswith("<user-home>")
        assert "some/deep/path.txt" in result or "some\\deep\\path.txt" in result

    def test_sanitize_replaces_forward_slash(self):
        home_fwd = mod._USER_HOME_FORWARD
        test_path = f"{home_fwd}/some/deep/path.txt"
        result = mod._sanitize_path(test_path)
        assert result.startswith("<user-home>")

    def test_sanitize_ignores_non_home_path(self):
        result = mod._sanitize_path("C:\\other\\path.txt")
        assert result == "C:\\other\\path.txt"

    def test_sanitize_ledger_row_sanitizes_path_keys(self):
        row = {
            "event": "launched",
            "prompt_file": f"{mod._USER_HOME}\\prompts\\test.txt",
            "expected_output": f"{mod._USER_HOME}\\output\\test.md",
            "work_dir": f"{mod._USER_HOME}\\work\\test",
            "label": "test-label",
            "model": "t/m",
        }
        result = mod._sanitize_ledger_row(row)
        assert result["prompt_file"].startswith("<user-home>")
        assert result["expected_output"].startswith("<user-home>")
        assert result["work_dir"].startswith("<user-home>")
        assert result["label"] == "test-label"
        assert result["model"] == "t/m"


# ─── 测试 29: converge_invocation_id ───────────────────────────────────
class TestConvergeInvocationId:
    """converge-invocation-id 必须写入派发账本的 launched 和 landed 事件。"""

    def test_converge_invocation_id_in_launched_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            row = {
                "event": "launched", "batch_id": "b1", "label": "t", "model": "t/m",
                "harness": "h", "prompt_file": "/p.txt", "expected_output": "/o.md",
                "work_dir": "/w",
            }
            mod._append_dispatch_ledger(ledger, {**row, "converge_invocation_id": "uuid-123"})
            parsed = json.loads(ledger.read_text(encoding="utf-8"))
            assert parsed.get("converge_invocation_id") == "uuid-123"

    def test_converge_invocation_id_in_landed_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            row = {
                "event": "landed", "label": "t", "model": "t/m",
                "output": "/o.md", "bytes": 100, "wall_min": 1.0,
                "pre_existed": False, "change": True,
            }
            mod._append_dispatch_ledger(ledger, {**row, "converge_invocation_id": "uuid-456"})
            parsed = json.loads(ledger.read_text(encoding="utf-8"))
            assert parsed.get("converge_invocation_id") == "uuid-456"

    def test_legacy_ledger_no_correlation_key(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            row = {
                "event": "launched", "batch_id": "b1", "label": "t", "model": "t/m",
                "harness": "h", "prompt_file": "/p.txt", "expected_output": "/o.md",
                "work_dir": "/w",
            }
            mod._append_dispatch_ledger(ledger, row)
            parsed = json.loads(ledger.read_text(encoding="utf-8"))
            assert "converge_invocation_id" not in parsed

    def test_correlation_key_is_uuid_not_user_path(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            cid = "550e8400-e29b-41d4-a716-446655440000"
            row = {
                "event": "launched", "batch_id": "b1", "label": "t", "model": "t/m",
                "harness": "h", "prompt_file": "/p.txt", "expected_output": "/o.md",
                "work_dir": "/w", "converge_invocation_id": cid,
            }
            mod._append_dispatch_ledger(ledger, row)
            parsed = json.loads(ledger.read_text(encoding="utf-8"))
            val = parsed.get("converge_invocation_id", "")
            assert "<user-home>" not in val
            assert "/" not in val or val.count("-") >= 4  # UUID shape

    def test_converge_invocation_id_not_sanitized_as_path(self):
        """converge-invocation-id is a UUID, never sanitized as a user path."""
        row = {"converge_invocation_id": "uuid-789", "label": "t"}
        result = mod._sanitize_ledger_row(row)
        assert result["converge_invocation_id"] == "uuid-789"

    def test_ocsr_framework_independence_no_converge_import(self):
        source = SCRIPT.read_text(encoding="utf-8")
        assert "import converge" not in source.lower()


# ─── 测试 30: 自动超时策略解析 ─────────────────────────────────────────
class TestWatchdogAutoPolicy:
    """TIMEOUT_POLICY_AUTO 必须按角色正确解析。"""

    LEAF_KILL_ROLES = [
        "ocsr-dispatch",
        "executor",
        "reviewer",
        "release-executor",
        "outer-reviewer",
        "blind-reviewer",
        "design-reviewer",
        "legacy",
        "unknown-role",
        "",
    ]
    HIERARCHICAL_ROLES = [
        "orchestrator",
        "planner",
        "commander",
        "ultraverge-initial",
        "arbiter",
    ]

    def test_auto_leaf_roles_resolve_to_leaf_kill(self):
        for role in self.LEAF_KILL_ROLES:
            result = mod._resolve_timeout_policy(mod.TIMEOUT_POLICY_AUTO, role)
            assert result == mod.TIMEOUT_POLICY_LEAF_KILL, \
                f"auto + role '{role}' → expected leaf_kill, got {result}"

    def test_auto_hierarchical_roles_resolve_to_hierarchical_report(self):
        for role in self.HIERARCHICAL_ROLES:
            result = mod._resolve_timeout_policy(mod.TIMEOUT_POLICY_AUTO, role)
            assert result == mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT, \
                f"auto + role '{role}' → expected hierarchical_report, got {result}"

    def test_explicit_leaf_kill_bypasses_auto(self):
        for role in self.LEAF_KILL_ROLES + self.HIERARCHICAL_ROLES:
            result = mod._resolve_timeout_policy(mod.TIMEOUT_POLICY_LEAF_KILL, role)
            assert result == mod.TIMEOUT_POLICY_LEAF_KILL, \
                f"leaf_kill + role '{role}' → expected leaf_kill, got {result}"

    def test_explicit_hierarchical_report_bypasses_auto(self):
        for role in self.LEAF_KILL_ROLES + self.HIERARCHICAL_ROLES:
            result = mod._resolve_timeout_policy(mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT, role)
            assert result == mod.TIMEOUT_POLICY_HIERARCHICAL_REPORT, \
                f"hierarchical_report + role '{role}' → expected hierarchical_report, got {result}"

    def test_auto_is_default_policy(self):
        """--timeout-policy 的默认值必须是 auto。"""
        assert mod.TIMEOUT_POLICY_AUTO in mod.TIMEOUT_POLICY_VALUES


# ─── 测试 31: --forbid-paths 禁止块注入 ───────────────────────────────
class TestForbidBlockInjection:
    """禁止块：列出全部禁止路径 + reads 要求；注入 prompt 副本，不改原文件。"""

    def test_block_contains_all_paths_and_reads_requirement(self):
        block = mod._build_forbid_block(["C:/work/reports", "D:\\tmp\\old.md"])
        assert "C:/work/reports" in block
        assert "D:/tmp/old.md" in block  # 反斜杠归一化为正斜杠
        assert "禁止读取" in block
        assert "reads:" in block

    def test_dispatch_injects_block_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt-src.txt"
            original = "评审任务：检查实现。\n"
            prompt_file.write_text(original, encoding="utf-8")
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            class FakeArgs:
                pass
            args = FakeArgs()
            args.worker = [f"{prompt_file}|deepseek/deepseek-v4-flash|r1"]
            args.output_dir = str(out_dir)
            args.output_pattern = "{label}.md"
            args.stagger = 0
            args.timeout = 15
            args.timeout_policy = mod.TIMEOUT_POLICY_LEAF_KILL
            args.watch = False
            args.progress = False
            args.work_dir = td
            args.harness = "test"
            args.meta = []
            args.ledger_dir = None
            args.forbid_paths = ["C:/work/reports", "D:/tmp/old.md"]
            with mock.patch.object(mod, "_check_model_calls_disabled"):
                with mock.patch.object(mod, "_lookup_model_cost",
                                       return_value={"input": 0.0, "output": 0.0}):
                    with mock.patch.object(mod.subprocess, "run",
                                           return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                        rc = mod.cmd_dispatch(args)
            assert rc == 0, f"expected rc=0, got {rc}"
            # 原 prompt 文件未被改动
            assert prompt_file.read_text(encoding="utf-8") == original
            # work-dir 下的注入版含全部禁止路径与 reads 要求
            copies = [p for p in Path(td).rglob("prompt.txt")]
            assert len(copies) == 1, f"expected 1 injected prompt copy, got {copies}"
            content = copies[0].read_text(encoding="utf-8")
            assert original.strip() in content
            assert "C:/work/reports" in content
            assert "D:/tmp/old.md" in content
            assert "禁止读取" in content
            assert "reads:" in content

    def test_dispatch_without_forbid_paths_no_block(self):
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt-src.txt"
            original = "执行任务。\n"
            prompt_file.write_text(original, encoding="utf-8")
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            class FakeArgs:
                pass
            args = FakeArgs()
            args.worker = [f"{prompt_file}|deepseek/deepseek-v4-flash|r1"]
            args.output_dir = str(out_dir)
            args.output_pattern = "{label}.md"
            args.stagger = 0
            args.timeout = 15
            args.timeout_policy = mod.TIMEOUT_POLICY_LEAF_KILL
            args.watch = False
            args.progress = False
            args.work_dir = td
            args.harness = "test"
            args.meta = []
            args.ledger_dir = None
            # FakeArgs 不设 forbid_paths：getattr 回退必须生效
            with mock.patch.object(mod, "_check_model_calls_disabled"):
                with mock.patch.object(mod, "_lookup_model_cost",
                                       return_value={"input": 0.0, "output": 0.0}):
                    with mock.patch.object(mod.subprocess, "run",
                                           return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                        rc = mod.cmd_dispatch(args)
            assert rc == 0
            copies = [p for p in Path(td).rglob("prompt.txt")]
            assert len(copies) == 1
            content = copies[0].read_text(encoding="utf-8")
            assert "禁止读取" not in content


# ─── 测试 32: 读路径解析与比对 ─────────────────────────────────────────
class TestReadPathAudit:
    """_parse_reads_list / _audit_output_reads：宽松解析 + 归一化对照。"""

    def test_parse_reads_basic(self):
        text = "# 报告\n## 执行证据\nreads:\n  - C:/work/src/main.py\n  - D:/docs/plan.md\n"
        reads = mod._parse_reads_list(text)
        assert reads == ["C:/work/src/main.py", "D:/docs/plan.md"]

    def test_parse_reads_tolerates_indent_and_windows_paths(self):
        text = "reads:\n    - c:\\Work\\Reports\\round-1.md\n\t- D:/docs/a.md\n"
        reads = mod._parse_reads_list(text)
        assert reads == ["c:\\Work\\Reports\\round-1.md", "D:/docs/a.md"]

    def test_parse_reads_stops_at_next_key(self):
        text = "reads:\n  - C:/a.py\nwrites:\n  - C:/b.py\n"
        reads = mod._parse_reads_list(text)
        assert reads == ["C:/a.py"]

    def test_parse_reads_missing_returns_none(self):
        assert mod._parse_reads_list("# 报告\n没有 reads 段\n") is None

    def test_parse_reads_inline_list(self):
        assert mod._parse_reads_list("reads: [C:/a.py, C:/b.py]\n") == ["C:/a.py", "C:/b.py"]

    def test_audit_clean(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("reads:\n  - C:/work/src/main.py\n", encoding="utf-8")
            status, violation = mod._audit_output_reads(out, ["C:/work/reports"])
            assert status == "clean"
            assert violation == ""

    def test_audit_violated_backslash_and_case_variant(self):
        """Windows 反斜杠 + 大小写变体必须命中（归一化对照）。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("reads:\n  - c:\\work\\reports\\round-1.md\n", encoding="utf-8")
            status, violation = mod._audit_output_reads(out, ["C:/Work/Reports"])
            assert status == "violated"
            assert "round-1.md" in violation

    def test_audit_subpath_counts_as_hit(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("reads:\n  - C:/work/reports/deep/nested/x.md\n", encoding="utf-8")
            status, _ = mod._audit_output_reads(out, ["C:/work/reports"])
            assert status == "violated"

    def test_audit_exact_file_hit(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("reads:\n  - C:/work/secret.md\n", encoding="utf-8")
            status, _ = mod._audit_output_reads(out, ["C:/work/secret.md"])
            assert status == "violated"

    def test_audit_sibling_prefix_not_hit(self):
        """前缀相似但非子路径（reports-x vs reports）不得误判。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("reads:\n  - C:/work/reports-x/a.md\n", encoding="utf-8")
            status, _ = mod._audit_output_reads(out, ["C:/work/reports"])
            assert status == "clean"

    def test_audit_unavailable_without_reads(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r1.md"
            out.write_text("# 报告\n无 reads 段。\n", encoding="utf-8")
            status, violation = mod._audit_output_reads(out, ["C:/work/reports"])
            assert status == "unavailable"
            assert violation == ""


# ─── 测试 33: watch 落盘后读路径审计 + 遥测 ────────────────────────────
class TestWatchReadAudit:
    """_watch_loop 落盘分支：审计输出 + 遥测 read_audit，且不改变退出码。"""

    def _run_watch(self, td: str, artifact: str, forbid: list[str]):
        wd = Path(td) / "worker1"
        wd.mkdir(parents=True)
        output = Path(td) / "out.md"
        output.write_text(artifact, encoding="utf-8")
        parsed = [
            {"output": output, "label": "w0", "model": "t/m",
             "prompt_size_bytes": 10, "work_dir": wd},
        ]
        old_dl = mod.DISPATCH_LOG
        mod.DISPATCH_LOG = Path(td) / "dispatch-log.jsonl"
        import io as _io, contextlib
        buf = _io.StringIO()
        try:
            with mock.patch.object(mod, "_lookup_model_cost",
                                   return_value={"input": 0.0, "output": 0.0}):
                with mock.patch.object(mod.time, "sleep", lambda x: None):
                    with contextlib.redirect_stdout(buf):
                        rc = mod._watch_loop(
                            parsed, [time.time()], timeout_min=1, progress=False,
                            forbid_paths=forbid,
                        )
        finally:
            mod.DISPATCH_LOG = old_dl
        rows = [json.loads(l) for l in
                (Path(td) / "dispatch-log.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        return rc, buf.getvalue(), rows

    def test_audit_clean(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, rows = self._run_watch(
                td, "# 报告\nreads:\n  - C:/work/src/main.py\n", ["C:/work/reports"])
            assert rc == 0
            assert "[ocsr] 读路径审计: w0 clean" in out
            assert rows[0].get("read_audit") == "clean"
            assert rows[0].get("forbid_paths") == 1

    def test_audit_violated_windows_variant(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, rows = self._run_watch(
                td, "# 报告\nreads:\n  - c:\\work\\reports\\round-1.md\n", ["C:/Work/Reports"])
            # 审计是报告机制：不改变退出码
            assert rc == 0
            assert "[ocsr] 读路径审计: w0 violated(" in out
            assert "round-1.md" in out
            assert rows[0].get("read_audit") == "violated"

    def test_audit_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, rows = self._run_watch(
                td, "# 报告\n没有 reads 段。\n", ["C:/work/reports"])
            assert rc == 0
            assert "[ocsr] 读路径审计: w0 unavailable(报告未含 reads 段)" in out
            assert rows[0].get("read_audit") == "unavailable"

    def test_no_forbid_paths_no_audit(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, rows = self._run_watch(td, "# 报告\n", [])
            assert rc == 0
            assert "读路径审计" not in out
            assert "read_audit" not in rows[0]
            assert "forbid_paths" not in rows[0]
