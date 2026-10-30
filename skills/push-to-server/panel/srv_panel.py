#!/usr/bin/env python3
"""srv_panel.py — /srv 项目管理面板（仅 root 运行）。

单文件、只用 Python 标准库的服务端渲染网页，管理 srv-project 创建的项目账号。
所有变更操作都以参数列表调用 /usr/local/sbin/srv-project，不经 shell。
账号密码登录（scrypt），首个账号 admin 凭安装时生成的初始密码登录并强制改密。

    python3 srv_panel.py [--bind 127.0.0.1]... [--port 9300] [--allow-remote]
                         [--tls-cert 证书 --tls-key 私钥] [--allow-from CIDR]...
                         [--allowed-host 主机名或IP]... [--users-file /etc/srv-panel/users.json]
                         [--log-file /var/log/srv-panel.log]
    python3 srv_panel.py --bootstrap | --reset-admin     （安装脚本调用）

纯函数（解析、校验、密码、限流）可直接 import 测试；导入时不检查 root、不启动服务。
"""
import argparse
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

SRV_PROJECT = "/usr/local/sbin/srv-project"
SRV_ROOT = "/srv"
REGISTRY = "/srv/PROJECTS.tsv"
KEYS_DIR = "/etc/ssh/srv-keys"
SSHD_DROPIN = "/etc/ssh/sshd_config.d/60-srv-projects.conf"
GROUP = "srvproj"
USERS_FILE = "/etc/srv-panel/users.json"
INITIAL_PW_FILE = "/etc/srv-panel/initial-password"
LOG_FILE = "/var/log/srv-panel.log"
COOKIE = "srvpanel"
SESSION_TTL = 8 * 3600
MAX_BODY = 64 * 1024

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
FROM_RE = re.compile(r"^[0-9A-Za-z.:/*?,!-]+$")
KEYREF_RE = re.compile(r"^[A-Za-z0-9._@:+/=-]{1,100}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,99}\.service$")
SERVICE_OPS = ("restart", "stop", "start")
CONFIRM_ACTIONS = ("disable", "revoke", "svc_stop")


# ───────────────────────── 纯函数：校验与比较 ─────────────────────────

def valid_name(name):
    """项目名，与 srv_project.sh 的 valid_name 同一正则。"""
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None


def valid_port(value):
    s = str(value)
    return s.isdigit() and 1024 <= int(s) <= 65535


def valid_unit(unit):
    return isinstance(unit, str) and UNIT_RE.fullmatch(unit) is not None


def host_ok(host_header, server_port, extra_hosts=()):
    """Host 头只能是 127.0.0.1 / localhost（可带本服务端口），防 DNS rebinding。"""
    if not host_header or len(host_header) > 255:
        return False
    m = re.fullmatch(r"([A-Za-z0-9.-]+)(?::(\d+))?", host_header)
    if not m:
        return False
    host, port = m.group(1), m.group(2)
    if port and int(port) != int(server_port):
        return False
    allowed = {"127.0.0.1", "localhost", *[h.lower() for h in extra_hosts]}
    return host.lower() in allowed


def token_equal(a, b):
    """常量时间比较。"""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def make_csrf(secret, sid):
    return hmac.new(secret, ("csrf:" + sid).encode("utf-8"), hashlib.sha256).hexdigest()


def check_csrf(secret, sid, given):
    return bool(sid) and bool(given) and token_equal(make_csrf(secret, sid), given)


# ───────────────────────── 纯函数：解析 ─────────────────────────

_SS_PROC_RE = re.compile(r'\("([^"]*)",pid=(\d+),fd=\d+\)')


def parse_ss(text):
    """解析 `ss -Hltnp`，返回 [{addr, port, procs:[(名称, pid)]}]。"""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        procs = [(m.group(1), int(m.group(2))) for m in _SS_PROC_RE.finditer(line)]
        out.append({"addr": addr.strip("[]"), "port": int(port), "procs": procs})
    return out


def parse_registry(text):
    """解析 PROJECTS.tsv，返回 [{name, port, contact, created}]；# 开头与空行跳过。"""
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        f += [""] * (4 - len(f))
        rows.append({"name": f[0], "port": f[1], "contact": f[2], "created": f[3]})
    return rows


_KEY_RE = re.compile(r"(?:^|\s)((?:ssh-|ecdsa-|sk-)[A-Za-z0-9@.-]*)\s+([A-Za-z0-9+/=]+)(?:\s+(.*))?$")


