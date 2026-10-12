#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build a bilingual (EN/CN) SRT from a JSON cue list.

Input JSON: a list of objects, either
  {"start": "00:00:29,140", "end": "00:00:37,939", "en": "...", "cn": "..."}
or with numeric seconds:
  {"start": 29.14, "end": 37.939, "en": "...", "cn": "..."}

Usage:
  python build_bilingual.py cues.json out.srt [--cn-only] [--en-only]
"""
import argparse, json, sys

def fmt(t):
    if isinstance(t, str):
        return t
    h, m, s = int(t // 3600), int((t % 3600) // 60), t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_in")
    ap.add_argument("srt_out")
    ap.add_argument("--cn-only", action="store_true")
    ap.add_argument("--en-only", action="store_true")
    a = ap.parse_args()

    cues = json.load(open(a.json_in, encoding="utf-8"))
    lines = []
    for i, c in enumerate(cues, 1):
        body = []
        if not a.cn_only and c.get("en"):
            body.append(c["en"])
        if not a.en_only and c.get("cn"):
            body.append(c["cn"])
        lines.append(f"{i}\n{fmt(c['start'])} --> {fmt(c['end'])}\n" + "\n".join(body) + "\n")
    open(a.srt_out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"cues: {len(cues)} -> {a.srt_out}")

if __name__ == "__main__":
    main()
