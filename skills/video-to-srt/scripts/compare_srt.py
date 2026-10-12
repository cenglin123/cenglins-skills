#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cross-validate multiple ASR outputs (SRT and/or plain text) by time window.

Usage:
  python compare_srt.py <dir_or_file...> [--bin 10] [--top 30]

- .srt  files are parsed with timestamps and compared in `--bin`-second windows,
  allowing +/-1 window tolerance for segmentation offsets.
- .txt  files (e.g. SenseVoice output) are compared as whole documents.
Prints per-file stats and the lowest-agreement time windows.
"""
import argparse, difflib, glob, os, re

def parse_srt(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = []
    for b in re.split(r"\n\s*\n", txt.strip()):
        lines = [l for l in b.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        out.append((g[0]*3600+g[1]*60+g[2]+g[3]/1000, " ".join(lines[2:]).strip()))
    return out

def norm(s):
    s = s.lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def bins(entries, width):
    d = {}
    for st, tx in entries:
        d.setdefault(int(st // width), []).append(norm(tx))
    return {k: " ".join(v).strip() for k, v in d.items() if " ".join(v).strip()}

def sim(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--bin", type=float, default=10.0)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()

    files = []
    for i in a.inputs:
        if os.path.isdir(i):
            files += sorted(glob.glob(os.path.join(i, "*.srt")))
            files += sorted(glob.glob(os.path.join(i, "*.txt")))
        else:
            files.append(i)
    timed, plain = {}, {}
    for f in files:
        name = os.path.splitext(os.path.basename(f))[0][:18]
        if f.lower().endswith(".srt"):
            b = bins(parse_srt(f), a.bin)
            if b:
                timed[name] = b
        else:
            plain[name] = norm(open(f, encoding="utf-8", errors="replace").read())
    print("timed models:", {k: len(v) for k, v in timed.items()})
    print("plain models:", list(plain))

    names = list(timed)
    if len(names) < 2:
        print("need >=2 timed (.srt) files to compare windows")
    else:
        keys = sorted(set().union(*[set(v) for v in timed.values()]))
        rep = []
        for k in keys:
            present = [n for n in names if timed[n].get(k)]
            if len(present) < 2:
                continue
            worst = 1.0
            for i in range(len(present)):
                for j in range(i+1, len(present)):
                    a1, b1 = timed[present[i]][k], timed[present[j]].get(k, "")
                    best = max(sim(a1, timed[present[j]].get(k+d, "")) for d in (-1, 0, 1))
                    worst = min(worst, best)
            rep.append((worst, k, present))
        rep.sort(key=lambda x: x[0])
        print(f"\n=== lowest-agreement {a.bin:.0f}s windows ===")
        for w, k, present in rep[:a.top]:
            t = int(k * a.bin)
            print(f"\n[{t//60:02d}:{t%60:02d}] agreement={w:.2f} models={present}")
            for n in names:
                if timed[n].get(k):
                    print(f"  {n:>18}: {timed[n][k][:200]}")

    if len(plain) >= 2:
        print("\n=== whole-text similarity (plain) ===")
        ks = list(plain)
        for i in range(len(ks)):
            for j in range(i+1, len(ks)):
                print(f"  {ks[i]} vs {ks[j]}: {sim(plain[ks[i]], plain[ks[j]]):.3f}")

if __name__ == "__main__":
    main()
