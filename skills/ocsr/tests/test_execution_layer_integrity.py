"""执行层验收语义回归测试（plan 20260809-execution-layer-integrity Phase 1）。

覆盖三条 P0：
  A1 `_watch_loop` 失败结案与退出码契约（0/1/2，混合结局优先级 1>2>0）
  A2 DB 锁重派不得提前宣布成功、必须刷新 pid.txt、只重派一次
  A3 看门狗按 PID 终止且校验 taskkill 退出码（含一条真起进程的离线集成测试）

全部离线，不触发任何模型调用。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

import pytest

# Fail-safe: 防止任何意外的模型调用
os.environ["OCSR_DISABLE_MODEL_CALLS"] = "1"

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ocsr_dispatch.py"
SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_eli", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def _worker(td: Path, name: str, *, marker: str | None = None, log: str = "",
            error: str | None = None, artifact: str | None = None,
            launcher: bool = False) -> dict:
    wd = td / f"wd-{name}"
    wd.mkdir(parents=True, exist_ok=True)
    if marker is not None:
        (wd / "start.marker").write_text(marker, encoding="utf-8")
    if log:
        (wd / "run.log").write_text(log, encoding="utf-8")
    if error is not None:
        (wd / "error.log").write_text(error, encoding="utf-8")
    if launcher:
        (wd / "launcher.ps1").write_text("noop", encoding="utf-8")
    out = td / f"{name}.md"
    if artifact is not None:
        out.write_text(artifact, encoding="utf-8")
    return {"output": out, "label": name, "model": "deepseek/deepseek-v4-flash",
            "prompt_size_bytes": 10, "work_dir": wd}


class _FakeClock:
    """可控时钟：`sleep` 只推进虚拟时间，不耗墙钟。

    必须用假时钟而非单纯 mock 掉 `sleep`——`_watch_loop` 的 deadline 判定读的是
    `time.time()`，只 mock `sleep` 会让它对着真实时钟忙等（DB 锁重派后 deadline
    还会顺延一整个 timeout 窗口）。假时钟让 deadline 逻辑仍被真实覆盖。
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(float(seconds), 1.0)


