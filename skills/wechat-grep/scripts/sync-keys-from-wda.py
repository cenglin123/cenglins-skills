#!/usr/bin/env python3
"""从 WeChatDataAnalysis 的 account_keys.json 同步密钥到 weflow CLI 配置。

用法：
    python sync-keys-from-wda.py            # 同步并显示状态（不回显密钥明文）
    python sync-keys-from-wda.py --check    # 只检查，不写入

前置：WeChatDataAnalysis 已完成「解密提取」且 output/account_keys.json 包含 db_key。
写入目标为 weflow **当前档案**（config.json 顶层 current_profile 指向的 profiles.<名称>），
密钥库条目按当前账号 wxid 匹配（条目键可能是 wxid 或 wxid 目录名，清洗后对齐）；
无法唯一确定当前账号且密钥库含多个账号条目时拒绝写入，避免把别的账号密钥写进当前档案。
密钥失效时重跑 WDA 捕获后再执行本脚本即可。
"""
import json
import os
import re
import subprocess
import sys

WF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "weflow.exe")
WDA_STORE = os.path.join(os.environ.get("APPDATA", ""), "wechat-data-analysis-desktop", "output", "account_keys.json")
WF_CFG = os.path.join(os.environ.get("APPDATA", ""), "weflow", "config.json")


def _clean_wxid(value):
    """wxid_rs1txff8lnvj22_8370 → wxid_rs1txff8lnvj22（去账号目录的随机后缀），与 capture 脚本对齐。"""
    value = str(value or "").strip()
    m = re.fullmatch(r"(wxid_[^_]+)(?:_.+)?", value, re.IGNORECASE)
    return m.group(1) if m else value


def load_json(path, what):
    """读 JSON；损坏/被占用时给出明确报错并返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as e:
        print(f"[-] {what} 无法解析（文件损坏或被占用）: {e}")
        return None


def detect_current():
    """读 weflow 配置确定当前账号：返回 (当前档案名, 该档案配置 dict)；未设置返回 (None, {})。"""
    data = load_json(WF_CFG, "weflow 配置 config.json")
    if not isinstance(data, dict):
        return None, {}
    profiles = data.get("profiles", {}) or {}
    profile = str(data.get("current_profile", "") or "").strip() or None
    cur = profiles.get(profile) if profile else None
    return profile, (cur if isinstance(cur, dict) else {})


def entry_matches(entry_key, wxid):
    """密钥库条目键可能是 wxid 本身或 wxid 目录名（带随机后缀），清洗后对齐匹配。"""
    keys = {str(entry_key or "").strip(), _clean_wxid(entry_key)} - {""}
    targets = {str(wxid or "").strip(), _clean_wxid(wxid)} - {""}
    return bool(keys & targets)


def cfg(key, value):
    try:
        r = subprocess.run([WF, "config", "set", key, str(value)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120)
    except subprocess.TimeoutExpired:
        return 1
    return r.returncode


def main():
    check_only = "--check" in sys.argv
    if not os.path.exists(WDA_STORE):
        print("WDA 密钥库不存在，请先在 WeChatDataAnalysis 中完成「解密提取」")
        return 1
    store = load_json(WDA_STORE, "WDA 密钥库 account_keys.json")
    if store is None:
        return 1
    if not isinstance(store, dict):
        print("[-] WDA 密钥库格式异常（顶层不是 JSON 对象）")
        return 1
    entries = {k: v for k, v in store.items() if isinstance(v, dict) and v.get("db_key")}
    if not entries:
        print("密钥库中未找到含 db_key 的账号条目")
        return 1

    profile, cur = detect_current()
    wxid = str(cur.get("wxid", "") or "").strip() or None
    matched = [k for k in entries if entry_matches(k, wxid)] if wxid else []

    if wxid and len(matched) == 1:
        chosen_key = matched[0]
        src = f"当前账号 {wxid}（档案 {profile}）在密钥库中的唯一匹配条目"
    elif len(entries) == 1 and not wxid:
        chosen_key = next(iter(entries))
        src = "密钥库唯一条目（weflow 未记录当前账号 wxid，无法交叉确认，请人工核对）"
    else:
        print("[-] 拒绝写入：无法唯一确定当前账号对应的密钥库条目——避免把别的账号密钥写进当前档案。")
        print(f"    当前档案: {profile or '未设置'}（wxid: {wxid or '未知'}）")
        print("    密钥库可用条目:")
        for k in sorted(entries):
            print(f"      - {k}")
        print("    处置：确认微信当前登录账号后执行 weflow config set current_profile <档案名>，")
        print("    或先用 capture_db_key.py 捕获当前账号（会写入档案 wxid 键）后重跑本脚本。")
        return 1

    item = entries[chosen_key]
    db_key = str(item.get("db_key", "")).strip()
    ok = len(db_key) == 64 and all(c in "0123456789abcdefABCDEF" for c in db_key)
    print(f"db_key: {'OK (64位十六进制)' if ok else f'异常 len={len(db_key)}'}")

    xor_raw = item.get("image_xor_key")
    try:
        xor_num = int(str(xor_raw), 0)
    except (ValueError, TypeError):
        xor_num = None
    aes_key = str(item.get("image_aes_key", "")).strip()
    print(f"image_aes_key: {'OK' if aes_key else '缺失'}; image_xor_key: {'OK' if xor_num is not None else '缺失/非数值'}")
    print(f"来源: {src} → 写入当前档案: {profile or 'default'}")
    if not profile:
        print("[!] weflow 未设置 current_profile，密钥将写入 default 档案；确认后可 "
              "weflow config set current_profile <档案名> 切换")

    if check_only:
        return 0 if ok else 1
    if not ok:
        return 1

    # 写前覆盖提示：目标档案已有不同旧值时提醒
    overrides = []
    for name, new in (("decrypt_key", db_key.lower()),
                      ("image_aes_key", aes_key.lower()),
                      ("image_xor_key", str(xor_num) if xor_num is not None else "")):
        old = str(cur.get(name, "") or "").strip().lower()
        if new and old and old != new:
            overrides.append(name)
    if overrides:
        print(f"[!] 当前档案已存在不同的旧值（{', '.join(overrides)}），将覆盖既有密钥")

    rc1 = cfg("decrypt_key", db_key)
    rc2 = cfg("image_aes_key", aes_key) if aes_key else "skip"
    rc3 = cfg("image_xor_key", xor_num) if xor_num is not None else "skip"
    print(f"写入 weflow 配置（当前档案）: decrypt_key rc={rc1}, image_aes_key rc={rc2}, image_xor_key rc={rc3}")

    # 验证
    try:
        r = subprocess.run([WF, "db", "test"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        out = ""
    else:
        out = ((r.stdout or "") + (r.stderr or "")).strip()
    passed = "connected" in out and "true" in out.lower()
    print("db test:", "PASS" if passed else out[:120])
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
