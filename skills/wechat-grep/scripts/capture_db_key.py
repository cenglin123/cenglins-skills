#!/usr/bin/env python3
"""微信 4.x 数据库密钥捕获（纯标准库，无需 WDA）。

算法（V4，与 WeChatDataAnalysis / recarto404 / 334456777-WeFlow 同源）：
  1. 静态扫描 Weixin.dll 的可执行段，定位 4 条连续 `mov rdx, imm64` 指令拼出的
     32 字节 internal_db_key（微信把真实密钥与该内部密钥异或后存放内存）
  2. 只读扫描 Weixin.exe 进程族的私有内存，按指针结构特征
     {[u64 指针][10x00][u64 32][u64 47]} 收集 32 字节混淆候选
  3. passphrase = 候选 XOR internal_db_key（也尝试原始候选，兼容旧版）
  4. SQLCipher 页校验：PBKDF2-HMAC-SHA512(256000) → mac_key(2次) → 页1 HMAC 比对
  5. 命中即写入 weflow 配置并 db test

用法：
    python capture_db_key.py                 # 全自动
    python capture_db_key.py --dll <Weixin.dll 路径>
    python capture_db_key.py --show          # 显示密钥明文
    python capture_db_key.py --no-set        # 只捕获不写配置

依赖：Python 3.8+ 标准库。微信需处于登录状态（密钥混淆形态在内存中常驻）。
"""
import argparse
import ctypes
import glob
import hashlib
import hmac
import json
import os
import re
import struct
import sys

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
KEY_SIZE = 32
ROUND_COUNT = 256000
PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
AES_BLOCK = 16
READ_CHUNK = 4 * 1024 * 1024

PATTERN_TAIL = b"\x00" * 10 + struct.pack("<Q", 0x20) + struct.pack("<Q", 0x2F)
PTR_BACK = 6

# Weixin.dll 代码段中的内部密钥特征：4 条 mov rdx, imm64（48 BA + 8B）拼接出 32 字节
DLL_KEY_PATTERN = re.compile(
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",  # test rax, rax
    re.DOTALL,
)
DLL_CHUNK = 2 * 1024 * 1024
DLL_OVERLAP = 100
CODE_SECTION_EXEC = 0x20000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
psapi = ctypes.WinDLL("psapi") if os.name == "nt" else None


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def find_weixin_pids():
    TH32CS_SNAPPROCESS = 0x2

    class PE32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    e = PE32W()
    e.dwSize = ctypes.sizeof(e)
    pids = []
    if kernel32.Process32FirstW(snap, ctypes.byref(e)):
        while True:
            if e.szExeFile in ("Weixin.exe", "WeChat.exe"):
                pids.append(e.th32ProcessID)
            if not kernel32.Process32NextW(snap, ctypes.byref(e)):
                break
    kernel32.CloseHandle(snap)
    return pids


def open_process(pid):
    return kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)


def enum_private_regions(handle):
    regions = []
    mbi = MBI()
    addr = 0
    while True:
        if not kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and mbi.RegionSize > 0:
            regions.append((mbi.BaseAddress or 0, mbi.RegionSize))
        nxt = (mbi.BaseAddress or addr) + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regions


def read_region(handle, base, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf, size, ctypes.byref(got))
    if not ok or got.value == 0:
        return b""
    return buf.raw[: got.value]