def _run_watch(td: Path, parsed: list[dict], *, timeout_min: int = 5,
               started_ago: float = 0.0, kill_ok: bool = True,
               subprocess_spy=None, **kw):
    """跑 _watch_loop，隔离遥测日志、外部副作用与真实时钟。"""
    old_log = mod.DISPATCH_LOG
    mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
    clock = _FakeClock()
    start_times = [clock.time() - started_ago for _ in parsed]
    runner = subprocess_spy if subprocess_spy is not None else (lambda *a, **k: None)
    try:
        with mock.patch.object(mod.time, "time", clock.time), \
             mock.patch.object(mod.time, "sleep", clock.sleep), \
             mock.patch.object(mod.subprocess, "run", runner), \
             mock.patch.object(mod, "_lookup_model_cost",
                               lambda _m: {"input": 0.0, "output": 0.0}), \
             mock.patch.object(mod, "_kill_worker", lambda _l, _w: kill_ok):
            rc = mod._watch_loop(parsed, start_times, timeout_min=timeout_min,
                                 progress=False, **kw)
        rows = []
        if mod.DISPATCH_LOG.is_file():
            rows = [json.loads(l) for l in
                    mod.DISPATCH_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rc, rows
    finally:
        mod.DISPATCH_LOG = old_log


# ─── A1 · 退出码契约 ─────────────────────────────────────────────────
class TestExitCodeContract:
    """dispatch --watch 的退出码与 stdout 必须忠实反映真实结果。

    历史缺陷：`_watch_loop` 用单一 `landed` 集合混淆「落盘」与「失败结案」，
    失败分支执行 `landed.add(i)`，致循环末尾判定「全部落盘」、
    打印「✅ 全部 worker 完成」并返回 0 —— 失败对外表现为成功。
    """

    def test_nonzero_exit_zero_artifact_returns_2(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", marker="exit=1\n", log="boom")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "全部 worker 完成" not in capsys.readouterr().out

    def test_zero_exit_zero_artifact_returns_2(self, capsys):
        """exit=0 但期望产物未落盘 —— §五 越界写入/路径碰撞的指纹，必须算失败。

        `_watch_loop` 只判 `exit_code is not None`，exit=0 与非零走同一分支；
        契约若只写「非零退出」，这条真实终结路径就会继续表现为成功。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", marker="exit=0\n", log="wrote nothing")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            out = capsys.readouterr().out
            assert "全部 worker 完成" not in out
            assert "exit=0" in out

    def test_launcher_error_returns_2(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", error="launcher blew up")])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert "全部 worker 完成" not in capsys.readouterr().out

    def test_missing_work_dir_returns_2(self):
        """work_dir 缺失 → 无法双监视、产物无从验证 → 归 failed。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p = _worker(td, "w0")
            p["work_dir"] = None
            rc, _ = _run_watch(td, [p])
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_all_landed_returns_0(self, capsys):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "w0", artifact="real content")])
            assert rc == 0
            assert "全部 worker 完成" in capsys.readouterr().out

    def test_partial_landed_partial_failed_returns_2(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "ok", artifact="content"),
                      _worker(td, "bad", marker="exit=1\n", log="boom")]
            rc, _ = _run_watch(td, parsed)
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_failure_plus_timeout_returns_1(self):
        """混合结局优先级：看门狗超时(1) 优先于确定性失败(2)。

        确定性失败是「已结案的失败」；未结案失联的进程仍可能在消耗预算，
        不能被已记录的失败掩盖。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "bad", marker="exit=1\n", log="boom"),
                      _worker(td, "slow")]  # 无 marker → 永不结案 → 超时
            rc, _ = _run_watch(td, parsed, timeout_min=1, started_ago=600)
            assert rc == 1

    def test_timeout_alone_returns_1(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [_worker(td, "slow")], timeout_min=1, started_ago=600)
            assert rc == 1

    def test_settled_failure_not_double_killed(self):
        """已结案的 failed worker 不得在 deadline 分支被二次 kill / 二次遥测。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            killed: list[str] = []
            old_log = mod.DISPATCH_LOG
            mod.DISPATCH_LOG = td / "dispatch-log.jsonl"
            clock = _FakeClock()
            parsed = [_worker(td, "bad", marker="exit=1\n", log="boom")]
            try:
                with mock.patch.object(mod.time, "time", clock.time), \
                     mock.patch.object(mod.time, "sleep", clock.sleep), \
                     mock.patch.object(mod, "_kill_worker",
                                       lambda label, _w: (killed.append(label), True)[1]):
                    rc = mod._watch_loop(parsed, [clock.time() - 600], timeout_min=1,
                                         progress=False)
            finally:
                mod.DISPATCH_LOG = old_log
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE
            assert killed == [], f"已结案的 worker 被二次 kill: {killed}"

    def test_exit_zero_no_artifact_has_own_outcome_detail(self):
        """exit=0 零产物应有可区分的 outcome_detail，便于事后归因写入路径错误。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [_worker(td, "w0", marker="exit=0\n", log="nothing")])
            assert any(r.get("outcome_detail") == "error:exit_0_no_artifact" for r in rows), rows


# ─── A3 · PID 捕获与按 PID 终止 ──────────────────────────────────────
class TestPidCaptureAndKill:
    """看门狗的 kill 必须真的具备终止能力。

    历史缺陷：`_kill_worker` 按 `WINDOWTITLE eq ocsr-*<label>*` 过滤，
    但代码从未设置过 pwsh 的窗口标题（`--title` 是 opencode 会话标题），
    且 launcher 以 `-WindowStyle Hidden` 启动（隐藏进程窗口标题为 N/A）——
    过滤器匹配不到任何进程，函数又无条件 return True，调用点也不检查返回值。
    """

    def test_launch_command_captures_pid(self):
        with tempfile.TemporaryDirectory() as t:
            cmd = mod._launch_command(Path(t))
            assert "-PassThru" in cmd
            assert mod.PID_FILE_NAME in cmd
            assert "$proc.Id" in cmd

    def test_kill_uses_pid_not_window_title(self):
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_text("4242\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")

            with mock.patch.object(mod.subprocess, "run", fake_run):
                assert mod._kill_worker("w0", wd) is True
            assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
            assert not any("WINDOWTITLE" in str(a) for a in calls)
            assert not any("/IM" in str(a) for a in calls), "禁止无差别 taskkill /IM"

    def test_kill_reports_failure_on_nonzero_returncode(self):
        """taskkill 非零退出必须返回 False —— 旧实现无条件 return True。"""
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_text("4242", encoding="utf-8")
            with mock.patch.object(
                mod.subprocess, "run",
                lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "not found"),
            ):
                assert mod._kill_worker("w0", wd) is False

    def test_kill_without_pid_file_returns_false(self):
        with tempfile.TemporaryDirectory() as t:
            assert mod._kill_worker("w0", Path(t)) is False

    def test_read_pid_tolerates_whitespace_and_bom(self):
        with tempfile.TemporaryDirectory() as t:
            wd = Path(t)
            (wd / mod.PID_FILE_NAME).write_bytes(b"\xef\xbb\xbf 1234 \r\n")
            assert mod._read_pid(wd) == 1234

    def test_kill_failure_recorded_as_killed_failed(self):
        """kill 失败时 outcome_detail 必须是 killed:failed，不得降级为普通 stall。

        `killed:failed` 的定义 = kill 操作本身失败、目标进程可能仍在运行，
        **不**表示「进程已被杀死」。记为普通 stall 会掩盖
        「看门狗已放弃止损而 worker 仍在消耗模型调用」这一事实。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            parsed = [_worker(td, "w0")]
            _, rows = _run_watch(td, parsed, timeout_min=1, started_ago=600,
                                 kill_ok=False,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    def test_kill_success_recorded_as_stall_not_killed_failed(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [_worker(td, "w0")], timeout_min=1, started_ago=600,
                                 kill_ok=True,
                                 timeout_policy=mod.TIMEOUT_POLICY_LEAF_KILL)
            assert not any(r.get("outcome_detail") == "killed:failed" for r in rows), rows

    @pytest.mark.skipif(sys.platform != "win32", reason="taskkill 仅 Windows")
    def test_kill_actually_terminates_process(self):
        """离线集成测试：真起一个进程、真杀掉、断言它确实消失。

        单元测试只能证明「走了 PID 路径」，证明不了「进程真的死了」——
        这条补上那一层，且不触发任何模型调用。
        整链（launcher→opencode→进程树终止）仍需一次真实派发的人工复验。
        """
        exe = "pwsh" if shutil.which("pwsh") else "powershell"
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with tempfile.TemporaryDirectory() as t:
                wd = Path(t)
                (wd / mod.PID_FILE_NAME).write_text(str(proc.pid), encoding="utf-8")
                assert proc.poll() is None, "被测进程未能启动"
                assert mod._kill_worker("dummy", wd) is True
                for _ in range(60):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                assert proc.poll() is not None, "taskkill 报告成功但目标进程仍存活"
        finally:
            if proc.poll() is None:
                proc.kill()


# ─── A2 · DB 锁重派 ──────────────────────────────────────────────────
class TestDbLockRetry:
    """DB 锁重派不得提前宣布完成，且必须刷新 pid.txt。

    历史缺陷：重派分支的 `continue` 跳过了 `all_landed = False`，
    函数在重派刚 Start-Process 出去时即宣布「全部完成」并返回 0（产物尚未写入），
    该次调用在遥测与账本中也无任何 landed/failed 记录。
    """

    @staticmethod
    def _locked_worker(td: Path) -> dict:
        return _worker(td, "w0", marker="exit=1\n",
                       log="Error: database is locked", launcher=True)

    def test_retry_does_not_return_success_early(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            rc, _ = _run_watch(td, [self._locked_worker(td)],
                               timeout_min=1, started_ago=600)
            assert rc != 0, "DB 锁重派后在零产物情况下返回了成功"
            assert rc == 1

    def test_retry_refreshes_pid_file(self):
        """重派必须走 `_launch_command`（覆盖刷新 pid.txt）。

        不刷新则看门狗到期时 taskkill 打在已死的旧 PID 上，
        重派出的新 worker 继续存活消耗模型调用。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            cmds: list[str] = []

            def spy(argv, **kw):
                if isinstance(argv, list) and argv:
                    cmds.append(str(argv[-1]))
                return None

            _run_watch(td, [self._locked_worker(td)], timeout_min=1,
                       started_ago=600, subprocess_spy=spy)
            assert cmds, "重派未发生"
            assert any(mod.PID_FILE_NAME in c and "-PassThru" in c for c in cmds), \
                f"重派未刷新 pid.txt: {cmds}"

    def test_retry_happens_only_once(self):
        """`retried` 集合是控制流的唯一依据；`retry_count` 只服务遥测。

        二者合一即重演历史上的 `retry_count[i] = 99` 哨兵式歧义。
        """
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            p = self._locked_worker(td)
            wd = p["work_dir"]
            relaunches: list[object] = []

            def spy(argv, **kw):
                relaunches.append(argv)
                # 每次重派后仍写回 DB 锁现场，诱使无限重派
                (wd / "start.marker").write_text("exit=1\n", encoding="utf-8")
                (wd / "run.log").write_text("database is locked", encoding="utf-8")
                return None

            rc, _ = _run_watch(td, [p], timeout_min=1, started_ago=600,
                               subprocess_spy=spy)
            assert len(relaunches) == 1, f"重派次数应为 1，实为 {len(relaunches)}"
            assert rc == mod.EXIT_DETERMINISTIC_FAILURE

    def test_retry_emits_ledger_and_telemetry(self):
        """重派事件必须留痕 —— 历史上该次调用在证据链中完全消失。"""
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            _, rows = _run_watch(td, [self._locked_worker(td)],
                                 timeout_min=1, started_ago=600)
            assert any(r.get("outcome_detail") == "error:database-locked-retry"
                       for r in rows), rows
            assert any(r.get("failure_retry_index") == 1 for r in rows), rows
