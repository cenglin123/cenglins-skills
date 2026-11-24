"""
Multi-model parallel SRT proofreader (ensemble mode).

- Every batch is checked by every model in ENSEMBLE (union of corrections, deduped).
- VRAM guard: falls back to fewer models if VRAM is tight.
- Full correction pipeline: schema normalize -> markdown strip -> [N] prefix strip
  -> cosmetic filter (case-sensitive) -> hallucination filter -> SAFE_RULES.
"""
import json, os, re, sys, time, glob
import urllib.request, urllib.error
import concurrent.futures
import subprocess

from rich.console import Console
from rich.table import Table

# === Config ===
OLLAMA_URL = "http://localhost:11434/api/chat"
BASE = os.getcwd()          # 默认当前工作目录；也可改为目标视频根目录
TARGETS = []                # 必填：BASE 下的子目录/频道名列表，如 ["某UP主"]
# 仅处理文件名包含任一子串的 SRT（避免同目录旧视频被重新校对）；留空=处理目录内全部
FILE_FILTER = []            # 留空=处理目录内全部 SRT；填子串则只校对文件名含该子串的
BATCH_SIZE = 30
WORKERS_PER_MODEL = 2  # 2 models x 2 workers: keeps VRAM well under 24GB
MAX_PASSES = 3  # iterate until a pass finds nothing new

# ordered by preference; VRAM guard trims from the end
MODELS = ["qwen3.5:9b", "gemma4:12b"]
MODEL_VRAM_GB = {"qwen3.5:9b": 9.5, "gemma4:12b": 9.5}  # model + kv headroom
ARBITER_MODEL = "qwen3.5:9b"  # decides when ensemble models conflict

# 按视频主题补充"模型反复漏掉"的硬编码修正（可选）。
# 示例：[("Antherapic","Anthropic"), ("大成","大乘"), ("悬天战零件","玄天战灵剑")]
SAFE_RULES = []

console = Console()

SYSTEM_PROMPT = """你是中文科技视频字幕校对员。视频主题：<在此填写本期主题与关键专名>。

逐行检查 ASR 错误：1) 英文专有名词；2) 领域术语；3) 同音错字。不确定的专有名词保留原样。不要改口语、断句、标点。

输出：只返回 JSON 数组，每项 {"line":"有错误的原文","corrected":"修正后"}，只列有错误的行，无错误返回 []。"""


