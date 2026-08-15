"""Offline mechanical regression checks for OCSR SKILL.md.

Resolves SKILL.md relative to this script. Runs no network calls,
no opencode invocations, no model status checks. Exits nonzero on failure.
"""

import importlib.util
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_PATH = os.path.join(SCRIPT_DIR, "..", "SKILL.md")

# Import TELEMETRY_FIELDS from ocsr_dispatch.py for deterministic field-set comparison
DISPATCH_SCRIPT = os.path.join(SCRIPT_DIR, "ocsr_dispatch.py")
_SPEC = importlib.util.spec_from_file_location("ocsr_dispatch_verify", DISPATCH_SCRIPT)
_DISPATCH_MOD = importlib.util.module_from_spec(_SPEC) if _SPEC and _SPEC.loader else None
if _DISPATCH_MOD:
    sys.modules[_SPEC.name] = _DISPATCH_MOD
    _SPEC.loader.exec_module(_DISPATCH_MOD)
    TELEMETRY_FIELDS = getattr(_DISPATCH_MOD, "TELEMETRY_FIELDS", {})
else:
    TELEMETRY_FIELDS = {}


def read_skill():
    if not os.path.isfile(SKILL_PATH):
        print(f"FAIL: SKILL.md not found at {SKILL_PATH}")
        sys.exit(1)
    with open(SKILL_PATH, "rb") as f:
        raw = f.read()
    with open(SKILL_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    return raw, text


def check_encoding(raw):
    """Verify UTF-8 without BOM, LF-only."""
    ok = True
    if raw.startswith(b"\xef\xbb\xbf"):
        print("FAIL: SKILL.md starts with UTF-8 BOM")
        ok = False
    else:
        print("PASS: no UTF-8 BOM")
    if b"\r\n" in raw:
        print("FAIL: SKILL.md contains CRLF")
        ok = False
    else:
        print("PASS: LF-only line endings")
    if b"\r" in raw.replace(b"\r\n", b""):
        print("FAIL: SKILL.md contains stray CR")
        ok = False
    else:
        print("PASS: no stray CR")
    return ok


def check_frontmatter(text):
    """Verify frontmatter name and primary trigger description exist."""
    ok = True
    if not text.startswith("---"):
        print("FAIL: SKILL.md does not start with frontmatter")
        return False
    end = text.find("---", 3)
    if end == -1:
        print("FAIL: frontmatter not closed")
        return False
    fm = text[:end]
    if "name:" not in fm.split("\n")[1].lower() if "\n" in fm else False:
        pass
    if 'name: ocsr' not in fm.lower():
        print("FAIL: frontmatter missing 'name: ocsr'")
        ok = False
    else:
        print("PASS: frontmatter name present")
    # Check primary trigger description
    triggers = ['ocsr', 'opencode run', 'subagent']
    found_triggers = [t for t in triggers if t.lower() in fm.lower()]
    if found_triggers:
        print(f"PASS: trigger keywords present: {found_triggers}")
    else:
        print("FAIL: no trigger keywords in frontmatter description")
        ok = False
    return ok


def check_knowledge_cutoff(text):
    """Old pattern must be absent; new risk-anchor pattern must be present."""
    ok = True
    if "你的知识截止于" in text:
        print("FAIL: old knowledge-cutoff claim '你的知识截止于' still present")
        ok = False
    else:
        print("PASS: old knowledge-cutoff claim absent")
    if "知识截止可能早于今天" in text:
        print("PASS: new risk-anchor pattern present")
    else:
        print("FAIL: new risk-anchor pattern '知识截止可能早于今天' missing")
        ok = False
    return ok


def check_retry_limit(text):
    """Max total attempts (3) and budget expansion rules must be present."""
    ok = True
    if "3 次总尝试" in text or "3次总尝试" in text:
        print("PASS: max total attempts (3) present")
    else:
        print("FAIL: max total attempts (3) not found")
        ok = False
    if "未经新鲜授权" in text and "不突破" in text:
        print("PASS: budget expansion authorization rule present")
    else:
        print("FAIL: budget expansion authorization rule missing")
        ok = False
    return ok


def check_cost_zero_heuristic(text):
    """cost=0 must be marked as heuristic, not weak-model equivalence."""
    ok = True
    # cost=0 should appear with heuristic/risk-signal language
    cost_lines = [l for l in text.split("\n") if "cost" in l.lower() and "0" in l and ("==" in l or "=" in l or "cost.input" in l or "cost.output" in l)]
    heuristic_signals = ["启发式", "元数据可能缺失", "不能单独证明", "价格元数据", "heuristic"]
    has_heuristic = any(s in text for s in heuristic_signals)
    # Old patterns that should NOT dominate the cost=0 narrative
    old_bad = ["免费档", "免费/弱模型", "free/weak"]
    has_old_bad = any(s in text for s in old_bad)
    if has_heuristic:
        print("PASS: cost=0 described with heuristic/risk-signal language")
    else:
        print("FAIL: cost=0 not described as heuristic risk signal")
        ok = False
    if not has_old_bad:
        print("PASS: no legacy 'free/weak model' equivalence for cost=0")
    else:
        print("FAIL: legacy 'free/weak model' language still present for cost=0")
        ok = False
    return ok


def check_security_boundary(text):
    """best-effort security boundary must mention what it can and cannot prevent."""
    ok = True
    if "best-effort" in text and ("安全隔离" in text or "不构成安全" in text):
        print("PASS: best-effort security boundary language present")
    else:
        print("FAIL: best-effort security boundary language missing")
        ok = False
    # Must mention what it cannot prevent
    if "不能阻止" in text or "不能构成" in text or "不构成安全隔离" in text:
        print("PASS: security limitation (what it cannot prevent) stated")
    else:
        print("FAIL: security limitation not stated")
        ok = False
    return ok


def check_write_fallback(text):
    """Write tool fallback language must exist (not hard Write requirement)."""
    ok = True
    if "优先使用 Write" in text or "若无 Write 工具" in text or "回退到" in text:
        print("PASS: Write fallback language present")
    else:
        print("FAIL: Write fallback language missing")
        ok = False
    # Should still require real file evidence
    if "未实际写入文件" in text or "未实际调用 Write" in text:
        print("PASS: real file evidence still required")
    else:
        print("FAIL: real file evidence requirement missing")
        ok = False
    return ok


def check_ps_version_table(text):
    """PowerShell 5.1/7 difference table must exist."""
    ok = True
    has_51 = "PowerShell 5.1" in text or "PS5.1" in text
    has_7 = "PowerShell 7" in text or "PS7" in text
    has_utf16le = "UTF-16LE" in text
    if has_51 and has_7 and has_utf16le:
        print("PASS: PS5.1/7 encoding difference table present")
    else:
        print("FAIL: PS5.1/7 encoding difference table missing or incomplete")
        ok = False
    return ok


def check_fork_precondition(text):
    """--fork must be documented as requiring --continue or --session."""
    ok = True
    if "--fork" in text:
        if "--continue" in text and "--session" in text:
            print("PASS: --fork precondition (--continue/--session) documented")
        else:
            print("FAIL: --fork present but --continue/--session precondition not found")
            ok = False
    else:
        print("FAIL: --fork not mentioned")
        ok = False
    return ok


def check_model_id_rule(text):
    """opencode models --verbose rule must be present; bare-name/id/providerID for -m must be forbidden."""
    ok = True
    if "opencode models --verbose" in text:
        print("PASS: opencode models --verbose rule present")
    else:
        print("FAIL: opencode models --verbose rule missing")
        ok = False
    if "禁止凭" in text and ("id" in text or "providerID" in text or "裸名" in text):
        print("PASS: bare-name/id/providerID splicing forbidden")
    else:
        print("FAIL: bare-name/id/providerID splice prohibition not found")
        ok = False
    return ok


def check_dispatch_hardening(text):
    """C1-C5 dispatch-link hardening anchors must be present."""
    ok = True
    # C1 detached-dispatch mode
    if "脱管派发模式" in text and "Start-Process" in text and "双监视" in text:
        print("PASS: C1 detached-dispatch mode (launcher + Start-Process + dual-watch)")
    else:
        print("FAIL: C1 detached-dispatch mode missing")
        ok = False
    # C2 silent-stall failure mode + watchdog threshold
    if "静默停滞" in text and "1.5" in text and "15 分钟" in text:
        print("PASS: C2 silent-stall failure mode + watchdog threshold (15min)")
    else:
        print("FAIL: C2 watchdog / silent-stall not found")
        ok = False
    # idempotency no-auto-retry clause verbatim preserved (plan §五 acceptance #4)
    if "禁止自动重派" in text and "幂等性" in text:
        print("PASS: idempotency no-auto-retry clause preserved")
    else:
        print("FAIL: idempotency no-auto-retry clause missing")
        ok = False
    # §七 pitfall-table two new rows (C2)
    if "harness 前台超时 < 单轮耗时" in text and "模型端静默停滞" in text:
        print("PASS: §7 pitfall-table new rows present")
    else:
        print("FAIL: §7 pitfall-table new rows missing")
        ok = False
    # C3 failover ladder + channel-exception clause
    if "失败切换阶梯" in text and "切换 family" in text and "通道例外" in text:
        print("PASS: C3 failover ladder + channel-exception clause")
    else:
        print("FAIL: C3 failover ladder missing")
        ok = False
    # C4 dispatch telemetry + default-flip threshold
    if "dispatch-log" in text and "≥5 次" in text:
        print("PASS: C4 dispatch telemetry + default-flip threshold")
    else:
        print("FAIL: C4 dispatch telemetry missing")
        ok = False
    # C5 honest value-premise
    if "价值前提" in text and "派发链路已在本机验证" in text:
        print("PASS: C5 honest value-premise")
    else:
        print("FAIL: C5 value premise missing")
        ok = False
    return ok


def check_telemetry_fields():
    """Verify dispatch-patterns.md telemetry template contains all implementation fields.

    Uses TELEMETRY_FIELDS from ocsr_dispatch.py as single source of truth.
    Required fields must appear in the template; optional fields should appear.
    """
    if not TELEMETRY_FIELDS:
        print("FAIL: could not load TELEMETRY_FIELDS from ocsr_dispatch.py")
        return False
    dp_path = os.path.join(SCRIPT_DIR, "..", "refs", "dispatch-patterns.md")
    if not os.path.isfile(dp_path):
        print(f"FAIL: dispatch-patterns.md not found at {dp_path}")
        return False
    with open(dp_path, "r", encoding="utf-8") as f:
        dp_text = f.read()

    section_marker = "## 派发遥测记录片段"
    section_pos = dp_text.find(section_marker)
    if section_pos < 0:
        print("FAIL: telemetry section header not found in dispatch-patterns.md")
        return False

    tail = dp_text[section_pos:]
    in_block = False
    code_block_lines = []
    for line in tail.split("\n"):
        if not in_block and "```" in line:
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("```"):
                break
            code_block_lines.append(line)
    template_text = "\n".join(code_block_lines)

    missing_required = []
    missing_optional = []
    for field, kind in sorted(TELEMETRY_FIELDS.items()):
        # Each field should appear as a key in the ordered dict literal
        if field not in template_text:
            if kind == "required":
                missing_required.append(field)
            else:
                missing_optional.append(field)

    ok = True
    if missing_required:
        print(f"FAIL: telemetry template missing required fields: {', '.join(missing_required)}")
        ok = False
    if missing_optional:
        print(f"INFO: telemetry template missing optional fields: {', '.join(missing_optional)}")
    if ok:
        print(f"PASS: telemetry template matches implementation schema ({len(TELEMETRY_FIELDS)} fields)")
    return ok


def check_allowlist():
    """Verify ALLOWED_MODELS is defined in ocsr_dispatch.py and has exactly 4 entries."""
    ok = True
    allowed = getattr(_DISPATCH_MOD, "ALLOWED_MODELS", None) if _DISPATCH_MOD else None
    if allowed is None:
        print("FAIL: ALLOWED_MODELS not found in ocsr_dispatch.py")
        return False
    if not isinstance(allowed, frozenset):
        print("FAIL: ALLOWED_MODELS is not a frozenset")
        ok = False
    if len(allowed) != 4:
        print(f"FAIL: ALLOWED_MODELS has {len(allowed)} entries, expected 4")
        ok = False
    expected = frozenset({
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "xiaomi/mimo-v2.5",
        "xiaomi/mimo-v2.5-pro",
    })
    if allowed != expected:
        print(f"FAIL: ALLOWED_MODELS mismatch")
        print(f"  expected: {sorted(expected)}")
        print(f"  got:      {sorted(allowed) if allowed else '?'}")
        ok = False
    if ok:
        print(f"PASS: ALLOWED_MODELS has exactly 4 entries: {', '.join(sorted(allowed))}")
    return ok


def main():
    raw, text = read_skill()
    results = []
    results.append(("Encoding/BOM/CRLF", check_encoding(raw)))
    results.append(("Frontmatter", check_frontmatter(text)))
    results.append(("Knowledge cutoff", check_knowledge_cutoff(text)))
    results.append(("Retry limit + budget", check_retry_limit(text)))
    results.append(("cost=0 heuristic", check_cost_zero_heuristic(text)))
    results.append(("Security boundary", check_security_boundary(text)))
    results.append(("Write fallback", check_write_fallback(text)))
    results.append(("PS5.1/7 table", check_ps_version_table(text)))
    results.append(("--fork precondition", check_fork_precondition(text)))
    results.append(("Model ID rule", check_model_id_rule(text)))
    results.append(("Telemetry field sync", check_telemetry_fields()))
    results.append(("Dispatch hardening (C1-C5)", check_dispatch_hardening(text)))
    results.append(("Model allowlist (ALLOWED_MODELS)", check_allowlist()))

    failed = [name for name, ok in results if not ok]
    print(f"\n{'='*50}")
    if failed:
        print(f"FAILED ({len(failed)}/{len(results)}): {', '.join(failed)}")
        sys.exit(1)
    else:
        print(f"ALL {len(results)} CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