def parse_key_line(line):
    """解析授权公钥行 `restrict,pty[,from="…"] 类型 密钥 标签`，无法识别返回 None。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = _KEY_RE.search(line)
    if not m:
        return None
    options = line[:m.start(1)].strip()
    rest = (m.group(3) or "").strip()
    label = rest.split()[-1] if rest else ""
    fm = re.search(r'from="([^"]*)"', options)
    return {"options": options, "type": m.group(1), "blob": m.group(2),
            "label": label, "from": fm.group(1) if fm else ""}


def parse_version(text):
    """取出文本中第一个 x[.y[.z]] 版本，未写部分为 None；无版本返回 None。"""
    m = re.search(r"v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?", text or "")
    if not m:
        return None
    return tuple(None if g is None or g in "xX*" else int(g) for g in m.groups())


def _fill(t):
    return tuple(0 if x is None else x for x in t)


def _bump(t):
    """把最后一个已写出的分量加一，其后清零（用于开区间上界）。"""
    parts = list(t)
    last = max((i for i, x in enumerate(parts) if x is not None), default=0)
    parts[last] = (parts[last] or 0) + 1
    return tuple(parts[:last + 1]) + (0,) * (2 - last)


def _token_ok(ver, token):
    m = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?v?(.+)", token)
    if not m:
        return None
    op, body = m.group(1) or "=", m.group(2)
    t = parse_version(body)
    if t is None or not re.fullmatch(r"\d+(?:\.(?:\d+|[xX*]))*", body):
        return None
    full = None not in t
    if op == ">=":
        return ver >= _fill(t)
    if op == ">":
        return ver > _fill(t) if full else ver >= _bump(t)
    if op == "<":
        return ver < _fill(t)
    if op == "<=":
        return ver <= _fill(t) if full else ver < _bump(t)
    if op == "=":
        return ver == _fill(t) if full else ver[:len([x for x in t if x is not None])] == tuple(x for x in t if x is not None)
    if op == "~":
        hi = (t[0], (t[1] + 1), 0) if t[1] is not None else (t[0] + 1, 0, 0)
        return _fill(t) <= ver < hi
    if op == "^":
        a, b, c = _fill(t)
        hi = (a + 1, 0, 0) if a > 0 or t[1] is None else ((0, b + 1, 0) if b > 0 or t[2] is None else (0, 0, c + 1))
        return _fill(t) <= ver < hi
    return None


def version_satisfies(spec, installed):
    """engines 风格版本约束的简单判断。返回 True/False；约束或版本看不懂返回 None。"""
    ver = parse_version(installed)
    if ver is None or None in ver or not spec or not spec.strip():
        return None
    spec = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", spec.strip())
    verdict = False
    for alt in spec.split("||"):
        tokens = alt.split()
        if not tokens:
            return None
        results = [_token_ok(ver, tk) for tk in tokens]
        if any(r is None for r in results):
            return None
        verdict = verdict or all(results)
    return verdict


def parse_units(text):
    """解析 `systemctl list-units --type=service --all --no-legend --plain`。"""
    units = []
    for line in text.splitlines():
        p = line.split(None, 4)
        if len(p) >= 4 and p[0].endswith(".service"):
            units.append({"unit": p[0], "load": p[1], "active": p[2], "sub": p[3],
                          "desc": p[4].strip() if len(p) > 4 else ""})
    return units


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


# ───────────────────────── 系统交互 ─────────────────────────

def run_cmd(args, stdin_text=None, timeout=30):
    """以参数列表执行命令（不经 shell），返回 (退出码, stdout, stderr)。"""
    try:
        p = subprocess.run(args, input=stdin_text, capture_output=True, text=True,
                           timeout=timeout, errors="replace",
                           stdin=None if stdin_text is not None else subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时：" + " ".join(args[:3])
    except OSError as e:
        return 127, "", str(e)


def read_text(path, limit=1 << 20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def read_project_file(project, rel):
    """读取 /srv/<项目>/ 下的文件。项目用户可控制文件，所以只读真实路径仍在其目录内的普通小文件。"""
    base = os.path.realpath(os.path.join(SRV_ROOT, project))
    path = os.path.realpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > 1 << 20:
        return None
    return read_text(path)


def account_info(name):
    """返回 (uid, 是否 srvproj 组成员)；账号不存在返回 (None, False)。"""
    try:
        import pwd
        uid = pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError):
        return None, False
    rc, out, _ = run_cmd(["id", "-nG", name], timeout=5)
    return uid, rc == 0 and GROUP in out.split()


def is_disabled(name):
    try:
        with open("/etc/shadow", encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split(":")
                if p[0] == name:
                    return len(p) > 7 and p[7] == "1"
    except OSError:
        pass
    return False


def uid_name(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def listeners():
    """本机监听端口，附属主账号名集合。"""
    rc, out, err = run_cmd(["ss", "-Hltnp"], timeout=10)
    rows = parse_ss(out)
    for r in rows:
        owners = set()
        for _, pid in r["procs"]:
            try:
                owners.add(uid_name(os.stat("/proc/%d" % pid).st_uid))
            except OSError:
                pass
        r["owners"] = sorted(owners)
    return rows


def key_fingerprint(line):
    rc, out, _ = run_cmd(["ssh-keygen", "-lf", "-"], stdin_text=line + "\n", timeout=10)
    parts = out.split()
    return parts[1] if rc == 0 and len(parts) > 1 else "（无法计算）"


def project_keys(name):
    keys = []
    for line in read_text(os.path.join(KEYS_DIR, name)).splitlines():
        k = parse_key_line(line)
        if k:
            k["fp"] = key_fingerprint(line)
            keys.append(k)
    return keys


def project_services(name, uid, disabled):
    """用户服务。只展示项目自己的服务：有 unit 文件，或不是 inactive+dead。"""
    if disabled:
        return [], "项目已停用，用户管理器未运行"
    rc, out, err = run_cmd(["systemctl", "--user", "--machine=%s@" % name, "list-units",
                            "--type=service", "--all", "--no-legend", "--plain", "--no-pager"], timeout=15)
    if rc != 0:
        return [], "无法读取用户服务（%s）" % (err.strip().splitlines()[-1] if err.strip() else "用户管理器未运行")
    ignore = ("dbus.service", "dbus-broker.service")
    shown = []
    for u in parse_units(out):
        if u["unit"] in ignore:
            continue
        own = os.path.exists(os.path.join(SRV_ROOT, name, ".config/systemd/user", u["unit"]))
        if own or not (u["active"] == "inactive" and u["sub"] == "dead"):
            shown.append(u)
    return shown, ""


def project_head(name):
    repo = os.path.join(SRV_ROOT, name, "repo")
    if not os.path.exists(os.path.join(repo, ".git")):
        return "repo/ 下没有 git 仓库"
    # 仓库内容由项目账号控制，git 配置可执行命令，所以降权为该账号运行，不以 root 读。
    rc, out, err = run_cmd(["runuser", "-u", name, "--", "git", "-c", "safe.directory=*", "-C", repo,
                            "log", "-1", "--format=%h  %ci  %s"], timeout=15)
    return out.strip() if rc == 0 and out.strip() else "（无法读取 HEAD：%s）" % (err.strip()[:120] or "无提交")


def disk_usage(name):
    rc, out, _ = run_cmd(["du", "-sk", "--one-file-system", os.path.join(SRV_ROOT, name)], timeout=30)
    if rc != 0 and not out:
        return "未知"
    try:
        kb = int(out.split()[0])
    except (ValueError, IndexError):
        return "未知"
    return "%.1f MB" % (kb / 1024) if kb < 1024 * 1024 else "%.2f GB" % (kb / 1024 / 1024)


def load_registry():
    return parse_registry(read_text(REGISTRY))


def collect_project(entry, lst):
    name = entry["name"]
    uid, member = account_info(name)
    d = {"entry": entry, "uid": uid, "member": member, "problems": [], "notes": []}
    if uid is None:
        d["missing"] = True
        d["problems"].append("账号不存在（登记表里有，系统里没有）")
        return d
    d["disabled"] = is_disabled(name)
    if not member:
        d["problems"].append("账号不在 %s 组，面板不对它做变更操作" % GROUP)
    port = int(entry["port"]) if entry["port"].isdigit() else None
    d["port"] = port
    on_port = [r for r in lst if r["port"] == port] if port else []
    d["listening"] = bool(on_port)
    owners = sorted({o for r in on_port for o in r["owners"]})
    d["owners"] = owners
    d["owner_ok"] = (not owners) or owners == [name]
    foreign = [o for o in owners if o != name]
    if foreign:
        d["problems"].append("端口 %s 被其他账号占用：%s" % (port, "、".join(foreign)))
    elif on_port and not owners:
        d["notes"].append("端口在监听，但看不到进程属主")
    d["keys"] = project_keys(name)
    d["services"], d["services_err"] = project_services(name, uid, d["disabled"])
    d["head"] = project_head(name)
    d["disk"] = disk_usage(name)
    return d


def collect_all():
    lst = listeners()
    reg = load_registry()
    with ThreadPoolExecutor(max_workers=4) as ex:
        projects = list(ex.map(lambda e: collect_project(e, lst), reg))
    by_port = {}
    for p in projects:
        by_port.setdefault(p["entry"]["port"], []).append(p["entry"]["name"])
    for p in projects:
        same = by_port.get(p["entry"]["port"], [])
        if len(same) > 1:
            p["problems"].append("登记端口 %s 与项目 %s 重复" % (
                p["entry"]["port"], "、".join(n for n in same if n != p["entry"]["name"])))
    return projects, lst, reg


# ───────────────────────── 环境依赖检测 ─────────────────────────

ENV_SCRIPT = (
    "for t in git node npm python3 docker; do "
    "p=$(command -v $t 2>/dev/null); "
    "if [ -n \"$p\" ]; then v=$($t --version 2>&1 | head -n 1); echo \"SRVENV|$t|$p|$v\"; "
    "else echo \"SRVENV|$t||\"; fi; done"
)
TOOLS = ("git", "node", "npm", "python3", "docker")


def parse_env_output(text):
    """解析 ENV_SCRIPT 输出 → {工具: {path, version}}。"""
    res = {}
    for line in text.splitlines():
        if line.startswith("SRVENV|"):
            p = line.split("|", 3) + ["", ""]
            res[p[1]] = {"path": p[2], "version": p[3].strip()}
    return res


def declared_requirements(name):
    """repo/ 里的版本声明：[(来源, 要求文本)]。"""
    decl = []
    pkg = read_project_file(name, "repo/package.json")
    if pkg is not None:
        decl.append(("package.json", None))
        try:
            eng = json.loads(pkg).get("engines", {})
            if isinstance(eng, dict) and isinstance(eng.get("node"), str):
                decl.append(("package.json engines.node", eng["node"]))
        except (ValueError, AttributeError):
            decl.append(("package.json 解析失败", None))
    nvm = read_project_file(name, "repo/.nvmrc")
    if nvm is not None:
        decl.append((".nvmrc", nvm.strip().splitlines()[0].strip() if nvm.strip() else ""))
    if read_project_file(name, "repo/requirements.txt") is not None:
        decl.append(("requirements.txt", None))
    py = read_project_file(name, "repo/pyproject.toml")
    if py is not None:
        m = re.search(r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', py, re.M)
        decl.append(("pyproject.toml", None))
        if m:
            decl.append(("pyproject.toml requires-python", m.group(1)))
    return decl


def env_findings(tools, decl):
    """把声明与实际版本比对，返回 [(级别, 文本)]，级别 ok/warn/info。"""
    out = []
    node = tools.get("node", {}).get("version", "")
    py = tools.get("python3", {}).get("version", "")
    for src, spec in decl:
        if src in ("package.json",):
            out.append(("ok" if node else "warn", "存在 package.json，" + ("账号可用 node " + node if node else "但该账号没有 node")))
        elif src == "package.json engines.node" or src == ".nvmrc":
            if not node:
                out.append(("warn", "%s 要求 node %s，但该账号没有 node" % (src, spec)))
                continue
            check_spec = spec
            if src == ".nvmrc":
                if not re.fullmatch(r"v?\d+(\.\d+){0,2}", spec or ""):
                    out.append(("info", ".nvmrc 内容「%s」无法自动比较（实际 %s）" % (spec, node)))
                    continue
                check_spec = spec.lstrip("v")
            r = version_satisfies(check_spec, node)
            if r is None:
                out.append(("info", "%s=「%s」无法自动判断（实际 node %s）" % (src, spec, node)))
            elif r:
                out.append(("ok", "%s=「%s」满足（实际 node %s）" % (src, spec, node)))
            else:
                out.append(("warn", "%s=「%s」不满足（实际 node %s）" % (src, spec, node)))
        elif src in ("requirements.txt", "pyproject.toml"):
            out.append(("ok" if py else "warn", "存在 %s，" % src + ("账号可用 python3 " + py if py else "但该账号没有 python3")))
        elif src == "pyproject.toml requires-python":
            r = version_satisfies(spec, py) if py else None
            if not py:
                out.append(("warn", "requires-python=「%s」，但该账号没有 python3" % spec))
            elif r is None:
                out.append(("info", "requires-python=「%s」无法自动判断（实际 %s）" % (spec, py)))
            else:
                out.append(("ok" if r else "warn", "requires-python=「%s」%s（实际 %s）" % (spec, "满足" if r else "不满足", py)))
        else:
            out.append(("info", src))
    if not decl:
        out.append(("info", "repo/ 里没有发现 package.json / .nvmrc / requirements.txt / pyproject.toml"))
    return out


def project_env(name):
    """以该账号身份（登录 + 交互 shell，以便读到 ~/.profile 与 ~/.bashrc 里加的 PATH）检测工具。"""
    if is_disabled(name):
        return None, [("info", "项目已停用，账号被锁定，未检测")], []
    inner = "bash -lic '" + ENV_SCRIPT.replace("'", "'\\''") + "'"
    rc, out, err = run_cmd(["runuser", "-l", name, "-c", inner], timeout=40)
    tools = parse_env_output(out)
    if not tools:
        return None, [("warn", "检测失败（退出码 %s）：%s" % (rc, err.strip()[-200:]))], []
    decl = declared_requirements(name)
    return tools, env_findings(tools, decl), decl


# ───────────────────────── 账号、密码、限流、来源控制 ─────────────────────────

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
MIN_PW_LEN = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


class UserError(ValueError):
    """用户库操作的中文错误。"""


def valid_username(name):
    return isinstance(name, str) and USERNAME_RE.fullmatch(name) is not None


def generate_password(length=16):
    return "".join(secrets.choice(PW_ALPHABET) for _ in range(length))


def hash_password(password, salt=None, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """scrypt，每次调用单独加盐。格式 scrypt$N$r$p$盐(hex)$哈希(hex)。"""
    salt = salt if salt is not None else os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (n, r, p, salt.hex(), dk.hex())


def verify_password(password, stored):
    """常量时间比较；存储串格式不对一律返回 False。"""
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        want_b = bytes.fromhex(want)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=128 * 1024 * 1024, dklen=len(want_b))
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(dk, want_b)


_dummy_hash = []


def dummy_hash():
    """用户不存在时也做一次同样代价的校验，让「用户不存在」和「密码错」不可区分。"""
    if not _dummy_hash:
        _dummy_hash.append(hash_password(secrets.token_hex(8)))
    return _dummy_hash[0]


def password_problem(new, username, current_hash=None):
    """新密码不合格时返回中文原因，否则 None。current_hash 非空时，新密码不能与当前密码（含初始密码）相同。"""
    if len(new) < MIN_PW_LEN:
        return "新密码至少 %d 位" % MIN_PW_LEN
    if len(new) > 128:
        return "新密码不超过 128 位"
    if new == username:
        return "新密码不能与用户名相同"
    if current_hash and verify_password(new, current_hash):
        return "新密码不能与当前密码（或初始密码）相同"
    return None


def delete_problem(users, actor, target):
    """删除用户的规则：目标须存在，不能删自己，不能删最后一个超级管理员。"""
    if target not in users:
        return "用户不存在：%s" % target
    if target == actor:
        return "不能删除自己"
    if users[target].get("role") == "super" and sum(1 for u in users.values() if u.get("role") == "super") <= 1:
        return "不能删除最后一个超级管理员"
    return None


def write_file_atomic(path, text, mode=0o600):
    """先写同目录临时文件再 rename；文件从创建起就是 mode 权限。"""
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_users_file(path):
    """读用户库，返回 {用户名: {hash, role, must_change, created}}；文件不存在返回 {}；损坏抛 UserError。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise UserError("用户库读取失败：%s" % e)
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        raise UserError("用户库格式不对")
    for name, u in users.items():
        if not valid_username(name) or not isinstance(u, dict) or u.get("role") not in ("super", "admin") \
                or not isinstance(u.get("hash"), str):
            raise UserError("用户库里的条目不合法：%s" % name)
    return users