# === SRT ===
def parse_srt(filepath):
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    entries = []
    for block in re.split(r"\n\n+", content.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            continue
        entries.append({"index": int(lines[0].strip()), "timestamp": lines[1].strip(), "text": "\n".join(lines[2:])})
    return entries


def rebuild_srt(entries):
    return "\n".join(f"{e['index']}\n{e['timestamp']}\n{e['text']}\n" for e in entries)


# === Ollama ===
def call_ollama(model, user_msg, timeout=180):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
        "stream": False, "think": False,
        "options": {"temperature": 0.1, "num_predict": 4096, "num_ctx": 2048},
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.loads(resp.read().decode("utf-8"))
        return r["message"].get("content", ""), time.time() - t0


def extract_corrections(text):
    """Parse model output -> filtered correction list."""
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*\n?(\[.*?\])\n?```", text, re.DOTALL)
    raw = None
    if m:
        try:
            raw = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    if raw is None:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                raw = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return normalize_corrections(raw or [])


def normalize_corrections(items):
    def clean(s):
        return re.sub(r"\*\*|__|``", "", re.sub(r"^\[\d+\]\s*", "", s)).strip()

    out = []
    for c in items:
        if not isinstance(c, dict):
            continue
        old = new = reason = None
        if "old" in c and "new" in c:
            old, new, reason = c["old"], c["new"], c.get("reason", "old/new")
        elif "line" in c and "corrected" in c:
            old, new, reason = c["line"], c["corrected"], c.get("reason", "line/corrected")
        elif "line" in c and "corrected_line" in c:
            old, new, reason = c["line"], c["corrected_line"], c.get("reason", "line/corrected_line")
        elif "original" in c and "corrected" in c:
            old, new, reason = c["original"], c["corrected"], c.get("reason", "original/corrected")
        if old is None:
            continue
        old, new = clean(old), clean(new)
        if not old or old == new:
            continue
        if "\n" in old or "\n" in new:  # cross-line garbage from batch boundaries
            continue
        if re.sub(r"\s", "", old) == re.sub(r"\s", "", new):  # cosmetic space-only
            continue
        r = len(new) / max(len(old), 1)  # hallucinated rewrite
        if r > 1.5 or r < 0.6:
            continue
        out.append({"old": old, "new": new, "reason": reason})
    return out


# === Conflict arbitration ===
def arbitrate(entries, old, candidates):
    """Models disagree on the fix for `old`. Ask arbiter to produce the right fix with context.
    Returns best new or None."""
    idx = next((i for i, e in enumerate(entries) if old in e["text"]), None)
    if idx is None:
        return None
    ctx = "\n".join(f"[{entries[j]['index']}] {entries[j]['text']}"
                    for j in range(max(0, idx - 2), min(len(entries), idx + 3)))
    cand = " / ".join(candidates)
    msg = (f"字幕片段（含上下文）：\n{ctx}\n\n"
           f"ASR 听错的片段是：「{old}」（就这 {len(old)} 个字）\n"
           f"候选修正（可能有对有错）：{cand}\n\n"
           f"结合上下文判断说话人真正说的是什么。只返回 JSON：{{\"new\":\"...\"}}，"
           f"new 只包含修正后的那几个字（与原片段等长或相近），"
           f"不要返回整句。\n"
           f"例：听错片段「汗死」→ 返回 {{\"new\":\"焊死\"}}，而不是整句。")
    try:
        body = {
            "model": ARBITER_MODEL,
            "messages": [{"role": "system", "content": "你是字幕仲裁员，只返回JSON对象。"},
                         {"role": "user", "content": msg}],
            "stream": False, "think": False,
            "options": {"temperature": 0.1, "num_predict": 256, "num_ctx": 2048},
        }
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = json.loads(resp.read().decode("utf-8"))["message"].get("content", "")
        m = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if not m:
            return None
        new = json.loads(m.group(0)).get("new", "")
        new = re.sub(r"\*\*|__|``", "", new).strip()
        if not new or new == old or "\n" in new:
            return None
        r = len(new) / max(len(old), 1)
        if r > 2.0 or r < 0.5:  # fragment may legitimately shrink/grow a bit
            return None
        return new
    except Exception:
        return None


# === VRAM guard ===
def get_free_vram_gb():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        total, used = [float(x) for x in r.stdout.strip().split(",")]
        return (total - used) / 1024
    except Exception:
        return 99.0  # unknown -> don't block


def get_loaded_models():
    try:
        req = urllib.request.Request("http://localhost:11434/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return [m["name"] for m in json.loads(resp.read().decode()).get("models", [])]
    except Exception:
        return []


def select_models(available):
    free = get_free_vram_gb()
    loaded = get_loaded_models()
    chosen = []
    need = 0.0
    for m in MODELS:
        if m not in available:
            continue
        cost = 0.0 if m in loaded else MODEL_VRAM_GB[m]  # resident models are free
        if need + cost <= free + 2.0:
            chosen.append(m)
            need += cost
    return chosen or [MODELS[0]]


def warm_model(model):
    try:
        call_ollama(model, "ping", timeout=300)
        return True
    except Exception as e:
        console.print(f"[red]Failed to warm {model}: {e}[/red]")
        return False


# === Task plumbing ===
def build_tasks(srt_files):
    tasks = []  # (srt_path, batch_idx, entries_slice, user_msg, model)
    for srt_path in srt_files:
        entries = parse_srt(srt_path)
        for bi, i in enumerate(range(0, len(entries), BATCH_SIZE)):
            batch = entries[i:i + BATCH_SIZE]
            batch_text = "\n".join(f"[{e['index']}] {e['text']}" for e in batch)
            msg = f"校对以下字幕（{len(batch)}条），只返回JSON数组：\n\n{batch_text}"
            tasks.append((srt_path, bi, msg))
    return tasks


def apply_merged(entries, merged):
    """Apply every correction to ALL matching entries in the given scope.

    No `break`: a correction whose `old` appears in several entries must fix
    every occurrence (same ASR error repeats on consecutive lines). Scope is
    the batch the model actually saw, so a fragment like `Cloud的` cannot leak
    into other contexts. Returns number of replacements applied.
    """
    applied = 0
    for c in merged:
        for e in entries:
            if c["old"] in e["text"]:
                e["text"] = e["text"].replace(c["old"], c["new"])
                applied += 1
    return applied


def apply_safe_rules(entries, rules):
    """Apply hardcoded SAFE_RULES to all matching entries (all occurrences)."""
    applied = 0
    for old, new in rules:
        for e in entries:
            if old in e["text"]:
                e["text"] = e["text"].replace(old, new)
                applied += 1
    return applied


def main():
    # ollama up?
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            available = [m["name"] for m in json.loads(resp.read().decode()).get("models", [])]
    except Exception as e:
        console.print(f"[red]Cannot connect to ollama: {e}[/red]")
        sys.exit(1)

    models = select_models(available)
    free = get_free_vram_gb()
    console.print(f"Free VRAM: {free:.1f} GB -> models: {', '.join(models)}")

    ok_models = [m for m in models if warm_model(m)]
    if not ok_models:
        console.print("[red]No model could be loaded[/red]")
        sys.exit(1)
    models = ok_models

    # collect files
    srt_files = []
    for ch in TARGETS:
        for srt in glob.glob(os.path.join(BASE, ch, "*.srt")):
            if not srt.endswith(".bak"):
                if FILE_FILTER and not any(f in os.path.basename(srt) for f in FILE_FILTER):
                    continue
                srt_files.append(srt)

    # restore from .bak (pass 1 baseline only)
    import shutil
    console.print(f"\n[bold]Restoring {len(srt_files)} SRTs from .bak...[/bold]")
    for p in srt_files:
        if os.path.exists(p + ".bak"):
            shutil.copy2(p + ".bak", p)

    cumulative = {p: [] for p in srt_files}  # all corrections across passes
    frozen = {p: set() for p in srt_files}   # applied old/new fragments: never re-edit

    def is_frozen(path, old):
        for f in frozen[path]:
            if f in old or old in f:
                return True
        return False

    for pass_n in range(1, MAX_PASSES + 1):
        console.print(f"\n[bold magenta]════ PASS {pass_n}/{MAX_PASSES} ════[/bold magenta]")
        results = {}  # srt_path -> {batch_idx: [(corr, model)]}

        base_tasks = build_tasks(srt_files)  # reads CURRENT (progressively corrected) files
        pairs = [(sp, bi, msg, m) for (sp, bi, msg) in base_tasks for m in models]
        console.print(f"[bold]{len(base_tasks)} batches x {len(models)} models = {len(pairs)} tasks[/bold]\n")

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(models) * WORKERS_PER_MODEL) as ex:
            futs = {ex.submit(call_ollama, m, msg): (sp, bi, m) for (sp, bi, msg, m) in pairs}
            done_n = 0
            for fut in concurrent.futures.as_completed(futs):
                sp, bi, m = futs[fut]
                try:
                    content, _ = fut.result()
                    corrs = extract_corrections(content)
                except Exception as e:
                    console.print(f"[red]{m} batch {bi} failed: {e}[/red]")
                    corrs = []
                results.setdefault(sp, {}).setdefault(bi, []).extend([(c, m) for c in corrs])
                done_n += 1
                if done_n % 20 == 0 or done_n == len(pairs):
                    console.print(f"  [dim]{done_n}/{len(pairs)} tasks done[/dim]")
        elapsed = time.time() - t0

        console.print(f"\n[bold green]Pass {pass_n} inference: {elapsed:.1f}s. Merging (arbitration on conflicts)...[/bold green]")

        pass_new_total = 0
        for srt_path in srt_files:
            entries = parse_srt(srt_path)

            merged_all, by_model = [], {}
            n_conflicts = n_arb_kept = 0
            applied = 0

            # Apply corrections within their own batch scope: a correction was
            # produced by the model looking at ONE batch, so it may only touch
            # entries in that batch (no cross-context leak for fragments like
            # `Cloud的`), but ALL matching entries in that batch are fixed.
            for bi in sorted(results.get(srt_path, {})):
                start = bi * BATCH_SIZE
                batch = entries[start:start + BATCH_SIZE]

                by_old = {}
                for c, m in results[srt_path][bi]:
                    by_old.setdefault(c["old"], []).append(c["new"])

                # skip olds already corrected in a previous pass (not present anymore)
                by_old = {o: ns for o, ns in by_old.items() if any(o in e["text"] for e in batch)}
                # freeze: never re-edit text produced by an earlier pass (stops churn)
                by_old = {o: ns for o, ns in by_old.items() if not is_frozen(srt_path, o)}

                merged = []
                for old, news in by_old.items():
                    distinct = list(dict.fromkeys(news))
                    if len(distinct) == 1:
                        new = distinct[0]
                        tag = "both" if len(news) > 1 else "single"
                    else:
                        n_conflicts += 1
                        new = arbitrate(entries, old, distinct[:2])
                        if new is None:
                            continue
                        n_arb_kept += 1
                        tag = "arbitrated"
                    merged.append({"old": old, "new": new, "reason": f"P{pass_n}:{tag}"})
                    by_model[tag] = by_model.get(tag, 0) + 1

                applied += apply_merged(batch, merged)
                merged_all.extend(merged)

            applied += apply_safe_rules(entries, SAFE_RULES)

            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(rebuild_srt(entries))

            if merged_all:
                cumulative[srt_path].extend(merged_all)
                for c in merged_all:
                    frozen[srt_path].add(c["old"])
                    frozen[srt_path].add(c["new"])
                log_path = srt_path.replace(".srt", "_corrections.json")
                with open(log_path, "w", encoding="utf-8") as f:
                    json.dump(cumulative[srt_path], f, ensure_ascii=False, indent=2)

            pass_new_total += len(merged_all)
            bm = ", ".join(f"{k}:{v}" for k, v in sorted(by_model.items())) or "-"
            console.print(f"  {os.path.basename(srt_path)[:42]:<44} new:{len(merged_all):<3} "
                          f"applied:{applied:<3} conflicts:{n_conflicts} arb:{n_arb_kept} [{bm}]")

        console.print(f"\n[bold]Pass {pass_n}: +{pass_new_total} new corrections[/bold]")
        if pass_new_total == 0:
            console.print("[bold green]Converged: no new corrections. Stopping.[/bold green]")
            break

    total_corrections = sum(len(v) for v in cumulative.values())
    console.print(f"\n[bold green]════ DONE: {total_corrections} total corrections over {pass_n} pass(es) ════[/bold green]")
    for p, corr in cumulative.items():
        console.print(f"  {os.path.basename(p)[:50]:<52} {len(corr)} corrections")

    vr = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True, text=True)
    console.print(f"Peak VRAM after run: {vr.stdout.strip()}")


if __name__ == "__main__":
    main()