def read_remote(handle, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
    if not ok or got.value != size:
        return b""
    return buf.raw


def extract_candidates(handle):
    """按指针结构特征收集 32 字节混淆候选。"""
    pointers = set()
    scanned = 0
    for base, size in regions_all(handle):
        pos = 0
        while pos < size:
            chunk = read_region(handle, base + pos, min(READ_CHUNK, size - pos))
            if not chunk:
                break
            start = 0
            while True:
                i = chunk.find(PATTERN_TAIL, start)
                if i < 0:
                    break
                if i >= PTR_BACK:
                    ptr = struct.unpack_from("<Q", chunk, i - PTR_BACK)[0]
                    if 0x10000 < ptr < 0x7FFFFFFEFFFF:
                        pointers.add(ptr)
                start = i + 1
            scanned += 1
            pos += len(chunk)
    print(f"[*] 区域 {scanned} 个，指针结构命中 {len(pointers)} 处")

    candidates = []
    seen = set()
    for ptr in pointers:
        key = read_remote(handle, ptr, KEY_SIZE)
        if len(key) == KEY_SIZE and key not in seen and is_potential_key(key):
            seen.add(key)
            candidates.append(key)
    return candidates


def regions_all(handle):
    return enum_private_regions(handle)


def is_potential_key(key):
    if len(set(key)) < 15:
        return False
    if sum(32 <= b <= 126 for b in key) > 24:
        return False
    return True


# ---------- Weixin.dll 静态扫描 ----------
def query_loaded_dll(pid, dll_name="Weixin.dll"):
    """枚举运行中进程已加载模块，反查其加载的 dll_name 实际路径（即运行版本的 DLL）；
    打开失败/未加载返回空串。启动器布局（exe 在顶层、DLL 在版本子目录）下唯一精确解。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return ""
    try:
        arr = (ctypes.c_void_p * 1024)()
        need = ctypes.c_ulong(0)
        LIST_MODULES_ALL = 0x03
        if not psapi.EnumProcessModulesEx(h, ctypes.byref(arr), ctypes.sizeof(arr),
                                          ctypes.byref(need), LIST_MODULES_ALL):
            return ""
        count = min(need.value // ctypes.sizeof(ctypes.c_void_p), 1024)
        buf = ctypes.create_unicode_buffer(1024)
        for i in range(count):
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(arr[i]), buf, len(buf)):
                if buf.value.endswith("\\" + dll_name):
                    return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def query_process_image(pid):
    """QueryFullProcessImageNameW 只读反查进程可执行文件路径；失败返回空串。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def version_key(path):
    """目录名的版本号 parse 成 int 元组供语义比较（4.1.15.56 > 4.1.9.20；字典序会误判）。"""
    ver = os.path.basename(os.path.dirname(path))
    nums = tuple(int(x) for x in re.findall(r"\d+", ver))
    return nums or (0,)


def find_weixin_dll(explicit=None, pids=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    # 优先从运行中的微信进程反查运行版本的 DLL（多版本目录共存时字典序/猜测都可能误选）
    if pids is None:
        pids = find_weixin_pids()
    for pid in pids:
        # ① 枚举进程已加载模块：直接命中运行中加载的 Weixin.dll（最精确）
        loaded = query_loaded_dll(pid)
        if loaded and os.path.exists(loaded):
            return loaded
        # ② 反查 exe 路径：与 Weixin.dll 同目录，或启动器布局下在其下版本子目录
        exe = query_process_image(pid)
        if exe:
            exe_dir = os.path.dirname(exe)
            for dll in [os.path.join(exe_dir, "Weixin.dll")] + sorted(
                    glob.glob(os.path.join(exe_dir, "*", "Weixin.dll")),
                    key=version_key, reverse=True):
                if os.path.exists(dll):
                    return dll
    # 回退：常见安装位置 glob，语义版本排序取最新
    pats = [
        r"C:\Program Files\Tencent\Weixin\*\Weixin.dll",
        r"C:\Program Files (x86)\Tencent\Weixin\*\Weixin.dll",
        r"D:\Program Files\Tencent\Weixin\*\Weixin.dll",
    ]
    hits = []
    for p in pats:
        hits.extend(glob.glob(p))
    if hits:
        return max(hits, key=version_key)
    return None


def extract_dll_internal_keys(dll_path):
    """纯标准库解析 PE 可执行段，扫描 4x mov rdx, imm64 特征，返回 internal_db_key 候选。"""
    with open(dll_path, "rb") as f:
        head = f.read(0x400)
    if head[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    f = open(dll_path, "rb")
    f.seek(e_lfanew)
    if f.read(4) != b"PE\x00\x00":
        raise ValueError("PE 签名错误")
    coff = f.read(20)
    num_sections = struct.unpack_from("<H", coff, 2)[0]
    size_opt = struct.unpack_from("<H", coff, 16)[0]
    sections = []
    for _ in range(num_sections):
        sec = f.read(40)
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", sec, 8)
        chars = struct.unpack_from("<I", sec, 36)[0]
        if chars & CODE_SECTION_EXEC and rawsize > 0:
            sections.append((rawptr, rawsize))
    print(f"[*] Weixin.dll 可执行段 {len(sections)} 个，扫描内部密钥特征...")

    found = []
    seen = set()
    pat = DLL_KEY_PATTERN
    for rawptr, rawsize in sections:
        pos = 0
        while pos < rawsize:
            f.seek(rawptr + pos)
            chunk = f.read(min(DLL_CHUNK + DLL_OVERLAP, rawsize - pos))
            if not chunk:
                break
            for m in pat.finditer(chunk):
                key = m.group(1) + m.group(2) + m.group(3) + m.group(4)
                if key not in seen:
                    seen.add(key)
                    found.append(key)
            pos += DLL_CHUNK
    f.close()
    return found


# ---------- 候选校验 ----------
def verify_candidate(passphrase, page1):
    salt = page1[:SALT_SIZE]
    mac_salt = bytes(x ^ 0x3A for x in salt)
    key32 = hashlib.pbkdf2_hmac("sha512", passphrase, salt, ROUND_COUNT, dklen=KEY_SIZE)
    mac_key = hashlib.pbkdf2_hmac("sha512", key32, mac_salt, 2, dklen=KEY_SIZE)
    reserve = ((IV_SIZE + HMAC_SIZE + AES_BLOCK - 1) // AES_BLOCK) * AES_BLOCK
    mac = hmac.new(mac_key, page1[SALT_SIZE : PAGE_SIZE - reserve + IV_SIZE], hashlib.sha512)
    mac.update(struct.pack("<I", 1))
    stored_at = PAGE_SIZE - reserve + IV_SIZE
    return hmac.compare_digest(mac.digest(), page1[stored_at : stored_at + HMAC_SIZE])


def validate_candidate(cand, internal_keys, pages):
    """校验单个候选：先试 XOR 混淆形态（4.1.10+），再试原始候选（旧版兼容）。
    pages = [(page1, 账号目录), ...]，逐账号页校验，命中哪个账号返回哪个账号目录。
    返回 (passphrase, 账号目录)；账号目录可能为 None（--db 直指文件且无法反推时）。"""
    for ik in internal_keys:
        pw = bytes(a ^ b for a, b in zip(cand, ik))
        for page1, acc in pages:
            if verify_candidate(pw, page1):
                return pw, acc
    for page1, acc in pages:
        if verify_candidate(cand, page1):
            return cand, acc
    return None, None


# ---------- weflow 注入 ----------
WXID_RE = re.compile(r"wxid[_A-Za-z0-9-]+")


def _clean_wxid(value):
    """wxid_rs1txff8lnvj22_8370 → wxid_rs1txff8lnvj22（去账号目录的随机后缀）。"""
    value = str(value or "").strip()
    m = re.fullmatch(r"(wxid_[^_]+)(?:_.+)?", value, re.IGNORECASE)
    return m.group(1) if m else value


def inject_into_weflow(db_key_hex, wf_exe, account_dir, show_key=False):
    """把捕获的密钥写入以 wxid 命名的配置档案，并切换 current_profile 到该档案。

    多账号模型：每账号一个档案（档案名 = wxid），密钥随档案永久保存；
    切换账号 = `config set current_profile <wxid>`（离线读库无需重新登录该账号）。
    返回 db test 是否通过。
    """
    kd = db_key_hex if show_key else db_key_hex[:6] + "..." + db_key_hex[-4:]
    print(f"[+] 密钥命中: {kd}")
    acc = os.path.abspath(account_dir) if account_dir else ""
    # 档案名 = 清洗后的账号目录 basename（wxid_ 目录去随机后缀；自定义微信号目录原样作键，
    # weflow 档案名是任意键）；仅当连 basename 都为空时才回退 default，避免多账号互相覆盖
    profile = _clean_wxid(os.path.basename(acc)) if acc else ""
    if not profile:
        profile = "default"
    if acc:
        subprocess_weflow(wf_exe, ["config", "set", f"{profile}.db_path", acc])
    subprocess_weflow(wf_exe, ["config", "set", "current_profile", profile])
    subprocess_weflow(wf_exe, ["config", "set", f"{profile}.decrypt_key", db_key_hex])

    out = subprocess_weflow(wf_exe, ["db", "wxid"])
    m = re.search(r"wxid:\s*(\S+)", out)
    wxid = m.group(1) if m else ""
    if WXID_RE.fullmatch(wxid):
        subprocess_weflow(wf_exe, ["config", "set", f"{profile}.wxid", wxid])
        print(f"[*] 档案 {profile}（wxid: {wxid}）")
    else:
        print("[!] 未能确认 wxid（db wxid 无 wxid: 行或格式不符），档案 wxid 键未写入——"
              "图片类命令前需 config set wxid <wxid>")

    out = subprocess_weflow(wf_exe, ["db", "test"])
    print("[*] db test:")
    for line in out.splitlines():
        print("    " + line)
    connected = "connected" in out.lower() and "true" in out.lower()
    if not connected:
        return False

    # 图片密钥：原生 key image（读 kvcomm + 账号目录 _t.dat 模板校验，不访问微信进程）
    args_img = ["key", "image", "--save"]
    if acc:
        args_img += ["--user-dir", acc]
    img = subprocess_weflow(wf_exe, args_img + ["--json"])
    img_ok = '"success":true' in img.replace(" ", "")
    if img_ok:
        print("[图片密钥] key image 派生+模板校验通过，已写入当前档案")
    else:
        print("[图片密钥] 未捕获（kvcomm 缓存或 _t.dat 模板缺失）——图片解析暂不可用；"
              "微信里浏览一些聊天图片后重跑本脚本可补。不影响文本/导出/统计。")
    return connected


def subprocess_weflow(wf_exe, args):
    import subprocess

    try:
        r = subprocess.run([wf_exe] + args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return f"[error] weflow {' '.join(args)} 超时（>120s），已终止"
    except FileNotFoundError:
        return (f"[error] weflow.exe 不存在或被杀软隔离（{wf_exe}）——"
                "从杀软隔离区恢复文件或将 skill 目录加入白名单后重试")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        return f"[rc!=0] {out}"
    return out


def account_dbs_under(p):
    """收集 p 下的 (session.db, 账号目录) 候选：p 是账号目录 → 单元素；p 是数据根 → 全部账号目录
    （多账号共存时全部收集，校验逐账号尝试，命中哪个账号就用哪个账号目录写配置，避免字典序误选）。
    p 是 session.db 文件时反推其上三级为账号目录（须存在 db_storage）。"""
    if os.path.isfile(p):
        account_dir = None
        cand = os.path.dirname(os.path.dirname(os.path.dirname(p)))
        if os.path.isdir(os.path.join(cand, "db_storage")):
            account_dir = cand
        return [(p, account_dir)]
    direct = glob.glob(os.path.join(p, "db_storage", "session", "session.db"))
    if direct:
        return [(direct[0], p)]
    hits = []
    for acc_db in sorted(glob.glob(os.path.join(p, "*", "db_storage", "session", "session.db"))):
        # <p>/<账号>/db_storage/session/session.db → 账号目录为其上三级
        account_dir = os.path.dirname(os.path.dirname(os.path.dirname(acc_db)))
        hits.append((acc_db, account_dir))
    return hits


def resolve_session_dbs(explicit):
    """定位校验用 session.db 候选：显式路径 → weflow 配置的 db_path → 常见位置扫描。

    显式路径与配置值可以是 session.db 文件、账号目录或数据根（账号目录在中间层）。
    返回 [(session.db 文件路径, 应写入配置的账号目录), ...]；账号目录为 None 表示无需改写。
    """
    def announce(hits, origin):
        if len(hits) > 1:
            print(f"[*] {origin} 下发现 {len(hits)} 个账号库，将逐一校验（命中账号的目录写入配置）")
            return
        acc = hits[0][1]
        if acc and os.path.abspath(acc) != os.path.abspath(origin):
            print(f"[*] 自动下钻定位账号目录: {acc}")

    if explicit:
        hits = account_dbs_under(explicit)
        if hits:
            announce(hits, explicit)
            return hits
        print(f"[-] --db 指定路径下未找到 session.db：{explicit}")
        return []
    cfg = os.path.join(os.environ.get("APPDATA", ""), "weflow", "config.json")
    candidates = []  # 回读顺序：当前档案 db_path → 全部档案 db_path（去重） → 旧版顶层 db_path
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        profiles = data.get("profiles", {}) or {}
        ordered = []
        current = str(data.get("current_profile", "") or "").strip()
        if current and isinstance(profiles.get(current), dict):
            ordered.append(profiles[current].get("db_path", ""))
        for p in profiles.values():
            if isinstance(p, dict):
                ordered.append(p.get("db_path", ""))
        ordered.append(data.get("db_path", ""))  # 旧版顶层 db_path 兜底
        for p in ordered:
            p = str(p or "").strip()
            if p and p not in candidates:
                candidates.append(p)
    except Exception:
        pass

    for db_path in candidates:
        hits = account_dbs_under(db_path)
        if hits:
            announce(hits, db_path)
            return hits

    # 配置缺失/失效：扫常见盘符位置（账号目录在数据根之下，自动下钻发现）
    for cand_root in bootstrap_candidate_roots():
        hits = account_dbs_under(cand_root)
        if hits:
            announce(hits, cand_root)
            return hits
    print("[-] 未找到 session.db。请让用户告知微信数据目录：数据根（形如 D:\\xwechat_files）或"
          "账号目录（形如 D:\\xwechat_files\\wxid_xxx_8370）均可，脚本会自动下钻并把正确的"
          "账号目录写进配置。拿到路径后重跑本脚本（或 --db <目录>），也可先执行："
          "weflow config set db_path <目录>（写数据根或账号目录皆可）")
    return []


def weflow_exe():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "weflow.exe")


def bootstrap_candidate_roots():
    """常见微信 4.x 数据根目录候选（weflow 自带探测对自定义盘不可靠，故自行扫描）。"""
    home = os.path.expanduser("~")
    roots = []
    docs = os.path.join(home, "Documents", "xwechat_files")
    if os.path.isdir(docs):
        roots.append(docs)
    for drive in "CDEFGH":
        roots.append(f"{drive}:\\xwechat_files")
    return [r for r in roots if os.path.isdir(r)]


def main():
    ap = argparse.ArgumentParser(description="微信 4.x 数据库密钥捕获（DLL 静态特征 + 内存 XOR 配对，只读不钩挂）")
    ap.add_argument("--db", help="校验用数据库路径：session.db 文件 / 账号目录 / 数据根（默认自动定位）")
    ap.add_argument("--dll", help="Weixin.dll 路径（默认自动查找）")
    ap.add_argument("--show", action="store_true", help="显示密钥明文")
    ap.add_argument("--no-set", action="store_true", help="只捕获不写 weflow 配置")
    args = ap.parse_args()

    if os.name != "nt":
        print("[-] 仅支持 Windows")
        return 1

    pids = find_weixin_pids()
    if not pids:
        print("[-] 未检测到微信进程。请打开微信并登录后重试。")
        return 1
    print(f"[*] 微信进程: {pids}")

    dll = find_weixin_dll(args.dll, pids=pids)
    if not dll:
        print("[-] 未找到 Weixin.dll——仅支持微信 4.x（安装目录形如 Tencent\\Weixin\\<版本>\\Weixin.dll），"
              "微信 3.x（WeChat 目录）不支持。可从任务管理器 → 微信进程 → 属性 查看安装路径，"
              "或用 --dll 指定；多版本目录共存时须用 --dll 指定运行版本的 Weixin.dll。")
        return 1
    print(f"[*] Weixin.dll: {dll}")
    internal_keys = extract_dll_internal_keys(dll)
    if not internal_keys:
        print("[-] DLL 中未找到内部密钥特征（微信版本可能更新，特征需更新）——"
              "请按 feedback/README.md 约定写一条反馈（含微信版本与 evidence），供特征表更新")
        return 1
    print(f"[*] 内部密钥候选 {len(internal_keys)} 个")

    dbs = resolve_session_dbs(args.db)
    if not dbs:
        return 1
    pages = []  # (page1, 账号目录)：数据根下多账号时每个账号一份，命中哪个账号就用哪个写配置
    for db_file, acc_dir in dbs:
        try:
            with open(db_file, "rb") as f:
                page1 = f.read(PAGE_SIZE)
        except OSError as e:
            print(f"[-] 跳过无法读取的库 {db_file}: {e}")
            continue
        pages.append((page1, acc_dir))
        print(f"[*] 校验库: {db_file}")
    if not pages:
        print("[-] 无可读取校验的 session.db")
        return 1

    total_tried = 0
    open_failed_pids = []
    for pid in pids:
        h = open_process(pid)
        if not h:
            open_failed_pids.append(pid)
            continue
        cands = extract_candidates(h)
        kernel32.CloseHandle(h)
        print(f"[pid={pid}] 候选 {len(cands)} 个，页校验中（候选×{len(internal_keys)+1} 组合×{len(pages)} 库）...")
        for cand in cands:
            pw, acc = validate_candidate(cand, internal_keys, pages)
            total_tried += 1
            if pw:
                key_hex = pw.hex()
                if not args.no_set:
                    wf = weflow_exe()
                    if not inject_into_weflow(key_hex, wf, account_dir=acc, show_key=args.show):
                        print("[-] db test 未通过（输出不含 connected: true）——密钥/库可能不匹配，"
                              "请检查后重跑本脚本。已写入未验证的配置（decrypt_key/current_profile），"
                              "可用 `weflow config profiles` 检查，重跑本脚本可覆盖修正")
                        return 1
                elif args.show:
                    print(f"[+] KEY: {key_hex}")
                else:
                    print(f"[+] KEY: {key_hex[:6]}...{key_hex[-4:]}（--show 显示明文）")
                return 0
    if open_failed_pids and total_tried == 0:
        print(f"[-] 微信进程 {open_failed_pids} 全部无法打开（OpenProcess 失败）——多为权限不足"
              "（微信以其他用户/提权运行）或跨会话隔离（服务/远程会话）。请在与微信相同的用户"
              "会话中运行本脚本，必要时提升到该用户权限后重试。")
        return 1
    print(f"[-] 全部 {total_tried} 组候选未通过校验。请确认微信已登录；确认在线仍失败时，"
          "按 feedback/README.md 约定写一条反馈（含微信版本、掩码后的候选计数与证据）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