def dump_users(users):
    return json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class UserStore:
    """用户库。每次访问先检查文件是否被外部改过（如 --reset-admin），改过就重读。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.users = {}
        self._sig = None
        self._refresh()

    def _signature(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _refresh(self):
        sig = self._signature()
        if sig != self._sig or self._sig is None:
            self.users = load_users_file(self.path)
            self._sig = sig

    def _save(self):
        write_file_atomic(self.path, dump_users(self.users))
        self._sig = self._signature()

    def get(self, name):
        with self.lock:
            self._refresh()
            u = self.users.get(name)
            return dict(u) if u else None

    def names(self):
        with self.lock:
            self._refresh()
            return sorted(self.users)

    def snapshot(self):
        with self.lock:
            self._refresh()
            return {k: dict(v) for k, v in self.users.items()}

    def authenticate(self, name, password):
        u = self.get(name) if valid_username(name) else None
        ok = verify_password(password, u["hash"] if u else dummy_hash())
        return bool(u) and ok

    def set_password(self, name, new, must_change=False):
        with self.lock:
            self._refresh()
            if name not in self.users:
                raise UserError("用户不存在：%s" % name)
            self.users[name]["hash"] = hash_password(new)
            self.users[name]["must_change"] = must_change
            self._save()

    def create(self, name, role="admin"):
        """新建用户，返回一次性初始密码。"""
        if not valid_username(name):
            raise UserError("用户名须以小写字母开头，只含小写字母、数字、. _ -，2–32 位")
        with self.lock:
            self._refresh()
            if name in self.users:
                raise UserError("用户已存在：%s" % name)
            pw = generate_password()
            self.users[name] = {"hash": hash_password(pw), "role": role, "must_change": True,
                                "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._save()
            return pw

    def reset(self, name):
        """重置密码，返回一次性初始密码，并强制下次改密。"""
        with self.lock:
            self._refresh()
            if name not in self.users:
                raise UserError("用户不存在：%s" % name)
            pw = generate_password()
            self.users[name]["hash"] = hash_password(pw)
            self.users[name]["must_change"] = True
            self._save()
            return pw

    def delete(self, actor, name):
        with self.lock:
            self._refresh()
            problem = delete_problem(self.users, actor, name)
            if problem:
                raise UserError(problem)
            del self.users[name]
            self._save()


def bootstrap_store(users_path, pw_path, reset=False):
    """首次安装：建 admin（超级管理员，须改密）并把初始密码写入 pw_path。
    reset=True：admin 存在则换新初始密码并设回须改密；admin 不存在则清空用户库后重建。
    返回中文说明（不含密码）。"""
    users = load_users_file(users_path)
    if users and not reset:
        return "已有用户库，未改动"
    pw = generate_password()
    if reset and "admin" in users:
        users["admin"].update({"hash": hash_password(pw), "must_change": True, "role": "super"})
        note = "已重置 admin 的密码并要求下次登录改密"
    else:
        users = {"admin": {"hash": hash_password(pw), "role": "super", "must_change": True,
                           "created": time.strftime("%Y-%m-%d %H:%M:%S")}}
        note = "已创建超级管理员 admin"
    write_file_atomic(pw_path, pw + "\n")
    write_file_atomic(users_path, dump_users(users))
    return note


class RateLimiter:
    """按来源 IP 计数：窗口内连续失败 max_fail 次则锁定 lock_seconds 秒；成功登录清零。"""

    def __init__(self, max_fail=5, lock_seconds=300, window=900, clock=time.time):
        self.max_fail, self.lock_seconds, self.window, self.clock = max_fail, lock_seconds, window, clock
        self.state = {}   # ip -> [失败次数, 最近失败时间, 锁定截止时间]
        self.mutex = threading.Lock()

    def locked_for(self, ip):
        with self.mutex:
            s = self.state.get(ip)
            left = int(s[2] - self.clock() + 0.999) if s else 0
            return max(left, 0)

    def record_failure(self, ip):
        """记一次失败；本次触发锁定返回 True。"""
        with self.mutex:
            now = self.clock()
            for k in [k for k, v in self.state.items() if v[2] < now and now - v[1] > self.window]:
                del self.state[k]
            s = self.state.setdefault(ip, [0, now, 0])
            if now - s[1] > self.window:
                s[0] = 0
            s[0] += 1
            s[1] = now
            if s[0] >= self.max_fail:
                s[0], s[2] = 0, now + self.lock_seconds
                return True
            return False

    def record_success(self, ip):
        with self.mutex:
            self.state.pop(ip, None)


def parse_allow_from(items):
    """CIDR 列表 → 网络对象列表；格式不对抛 ValueError。"""
    return [ipaddress.ip_network(i.strip(), strict=False) for i in items]


def normalize_ip(text):
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except (ValueError, AttributeError):
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def ip_allowed(text, nets):
    """名单为空不限制；回环来源（服务器本机）始终放行；其余必须落在某个网段内。"""
    if not nets:
        return True
    ip = normalize_ip(text)
    if ip is None:
        return False
    return ip.is_loopback or any(ip in n for n in nets)


def valid_lan_ip(text):
    """证书 SAN / 监听用的局域网 IPv4：必须是普通单播地址。"""
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local
                or ip.is_reserved or str(ip) == "255.255.255.255")


def build_san(lan_ip):
    """openssl subjectAltName 值：局域网 IP + 127.0.0.1 + localhost。"""
    if not valid_lan_ip(lan_ip):
        raise ValueError("不是有效的局域网 IPv4 地址：%s" % lan_ip)
    return "IP:%s,IP:127.0.0.1,DNS:localhost" % lan_ip


def needs_password_change(user, path):
    """must_change 的用户，除改密页与退出外的页面都应重定向到 /password。"""
    return bool(user and user.get("must_change")) and path not in ("/password", "/logout")


# ───────────────────────── 审计与变更操作 ─────────────────────────

_log_lock = threading.Lock()


def audit(log_file, action, params, rc, who=""):
    line = "%s\t%s\t%s\t%s\trc=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), action,
                                       json.dumps(params, ensure_ascii=False, sort_keys=True), who, rc)
    with _log_lock:
        try:
            fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            sys.stderr.write("审计日志写入失败：%s\n" % e)


def registered_names():
    return {r["name"] for r in load_registry()}


def build_action(form):
    """校验表单并生成 (动作名, 审计参数, 命令参数列表, stdin)。校验失败抛 ValueError(中文原因)。"""
    action = form.get("action", "")
    name = form.get("project", "").strip()
    if action == "create":
        port, contact = form.get("port", "").strip(), form.get("contact", "").strip()
        if not valid_name(name):
            raise ValueError("项目名只能用小写字母、数字、- 和 _，以字母开头，2–32 个字符")
        if not valid_port(port):
            raise ValueError("端口须为 1024–65535 的整数")
        if "\t" in contact or "\n" in contact or "\r" in contact or len(contact) > 100:
            raise ValueError("联系人不能含制表符或换行，且不超过 100 字")
        args = [SRV_PROJECT, "create", name, "--port", port]
        if contact:
            args += ["--contact", contact]
        return "create", {"project": name, "port": port, "contact": contact}, args, None
    if not valid_name(name) or name not in registered_names():
        raise ValueError("项目不存在或未登记：%s" % name)
    if action == "grant":
        label, frm = form.get("label", "").strip(), form.get("from", "").strip()
        pub = form.get("pubkey", "").strip()
        if not LABEL_RE.fullmatch(label):
            raise ValueError("标签只能用字母、数字和 . _ @ -（1–64 字符）")
        if frm and not FROM_RE.fullmatch(frm):
            raise ValueError("来源地址格式不对")
        if not pub or len(pub) > 4096 or "\n" in pub.replace("\r", "").strip():
            raise ValueError("请粘贴单行公钥（类型 密钥 [注释]）")
        args = [SRV_PROJECT, "grant", name, "-", "--label", label]
        if frm:
            args += ["--from", frm]
        fp = key_fingerprint(pub) if "PRIVATE KEY" not in pub else "（私钥，已拒绝）"
        return "grant", {"project": name, "label": label, "from": frm, "fingerprint": fp}, args, pub + "\n"
    if action == "revoke":
        key = form.get("key", "").strip()
        if not KEYREF_RE.fullmatch(key) or key.startswith("-"):
            raise ValueError("标签或指纹格式不对")
        return "revoke", {"project": name, "key": key}, [SRV_PROJECT, "revoke", name, key], None
    if action in ("disable", "enable"):
        return action, {"project": name}, [SRV_PROJECT, action, name], None
    if action in ("svc_restart", "svc_stop", "svc_start"):
        unit = form.get("unit", "").strip()
        if not valid_unit(unit):
            raise ValueError("服务名格式不对：%s" % unit)
        uid, member = account_info(name)
        if not member:
            raise ValueError("%s 不是 %s 项目账号" % (name, GROUP))
        op = action[4:]
        args = ["systemctl", "--user", "--machine=%s@" % name, op, unit]
        return action, {"project": name, "unit": unit}, args, None
    raise ValueError("未知操作：%s" % action)


def service_log(name, unit):
    uid, member = account_info(name)
    if not member or name not in registered_names():
        return 1, "", "不是项目账号"
    u = str(uid)
    return run_cmd(["journalctl", "-n", "100", "--no-pager", "-o", "short-iso",
                    "_UID=" + u, "_SYSTEMD_USER_UNIT=" + unit, "+",
                    "_UID=" + u, "USER_UNIT=" + unit], timeout=20)


# ───────────────────────── 页面渲染 ─────────────────────────

CSS = """
body{font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f5f7;color:#1f2933}
header{background:#1f2933;color:#fff;padding:10px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header a{color:#cbd2d9;text-decoration:none}header a:hover{color:#fff}header b{margin-right:10px}
main{max-width:1100px;margin:16px auto;padding:0 16px}
.card{background:#fff;border:1px solid #d9dee3;border-radius:6px;padding:12px 16px;margin:12px 0}
h1{font-size:20px}h2{font-size:16px;margin:0 0 8px}h3{font-size:14px;margin:12px 0 4px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #e4e7eb;vertical-align:top}
th{background:#f0f2f4;font-weight:600}
.ok{color:#0b7a3b}.warn{color:#b45309}.bad{color:#b91c1c}.mute{color:#6b7280}
.tag{display:inline-block;padding:0 8px;border-radius:10px;font-size:12px;color:#fff}
.tag.ok{background:#0b7a3b}.tag.bad{background:#b91c1c}.tag.warn{background:#b45309}.tag.mute{background:#6b7280}
pre{background:#111827;color:#e5e7eb;padding:10px;border-radius:4px;overflow:auto;white-space:pre-wrap;word-break:break-all}
input[type=text],input[type=password],textarea{padding:5px;border:1px solid #9aa5b1;border-radius:4px;width:100%;box-sizing:border-box;font:inherit}
button{padding:4px 12px;border:1px solid #52606d;background:#fff;border-radius:4px;cursor:pointer;font:inherit}
button.danger{background:#b91c1c;color:#fff;border-color:#b91c1c}button.primary{background:#2563eb;color:#fff;border-color:#2563eb}
form.inline{display:inline}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.problem{background:#fef2f2;border:1px solid #fecaca;padding:4px 8px;border-radius:4px;margin:4px 0}
"""


def page(title, body, csrf=None, flash=""):
    nav = ""
    u = getattr(_ctx, "user", None)
    if csrf is not None:
        if u and u.get("must_change"):
            links = ""
        else:
            links = ('<a href="/">总览</a><a href="/global">全局检查</a><a href="/new">新建项目</a>'
                     + ('<a href="/users">用户管理</a>' if u and u.get("role") == "super" else ""))
        who = ('<span class="mute">%s（%s）</span><a href="/password">修改密码</a>'
               % (esc(u["name"]), "超级管理员" if u["role"] == "super" else "管理员")) if u else ""
        nav = ('<header><b>/srv 项目管理面板</b>%s<span style="margin-left:auto"></span>%s'
               '<form class="inline" method="post" action="/logout">'
               '<input type="hidden" name="csrf" value="%s"><button>退出登录</button></form></header>' % (links, who, esc(csrf)))
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>'
            '<style>%s</style></head><body>%s<main>%s%s</main></body></html>'
            % (esc(title), CSS, nav, flash, body))


def tag(text, cls):
    return '<span class="tag %s">%s</span>' % (cls, esc(text))


def hidden(csrf, **kw):
    s = '<input type="hidden" name="csrf" value="%s">' % esc(csrf)
    for k, v in kw.items():
        s += '<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v))
    return s


def btn_form(csrf, label, cls="", **fields):
    return ('<form class="inline" method="post" action="/action">%s<button class="%s">%s</button></form>'
            % (hidden(csrf, **fields), cls, esc(label)))


def render_login(csrf, error=""):
    err = '<div class="problem">%s</div>' % esc(error) if error else ""
    return page("登录", '<div class="card" style="max-width:420px;margin:60px auto"><h2>/srv 项目管理面板</h2>%s'
                '<form method="post" action="/login">%s<p>用户名<input type="text" name="username" autocomplete="username" autofocus required></p>'
                '<p>密码<input type="password" name="password" autocomplete="current-password" required></p>'
                '<button class="primary">登录</button></form>'
                '<p class="mute">首次使用：用户名 admin，初始密码在服务器的 /etc/srv-panel/initial-password。</p></div>' % (err, hidden(csrf)))


def project_card(p, csrf):
    e = p["entry"]
    name = e["name"]
    if p.get("missing"):
        return ('<div class="card"><h2>%s %s</h2><div class="problem">%s</div></div>'
                % (esc(name), tag("账号缺失", "bad"), esc(p["problems"][0])))
    state = tag("停用", "mute") if p["disabled"] else tag("启用", "ok")
    if p["listening"]:
        owner = ("、".join(p["owners"]) or "未知")
        lis = tag("监听中", "ok") + " 属主 " + (tag(owner, "ok") if p["owner_ok"] else tag(owner + "（非项目账号）", "bad"))
    else:
        lis = tag("未监听", "mute")
    probs = "".join('<div class="problem">%s</div>' % esc(x) for x in p["problems"])
    notes = "".join('<div class="mute">%s</div>' % esc(x) for x in p["notes"])
    svc = "".join("<li>%s %s</li>" % (esc(u["unit"]), tag(u["active"] + "/" + u["sub"], "ok" if u["active"] == "active" else ("bad" if u["active"] == "failed" else "mute")))
                  for u in p["services"]) or '<li class="mute">%s</li>' % esc(p["services_err"] or "无运行中的用户服务")
    keys = "".join("<li>%s <span class='mute'>%s%s</span></li>" % (esc(k["label"]), esc(k["fp"]), "  仅限 " + esc(k["from"]) if k["from"] else "")
                   for k in p["keys"]) or '<li class="mute">无授权公钥</li>'
    return ('<div class="card"><h2><a href="/project/%s">%s</a> %s</h2>%s%s'
            '<table><tr><th>端口</th><td>%s（登记）%s</td><th>联系人</th><td>%s</td></tr>'
            '<tr><th>git HEAD</th><td>%s</td><th>磁盘占用</th><td>%s</td></tr></table>'
            '<div class="grid"><div><h3>用户服务</h3><ul>%s</ul></div><div><h3>已授权公钥（%d）</h3><ul>%s</ul></div></div></div>'
            % (esc(name), esc(name), state, probs, notes, esc(e["port"]), lis, esc(e["contact"] or "-"),
               esc(p["head"]), esc(p["disk"]), svc, len(p["keys"]), keys))


def render_overview(projects, csrf):
    body = "<h1>项目总览</h1>"
    bad = sum(len(p["problems"]) for p in projects)
    body += '<p>共 %d 个项目，%s</p>' % (len(projects), tag("%d 个问题" % bad, "bad") if bad else tag("未发现问题", "ok"))
    body += "".join(project_card(p, csrf) for p in projects) or '<div class="card mute">还没有登记项目。<a href="/new">新建项目</a></div>'
    return page("总览", body, csrf)


def render_project(p, tools, findings, decl, csrf):
    e = p["entry"]
    name = e["name"]
    body = '<p><a href="/">← 总览</a></p>' + project_card(p, csrf)
    if p.get("missing"):
        return page(name, body, csrf)
    # 操作
    ops = ""
    ops += btn_form(csrf, "启用", "primary", action="enable", project=name) if p["disabled"] else \
        '<form class="inline" method="post" action="/action">%s<button class="danger">停用项目…</button></form>' % hidden(csrf, action="disable", project=name)
    body += '<div class="card"><h2>项目操作</h2><p>%s</p></div>' % ops
    # 公钥
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><form class='inline' method='post' action='/action'>%s<button class='danger'>吊销…</button></form></td></tr>"
        % (esc(k["label"]), esc(k["type"]), esc(k["fp"]), esc(k["from"] or "不限"), hidden(csrf, action="revoke", project=name, key=k["label"]))
        for k in p["keys"])
    body += ('<div class="card"><h2>授权公钥</h2><table><tr><th>标签</th><th>类型</th><th>指纹</th><th>来源限制</th><th></th></tr>%s</table>'
             '<h3>授权新公钥</h3><form method="post" action="/action">%s'
             '<p>标签（字母数字 . _ @ -）<input type="text" name="label" required maxlength="64"></p>'
             '<p>来源地址（可选，如 10.0.0.5 或 10.0.0.0/24）<input type="text" name="from" maxlength="100"></p>'
             '<p>公钥（单行，只接受 .pub 内容）<textarea name="pubkey" rows="3" required></textarea></p>'
             '<button class="primary">授权</button></form></div>'
             % (rows or '<tr><td colspan="5" class="mute">无</td></tr>', hidden(csrf, action="grant", project=name)))
    # 服务
    srows = ""
    for u in p["services"]:
        srows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s %s %s <a href='/logs?project=%s&amp;unit=%s'>日志</a></td></tr>"
                  % (esc(u["unit"]), esc(u["active"] + "/" + u["sub"]), esc(u["desc"]),
                     btn_form(csrf, "重启", action="svc_restart", project=name, unit=u["unit"]),
                     btn_form(csrf, "启动", action="svc_start", project=name, unit=u["unit"]),
                     '<form class="inline" method="post" action="/action">%s<button class="danger">停止…</button></form>' % hidden(csrf, action="svc_stop", project=name, unit=u["unit"]),
                     urllib.parse.quote(name), urllib.parse.quote(u["unit"])))
    body += ('<div class="card"><h2>用户服务</h2><table><tr><th>服务</th><th>状态</th><th>描述</th><th>操作</th></tr>%s</table>%s</div>'
             % (srows or '<tr><td colspan="4" class="mute">无</td></tr>', '<p class="mute">%s</p>' % esc(p["services_err"]) if p["services_err"] else ""))
    # 环境
    envrows = ""
    for t in TOOLS:
        i = (tools or {}).get(t, {})
        envrows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            t, tag("可用", "ok") if i.get("path") else tag("缺失", "mute") if tools else tag("未知", "mute"),
            esc(i.get("version", "")), esc(i.get("path", "")))
    fl = "".join('<li class="%s">%s</li>' % (c, esc(t)) for c, t in findings)
    body += ('<div class="card"><h2>环境依赖（以 %s 身份检测）</h2><table><tr><th>工具</th><th>状态</th><th>版本</th><th>路径</th></tr>%s</table>'
             '<h3>repo/ 声明与实际版本</h3><ul>%s</ul></div>' % (esc(name), envrows, fl))
    return page(name, body, csrf)


def render_global(projects, lst, csrf):
    checks = [
        ("srv-project 已安装", os.access(SRV_PROJECT, os.X_OK), SRV_PROJECT),
        ("sshd 项目规则存在", os.path.exists(SSHD_DROPIN), SSHD_DROPIN),
        ("公钥目录存在", os.path.isdir(KEYS_DIR), KEYS_DIR),
        ("登记表存在", os.path.exists(REGISTRY), REGISTRY),
    ]
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (esc(n), tag("正常", "ok") if ok else tag("缺失", "bad"), esc(d)) for n, ok, d in checks)
    body = '<h1>全局检查</h1><div class="card"><h2>安装状态</h2><table>%s</table></div>' % rows
    # 权限
    warn, extra = [], []
    reg = {p["entry"]["name"] for p in projects}
    try:
        for n in sorted(os.listdir(SRV_ROOT)):
            full = os.path.join(SRV_ROOT, n)
            if os.path.isdir(full) and not os.path.islink(full):
                if os.stat(full).st_mode & stat.S_IROTH:
                    warn.append(full)
                if n not in reg:
                    extra.append(full)
    except OSError:
        pass
    body += '<div class="card"><h2>/srv 目录权限</h2>%s</div>' % (
        "".join('<div class="problem">%s 对其他账号可读（可执行 chmod o-rwx %s）</div>' % (esc(w), esc(w)) for w in warn)
        or '<p class="ok">没有对 other 可读的目录</p>')
    if extra:
        body += '<div class="card"><h2>/srv 下未登记的目录</h2><ul>%s</ul></div>' % "".join("<li>%s</li>" % esc(x) for x in extra)
    ports = {p["entry"]["port"]: p["entry"]["name"] for p in projects}
    lrows = ""
    for r in sorted(lst, key=lambda r: (r["port"], r["addr"])):
        reg_name = ports.get(str(r["port"]))
        owner = "、".join(r["owners"]) or "（不可见）"
        if reg_name:
            st = tag("项目端口：" + reg_name, "ok" if r["owners"] in ([reg_name], []) else "bad")
        else:
            st = tag("未登记", "mute")
        lrows += "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(r["addr"]), r["port"], esc(", ".join(n for n, _ in r["procs"]) or "-"), esc(owner), st)
    body += ('<div class="card"><h2>本机监听端口</h2><table><tr><th>地址</th><th>端口</th><th>进程</th><th>属主</th><th>登记情况</th></tr>%s</table></div>' % lrows)
    return page("全局检查", body, csrf)


def render_new(csrf):
    return page("新建项目", '<h1>新建项目</h1><div class="card"><form method="post" action="/action">%s'
                '<p>项目名（小写字母开头，字母数字 - _，2–32 字符）<input type="text" name="project" required maxlength="32"></p>'
                '<p>端口（1024–65535）<input type="text" name="port" required maxlength="5"></p>'
                '<p>联系人（可选）<input type="text" name="contact" maxlength="100"></p>'
                '<button class="primary">创建</button></form></div>' % hidden(csrf, action="create"), csrf)


def render_result(title, args, rc, out, err, csrf, params=None):
    cmd = " ".join(args)
    cls = "ok" if rc == 0 else "bad"
    body = ('<h1>%s</h1><div class="card"><p>命令：<code>%s</code></p><p>退出码：<b class="%s">%s</b></p>'
            '<h3>标准输出</h3><pre>%s</pre><h3>标准错误</h3><pre>%s</pre></div><p><a href="/">返回总览</a></p>'
            % (esc(title), esc(cmd), cls, rc, esc(out.strip() or "（空）"), esc(err.strip() or "（空）")))
    return page(title, body, csrf)


CONFIRM_TEXT = {
    "disable": "停用会锁定账号（SSH 登录被拒）、停掉该账号全部进程与用户服务，数据不动。",
    "revoke": "吊销后这把公钥不能再登录；已建立的 SSH 会话不受影响。",
    "svc_stop": "停止后服务不再响应，直到再次启动。",
}


def render_confirm(form, csrf, post_to="/action", texts=None):
    action = form.get("action", "")
    hid = {k: v for k, v in form.items() if k not in ("csrf", "confirmed")}
    detail = "".join("<li>%s：%s</li>" % (esc(k), esc(v)) for k, v in hid.items() if k != "action")
    text = (texts or CONFIRM_TEXT).get(action, "")
    return page("确认操作", '<h1>请确认</h1><div class="card"><p class="warn"><b>操作：%s</b></p><ul>%s</ul><p>%s</p>'
                '<form method="post" action="%s">%s<button class="danger">确认执行</button></form>'
                '<p><a href="/">取消</a></p></div>'
                % (esc(action), detail, esc(text), esc(post_to), hidden(csrf, confirmed="1", **hid)), csrf)


# ───────────────────────── 账号相关页面 ─────────────────────────

def render_password(csrf, user, error="", forced=False):
    err = '<div class="problem">%s</div>' % esc(error) if error else ""
    note = ('<p class="warn"><b>首次登录（或密码刚被重置）必须先修改密码，改完才能使用面板。</b></p>' if forced else "")
    return page("修改密码", '<h1>修改密码</h1><div class="card" style="max-width:480px">%s%s'
                '<form method="post" action="/password">%s'
                '<p>当前密码<input type="password" name="old" autocomplete="current-password" required></p>'
                '<p>新密码（至少 %d 位，不能与当前密码相同）<input type="password" name="new" autocomplete="new-password" required></p>'
                '<p>再输一次新密码<input type="password" name="confirm" autocomplete="new-password" required></p>'
                '<button class="primary">修改密码</button></form></div>' % (note, err, hidden(csrf), MIN_PW_LEN), csrf)


def render_users(users, me, csrf):
    rows = ""
    for name in sorted(users):
        u = users[name]
        ops = ""
        if name != me:
            ops = ('<form class="inline" method="post" action="/users/action">%s<button>重置密码…</button></form> '
                   '<form class="inline" method="post" action="/users/action">%s<button class="danger">删除…</button></form>'
                   % (hidden(csrf, action="reset_user", username=name), hidden(csrf, action="delete_user", username=name)))
        rows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(name), "超级管理员" if u.get("role") == "super" else "管理员",
            tag("须改密", "warn") if u.get("must_change") else tag("正常", "ok"), esc(u.get("created", "")), ops or '<span class="mute">（本人）</span>')
    return page("用户管理", '<h1>用户管理</h1><div class="card"><table><tr><th>用户名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr>%s</table></div>'
                '<div class="card"><h2>新建管理员</h2><form method="post" action="/users/action">%s'
                '<p>用户名（小写字母开头，字母数字 . _ -，2–32 位）<input type="text" name="username" required maxlength="32"></p>'
                '<p class="mute">系统会生成一次性初始密码，只在结果页显示一次；对方首次登录必须改密。</p>'
                '<button class="primary">新建</button></form></div>' % (rows, hidden(csrf, action="create_user")), csrf)


def render_secret_result(title, text, password, csrf):
    return page(title, '<h1>%s</h1><div class="card"><p>%s</p><p>初始密码（<b>只显示这一次</b>，请当面或经安全渠道交给对方；对方首次登录须改密）：</p>'
                '<pre>%s</pre></div><p><a href="/users">返回用户管理</a></p>' % (esc(title), esc(text), esc(password)), csrf)


USER_CONFIRM_TEXT = {
    "reset_user": "重置后该用户的旧密码立即失效，其全部登录会话被注销，并生成新的一次性初始密码。",
    "delete_user": "删除后该用户立即不能登录，其全部登录会话被注销。",
}


# ───────────────────────── HTTP 服务 ─────────────────────────

class State:
    def __init__(self, store, port, extra_hosts=(), log_file=LOG_FILE, allow_nets=(), secure=False,
                 initial_pw_file=INITIAL_PW_FILE, limiter=None):
        self.store = store
        self.port = port
        self.extra_hosts = tuple(extra_hosts)
        self.log_file = log_file
        self.allow_nets = list(allow_nets)
        self.secure = secure
        self.initial_pw_file = initial_pw_file
        self.limiter = limiter or RateLimiter()
        self.secret = secrets.token_bytes(32)
        self.sessions = {}            # sid -> (用户名, 过期时间)
        self.sess_lock = threading.Lock()
        self.deny_logged = {}
        self.lock = threading.Lock()   # 串行化变更操作

    def new_session(self, user):
        sid = secrets.token_urlsafe(32)
        with self.sess_lock:
            self.sessions[sid] = (user, time.time() + SESSION_TTL)
        return sid

    def session_user(self, sid):
        with self.sess_lock:
            s = self.sessions.get(sid or "")
            if s and s[1] > time.time():
                return s[0]
            self.sessions.pop(sid or "", None)
        return None

    def drop_sessions(self, user, keep=None):
        with self.sess_lock:
            for sid in [k for k, v in self.sessions.items() if v[0] == user and k != keep]:
                del self.sessions[sid]


_ctx = threading.local()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "srv-panel"
    sys_version = ""
    timeout = 30
    state = None  # 由 make_servers 设置

    def log_message(self, fmt, *args):
        pass

    # --- 基础 ---
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, headers=()):
        self.send(303, "", headers=[("Location", to), *headers])

    def sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def cookie_attrs(self):
        return "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.state.secure else "")

    def current_user(self):
        """(用户名, 用户记录)；会话无效或用户已被删除返回 None。"""
        name = self.state.session_user(self.sid())
        if not name:
            return None
        u = self.state.store.get(name)
        if not u:
            self.state.drop_sessions(name)
            return None
        return name, u

    def csrf(self):
        return make_csrf(self.state.secret, self.sid())

    def who(self):
        ip = normalize_ip(self.client_address[0])
        return str(ip) if ip else self.client_address[0]

    def guard(self):
        """来源 IP 与 Host 头检查。"""
        st = self.state
        ip = self.who()
        if not ip_allowed(ip, st.allow_nets):
            now = time.time()
            if now - st.deny_logged.get(ip, 0) > 60:     # 同一来源一分钟只记一条，防日志被刷
                st.deny_logged[ip] = now
                audit(st.log_file, "ip_denied", {}, 403, ip)
            self.send(403, "<h1>403 Forbidden</h1>")
            return False
        if not host_ok(self.headers.get("Host", ""), st.port, st.extra_hosts):
            self.send(400, "<h1>Bad Host</h1>")
            return False
        return True

    def read_form(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self.send(413, page("错误", "<h1>请求体过大</h1>"))
            return None
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def login_page(self, code=200, error=""):
        self.send(code, render_login(make_csrf(self.state.secret, "login"), error))

    # --- GET ---
    def do_GET(self):
        _ctx.user = None
        if not self.guard():
            return
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/login":
            return self.login_page()
        cu = self.current_user()
        if not cu:
            return self.redirect("/login")
        me, user = cu
        if needs_password_change(user, u.path):
            return self.redirect("/password")
        _ctx.user = {"name": me, "role": user["role"], "must_change": user.get("must_change")}
        csrf = self.csrf()
        try:
            if u.path == "/password":
                return self.send(200, render_password(csrf, me, forced=bool(user.get("must_change"))))
            if u.path == "/users":
                if user["role"] != "super":
                    return self.send(403, page("无权限", "<h1>只有超级管理员能管理用户</h1>", csrf))
                return self.send(200, render_users(self.state.store.snapshot(), me, csrf))
            if u.path == "/":
                projects, _, _ = collect_all()
                return self.send(200, render_overview(projects, csrf))
            if u.path == "/global":
                projects, lst, _ = collect_all()
                return self.send(200, render_global(projects, lst, csrf))
            if u.path == "/new":
                return self.send(200, render_new(csrf))
            if u.path.startswith("/project/"):
                name = urllib.parse.unquote(u.path[len("/project/"):])
                entry = next((r for r in load_registry() if r["name"] == name), None)
                if not valid_name(name) or not entry:
                    return self.send(404, page("未找到", "<h1>项目未登记</h1>", csrf))
                p = collect_project(entry, listeners())
                tools, findings, decl = (None, [], []) if p.get("missing") else project_env(name)
                return self.send(200, render_project(p, tools, findings, decl, csrf))
            if u.path == "/logs":
                name, unit = q.get("project", ""), q.get("unit", "")
                if not valid_name(name) or not valid_unit(unit) or name not in registered_names():
                    return self.send(400, page("错误", "<h1>参数不对</h1>", csrf))
                rc, out, err = service_log(name, unit)
                body = ('<p><a href="/project/%s">← %s</a></p><h1>%s 最近 100 行日志</h1><pre>%s</pre>%s'
                        % (esc(urllib.parse.quote(name)), esc(name), esc(unit), esc(out.strip() or "（没有日志）"),
                           "<pre>%s</pre>" % esc(err) if err.strip() else ""))
                return self.send(200, page("日志", body, csrf))
        except Exception as e:  # 页面不因单项采集失败而整体崩溃
            return self.send(500, page("错误", "<h1>内部错误</h1><pre>%s</pre>" % esc(repr(e)), csrf))
        self.send(404, page("未找到", "<h1>404</h1>", csrf))

    # --- POST ---
    def do_POST(self):
        _ctx.user = None
        if not self.guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        form = self.read_form()
        if form is None:
            return
        if path == "/login":
            return self.do_login(form)
        cu = self.current_user()
        if not cu:
            return self.send(401, page("未登录", '<h1>未登录</h1><p><a href="/login">去登录</a></p>'))
        me, user = cu
        if not check_csrf(self.state.secret, self.sid(), form.get("csrf", "")):
            audit(self.state.log_file, "csrf_rejected", {"path": path, "user": me}, 403, self.who())
            return self.send(403, page("错误", "<h1>CSRF 校验失败</h1>"))
        csrf = self.csrf()
        if path == "/logout":
            self.state.sessions.pop(self.sid(), None)
            return self.redirect("/login", [("Set-Cookie", "%s=; Max-Age=0; %s" % (COOKIE, self.cookie_attrs()))])
        if needs_password_change(user, path):
            return self.redirect("/password")
        _ctx.user = {"name": me, "role": user["role"], "must_change": user.get("must_change")}
        if path == "/password":
            return self.do_password(form, me, user, csrf)
        if path == "/users/action":
            if user["role"] != "super":
                return self.send(403, page("无权限", "<h1>只有超级管理员能管理用户</h1>", csrf))
            return self.do_users_action(form, me, csrf)
        if path != "/action":
            return self.send(404, page("未找到", "<h1>404</h1>", csrf))
        try:
            action, params, args, stdin = build_action(form)
        except ValueError as e:
            return self.send(400, page("参数错误", '<h1>参数错误</h1><div class="problem">%s</div><p><a href="javascript:history.back()">返回</a></p>' % esc(e), csrf))
        if action in CONFIRM_ACTIONS and form.get("confirmed") != "1":
            return self.send(200, render_confirm(form, csrf))
        with self.state.lock:
            rc, out, err = run_cmd(args, stdin_text=stdin, timeout=120)
        params["by"] = me
        audit(self.state.log_file, action, params, rc, self.who())
        self.send(200, render_result("操作结果：" + action, args, rc, out, err, csrf))

    def do_login(self, form):
        st = self.state
        if not token_equal(make_csrf(st.secret, "login"), form.get("csrf", "")):
            return self.send(403, page("错误", "<h1>CSRF 校验失败</h1>"))
        ip = self.who()
        left = st.limiter.locked_for(ip)
        if left > 0:
            return self.login_page(429, "失败次数过多，请 %d 分钟后再试" % ((left + 59) // 60))
        username, password = form.get("username", "").strip()[:64], form.get("password", "")[:256]
        shown = username if valid_username(username) else "?"
        if st.store.authenticate(username, password):
            st.limiter.record_success(ip)
            sid = st.new_session(username)
            audit(st.log_file, "login", {"user": shown}, 0, ip)
            return self.redirect("/", [("Set-Cookie", "%s=%s; %s; Max-Age=%d" % (COOKIE, sid, self.cookie_attrs(), SESSION_TTL))])
        locked = st.limiter.record_failure(ip)
        audit(st.log_file, "login", {"user": shown}, 1, ip)
        if locked:
            audit(st.log_file, "login_locked", {"minutes": int(st.limiter.lock_seconds // 60)}, 429, ip)
        self.login_page(401, "用户名或密码不对")

    def do_password(self, form, me, user, csrf):
        st = self.state
        old, new, confirm = form.get("old", ""), form.get("new", ""), form.get("confirm", "")
        forced = bool(user.get("must_change"))

        def fail(msg):
            audit(st.log_file, "password_change", {"user": me}, 1, self.who())
            self.send(400, render_password(csrf, me, msg, forced))

        if not verify_password(old, user["hash"]):
            return fail("当前密码不对")
        if new != confirm:
            return fail("两次输入的新密码不一致")
        problem = password_problem(new, me, user["hash"])
        if problem:
            return fail(problem)
        st.store.set_password(me, new, must_change=False)
        st.drop_sessions(me, keep=self.sid())
        if me == "admin":
            try:
                os.unlink(st.initial_pw_file)
            except OSError:
                pass
        audit(st.log_file, "password_change", {"user": me}, 0, self.who())
        self.redirect("/")

    def do_users_action(self, form, me, csrf):
        st = self.state
        action, target = form.get("action", ""), form.get("username", "").strip()
        if action not in ("create_user", "reset_user", "delete_user"):
            return self.send(400, page("参数错误", "<h1>未知操作</h1>", csrf))
        if action in ("reset_user", "delete_user") and form.get("confirmed") != "1":
            if target == me:
                return self.send(400, page("参数错误", '<h1>不能对自己执行此操作</h1>', csrf))
            return self.send(200, render_confirm(form, csrf, post_to="/users/action", texts=USER_CONFIRM_TEXT))
        try:
            with st.lock:
                if action == "create_user":
                    pw = st.store.create(target, "admin")
                    result = ("新建管理员 %s" % target, "管理员 %s 已创建，角色为普通管理员，可做全部项目操作，不能管理用户。" % target)
                elif action == "reset_user":
                    if target == me:
                        raise UserError("请用「修改密码」改自己的密码")
                    pw = st.store.reset(target)
                    st.drop_sessions(target)
                    result = ("重置 %s 的密码" % target, "用户 %s 的旧密码已失效，已注销其全部会话。" % target)
                else:
                    st.store.delete(me, target)
                    st.drop_sessions(target)
                    pw = None
                    result = ("删除用户 %s" % target, "用户 %s 已删除，已注销其全部会话。" % target)
        except UserError as e:
            audit(st.log_file, action, {"target": target, "by": me}, 1, self.who())
            return self.send(400, page("操作失败", '<h1>操作失败</h1><div class="problem">%s</div><p><a href="/users">返回用户管理</a></p>' % esc(e), csrf))
        audit(st.log_file, action, {"target": target, "by": me}, 0, self.who())
        if pw is None:
            return self.send(200, page(result[0], '<h1>%s</h1><div class="card"><p>%s</p></div><p><a href="/users">返回用户管理</a></p>' % (esc(result[0]), esc(result[1])), csrf))
        self.send(200, render_secret_result(result[0], result[1], pw, csrf))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    ssl_ctx = None

    def process_request_thread(self, request, client_address):
        # TLS 握手放在工作线程里并带超时，慢速连接不会堵住 accept 循环
        if self.ssl_ctx is not None:
            try:
                request.settimeout(15)
                request = self.ssl_ctx.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                self.shutdown_request(request)
                return
        super().process_request_thread(request, client_address)


def is_loopback(bind):
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def make_tls_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def make_servers(binds, port, state, tls_ctx=None):
    """每个监听地址一个 Server，共用同一份状态。"""
    handler = type("BoundHandler", (Handler,), {"state": state})
    servers = []
    for b in binds:
        cls = Server if ":" not in b else type("Server6", (Server,), {"address_family": socket.AF_INET6})
        srv = cls((b, port), handler)
        srv.ssl_ctx = tls_ctx
        servers.append(srv)
    return servers


def main(argv=None):
    ap = argparse.ArgumentParser(description="/srv 项目管理面板")
    ap.add_argument("--bind", action="append", help="监听地址，可重复，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=9300)
    ap.add_argument("--allow-remote", action="store_true", help="允许绑定非回环地址（还必须同时提供 TLS 证书）")
    ap.add_argument("--allowed-host", action="append", default=[], help="额外允许的 Host 头主机名/IP（局域网访问时填服务器 IP）")
    ap.add_argument("--allow-from", action="append", default=[], metavar="CIDR", help="只允许这些来源网段访问，可重复；默认不限制")
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    ap.add_argument("--users-file", default=USERS_FILE)
    ap.add_argument("--initial-password-file", default=INITIAL_PW_FILE)
    ap.add_argument("--log-file", default=LOG_FILE)
    ap.add_argument("--bootstrap", action="store_true", help="用户库不存在时创建 admin 并写初始密码文件")
    ap.add_argument("--reset-admin", action="store_true", help="重置 admin 的初始密码；admin 不存在则清空用户库重建")
    ap.add_argument("--print-san", metavar="IP", help="校验局域网 IP 并打印证书 SAN，供安装脚本使用")
    ap.add_argument("--check-cidr", metavar="CIDR", help="校验 CIDR 格式，供安装脚本使用")
    a = ap.parse_args(argv)
    if a.print_san:
        try:
            print(build_san(a.print_san))
        except ValueError as e:
            sys.exit(str(e))
        return
    if a.check_cidr:
        try:
            parse_allow_from([a.check_cidr])
        except ValueError:
            sys.exit("不是有效的网段（CIDR）：%s" % a.check_cidr)
        return
    if os.geteuid() != 0:
        sys.exit("需要 root 运行")
    if a.bootstrap or a.reset_admin:
        try:
            print(bootstrap_store(a.users_file, a.initial_password_file, reset=a.reset_admin))
        except UserError as e:
            sys.exit(str(e))
        return
    binds = a.bind or ["127.0.0.1"]
    remote = [b for b in binds if not is_loopback(b)]
    if remote and not a.allow_remote:
        sys.exit("拒绝绑定非回环地址 %s；确需局域网访问请显式加 --allow-remote" % "、".join(remote))
    if remote and not (a.tls_cert and a.tls_key):
        sys.exit("绑定非回环地址必须同时提供 --tls-cert 和 --tls-key，不接受明文 HTTP")
    if bool(a.tls_cert) != bool(a.tls_key):
        sys.exit("--tls-cert 与 --tls-key 须同时提供")
    try:
        nets = parse_allow_from(a.allow_from)
        store = UserStore(a.users_file)
        tls = make_tls_context(a.tls_cert, a.tls_key) if a.tls_cert else None
    except (ValueError, UserError, OSError, ssl.SSLError) as e:
        sys.exit("启动检查失败：%s" % e)
    if not store.names():
        sys.exit("用户库为空或不存在：请先 sudo bash install_panel.sh（或 python3 srv_panel.py --bootstrap）")
    state = State(store, a.port, a.allowed_host, a.log_file, nets, secure=tls is not None,
                  initial_pw_file=a.initial_password_file)
    try:
        servers = make_servers(binds, a.port, state, tls)
    except OSError as e:
        sys.exit("无法监听：%s" % e)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("srv-panel 监听 %s:%d（%s）" % ("、".join(binds), a.port, "HTTPS" if tls else "HTTP"), flush=True)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
