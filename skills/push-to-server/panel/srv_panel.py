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
    python3 srv_panel.py invite create <项目> --label L [--hours N] [--from X] | list | revoke <项目> <编号>
    python3 srv_panel.py api list|status|env|info|invites|logs|svc …      （srv-project 的 JSON 子命令转调到这里）

纯函数（解析、校验、密码、限流）可直接 import 测试；导入时不检查 root、不启动服务。
"""
import argparse
import base64
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
CONFIRM_ACTIONS = ("disable", "revoke", "svc_stop", "delete")


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


def disk_usage_kb(name):
    rc, out, _ = run_cmd(["du", "-sk", "--one-file-system", os.path.join(SRV_ROOT, name)], timeout=30)
    if rc != 0 and not out:
        return None
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return None


def format_kb(kb):
    if kb is None:
        return "未知"
    return "%.1f MB" % (kb / 1024) if kb < 1024 * 1024 else "%.2f GB" % (kb / 1024 / 1024)


def disk_usage(name):
    return format_kb(disk_usage_kb(name))


def load_registry():
    return parse_registry(read_text(REGISTRY))


def collect_project(entry, lst, keys=True):
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
    d["keys"] = project_keys(name) if keys else []
    d["services"], d["services_err"] = project_services(name, uid, d["disabled"])
    d["head"] = project_head(name)
    d["disk_kb"] = disk_usage_kb(name)
    d["disk"] = format_kb(d["disk_kb"])
    return d


def collect_all(keys=True):
    lst = listeners()
    reg = load_registry()
    with ThreadPoolExecutor(max_workers=4) as ex:
        projects = list(ex.map(lambda e: collect_project(e, lst, keys), reg))
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
MIN_PW_LEN = 8
PW_RULE = "至少 %d 位，须同时包含大写字母、小写字母、数字和特殊符号" % MIN_PW_LEN
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
    if len(new) > 128:
        return "新密码不超过 128 位"
    if (len(new) < MIN_PW_LEN or not any(c.isascii() and c.isupper() for c in new)
            or not any(c.isascii() and c.islower() for c in new) or not any(c.isdigit() for c in new)
            or not any(not c.isalnum() and not c.isspace() for c in new)):
        return "新密码" + PW_RULE
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


# ───────────────────────── 设备邀请（邀请码制登记） ─────────────────────────

INVITES_FILE = "/etc/srv-panel/invites.json"
PANEL_CONF = "/etc/srv-panel/panel.conf"
INVITE_PREFIX = "srvinvite1."
DEVICE_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")
ENROLL_MAX_BODY = 8 * 1024
GENERIC_INVALID = "邀请码无效或已失效"
STATUS_TEXT = {"pending": "待使用", "used": "已使用", "revoked": "已作废", "expired": "已过期"}
ALLOWED_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
MIN_RSA_BITS = 3072


class InviteError(ValueError):
    """生成/管理邀请时的中文错误。"""


class EnrollError(Exception):
    """登记请求被拒：msg 对外可见，status 为 HTTP 状态码，why 只进审计日志。"""

    def __init__(self, msg, status=400, why="bad_request"):
        super().__init__(msg)
        self.msg, self.status, self.why = msg, status, why


def new_code():
    return secrets.token_urlsafe(24)       # 192 bit


def code_hash(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def iso_utc(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso_utc(text):
    import calendar
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def encode_invite(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    import base64
    return INVITE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def invite_status(rec, now=None):
    """pending 的邀请过期按时间计算；其余状态原样返回。"""
    st = rec.get("status", "pending")
    if st == "pending":
        try:
            if (now if now is not None else time.time()) >= parse_iso_utc(rec["expires"]):
                return "expired"
        except (KeyError, ValueError):
            return "expired"
    return st


def parse_host_keys(texts):
    """ssh_host_*_key.pub 文件内容列表 → ["类型 base64", ...]（去掉注释字段）。"""
    out = []
    for t in texts:
        parts = t.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9@.-]+", parts[0]) and re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
            line = parts[0] + " " + parts[1]
            if line not in out:
                out.append(line)
    return out


def cert_sha256_hex(pem_text):
    """PEM 证书 → DER 的 SHA256（hex）。"""
    der = ssl.PEM_cert_to_DER_cert(pem_text.strip())
    return hashlib.sha256(der).hexdigest()


def normalize_from_spec(spec):
    """邀请的来源限制：单个 IP 或 CIDR（可逗号分隔多个）。返回规范化文本，空串表示不限制。"""
    spec = re.sub(r"\s*,\s*", ",", (spec or "").strip())
    if not spec:
        return ""
    if not FROM_RE.fullmatch(spec) or len(spec) > 200:
        raise InviteError("来源限制格式不对（用 IP 或 CIDR，多个用逗号分隔）")
    for part in spec.split(","):
        try:
            ipaddress.ip_network(part.strip(), strict=False)
        except ValueError:
            raise InviteError("来源限制格式不对：%s" % part)
    return ",".join(p.strip() for p in spec.split(","))


def invite_from_allows(spec, ip_text):
    """邀请自带的来源限制；与面板 --allow-from 不同，回环地址不享受豁免。"""
    if not spec:
        return True
    ip = normalize_ip(ip_text)
    if ip is None:
        return False
    for part in spec.split(","):
        try:
            if ip in ipaddress.ip_network(part.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def label_conflict(label, key_labels, pending_labels):
    """label 不能与已授权公钥标签或同项目 pending 邀请重复。返回中文原因或 None。"""
    if label in key_labels:
        return "标签 %s 已被该项目的一把已授权公钥使用" % label
    if label in pending_labels:
        return "标签 %s 已有一条待使用的邀请" % label
    return None


def _ssh_string(buf, pos):
    n = int.from_bytes(buf[pos:pos + 4], "big")
    return buf[pos + 4:pos + 4 + n], pos + 4 + n


def rsa_bits(blob):
    """ssh-rsa 公钥 blob → 模数位数。"""
    t, pos = _ssh_string(blob, 0)
    if t != b"ssh-rsa":
        raise ValueError("不是 RSA 公钥")
    _, pos = _ssh_string(blob, pos)
    n, _ = _ssh_string(blob, pos)
    return int.from_bytes(n, "big").bit_length()


def validate_pubkey(line):
    """校验登记用公钥：单行、不是私钥、类型在白名单内、RSA 至少 3072 位。返回「类型 base64」。"""
    import base64
    if not isinstance(line, str) or "PRIVATE KEY" in line:
        raise ValueError("只接受公钥，不接受私钥")
    text = line.strip()
    if not text or "\n" in text or "\r" in text or len(text) > 4096:
        raise ValueError("公钥须为单行")
    parts = text.split()
    if len(parts) < 2 or parts[0] not in ALLOWED_KEY_TYPES:
        raise ValueError("不支持的公钥类型（支持 ed25519、ecdsa、rsa）")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        t, _ = _ssh_string(blob, 0)
        if t.decode("ascii", "replace") != parts[0]:
            raise ValueError("公钥内容与类型不符")
        if parts[0] == "ssh-rsa" and rsa_bits(blob) < MIN_RSA_BITS:
            raise ValueError("RSA 公钥至少 %d 位" % MIN_RSA_BITS)
    except (ValueError, TypeError, IndexError) as e:
        raise ValueError(str(e) if "公钥" in str(e) or "RSA" in str(e) else "公钥格式不对")
    return parts[0] + " " + parts[1]


def parse_enroll_body(raw):
    """登记请求体（JSON）→ (code, pubkey, device_name)。格式不对抛 EnrollError。"""
    if len(raw) > ENROLL_MAX_BODY:
        raise EnrollError("请求体过大", 413, "too_large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise EnrollError("请求不是合法的 JSON", 400, "bad_json")
    if not isinstance(data, dict):
        raise EnrollError("请求格式不对", 400, "bad_shape")
    code, pubkey, device = data.get("code"), data.get("pubkey"), data.get("device_name")
    if not isinstance(code, str) or not 16 <= len(code) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        raise EnrollError(GENERIC_INVALID, 403, "invalid_code")
    if not isinstance(pubkey, str):
        raise EnrollError("缺少公钥", 400, "bad_shape")
    if not isinstance(device, str) or not DEVICE_RE.fullmatch(device):
        raise EnrollError("设备名只能用字母、数字、. _ 空格 -，最长 64 个字符", 400, "bad_device")
    return code, pubkey, device


def load_panel_conf(path=PANEL_CONF):
    """安装时写入的 {lan_ip, port, tls_cert}；面板服务和 CLI 共用，保证两边一致。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise InviteError("读取 %s 失败：%s" % (path, e))
    if not isinstance(d, dict):
        raise InviteError("%s 格式不对" % path)
    return d


def sshd_port():
    rc, out, _ = run_cmd(["sshd", "-T"], timeout=10)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "port" and parts[1].isdigit():
            return int(parts[1])
    return 22


def _file_lock(path):
    """跨进程文件锁（CLI 与面板服务同时改邀请库时用）；没有 fcntl 的平台退化为无锁。"""
    import contextlib

    @contextlib.contextmanager
    def cm():
        try:
            import fcntl
        except ImportError:
            yield
            return
        fd = os.open(path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    return cm()


class InviteStore:
    """邀请库。只存 code 的 SHA256，不存明文；每次访问先检查文件是否被别的进程改过。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.invites = {}
        self._sig = None
        if not os.path.exists(path):
            write_file_atomic(path, self._dump({}))
        self._refresh()

    @staticmethod
    def _dump(invites):
        return json.dumps({"version": 1, "invites": invites}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _signature(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _refresh(self):
        sig = self._signature()
        if sig != self._sig or self._sig is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                inv = data.get("invites") if isinstance(data, dict) else None
                if not isinstance(inv, dict):
                    raise ValueError("格式不对")
            except FileNotFoundError:
                inv = {}
            except (OSError, ValueError) as e:
                raise InviteError("邀请库读取失败：%s" % e)
            self.invites = inv
            self._sig = sig

    def _save(self):
        write_file_atomic(self.path, self._dump(self.invites))
        self._sig = self._signature()

    def _mutate(self, fn):
        with self.lock, _file_lock(self.path):
            self._sig = None            # 拿到锁后强制重读，避免覆盖别的进程刚写的内容
            self._refresh()
            result = fn()
            self._save()
            return result

    def snapshot(self):
        with self.lock:
            self._refresh()
            return {k: dict(v) for k, v in self.invites.items()}

    def find_by_code(self, code):
        h = code_hash(code)
        with self.lock:
            self._refresh()
            for rec in self.invites.values():
                if hmac.compare_digest(rec.get("code_sha256", ""), h):
                    return dict(rec)
        return None

    def add(self, rec):
        def go():
            self.invites[rec["id"]] = rec
        self._mutate(go)

    def revoke(self, inv_id, now=None, project=None):
        def go():
            rec = self.invites.get(inv_id)
            if not rec or (project is not None and rec.get("project") != project):
                raise InviteError("找不到邀请：%s" % inv_id)
            st = invite_status(rec, now)
            if st != "pending":
                raise InviteError("邀请 %s 当前状态为「%s」，不能作废" % (inv_id, STATUS_TEXT.get(st, st)))
            rec["status"] = "revoked"
            rec["revoked_at"] = iso_utc(now if now is not None else time.time())
            return dict(rec)
        return self._mutate(go)

    def mark_used(self, inv_id, ip, device, fingerprint, now=None):
        def go():
            rec = self.invites[inv_id]
            if invite_status(rec, now) != "pending":
                raise InviteError(GENERIC_INVALID)
            rec.update({"status": "used", "used_at": iso_utc(now if now is not None else time.time()),
                        "used_ip": ip, "used_device": device, "used_fingerprint": fingerprint})
        self._mutate(go)


def make_invite(store, conf, project, label, hours, frm, created_by, now=None, sshd_port_fn=None):
    """生成邀请，返回 (邀请文本, 记录)。项目须已登记且启用；label 不能与已授权标签或 pending 邀请重复。"""
    now = now if now is not None else time.time()
    if not valid_name(project) or project not in registered_names():
        raise InviteError("项目未登记：%s" % project)
    uid, member = account_info(project)
    if not member:
        raise InviteError("%s 不是 %s 项目账号" % (project, GROUP))
    if is_disabled(project):
        raise InviteError("项目 %s 已停用，不能生成邀请" % project)
    if not LABEL_RE.fullmatch(label or ""):
        raise InviteError("标签只能用字母、数字和 . _ @ -（1–64 字符）")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        raise InviteError("有效小时须为 1–168 的整数")
    if not 1 <= hours <= 168:
        raise InviteError("有效小时须为 1–168 的整数")
    frm = normalize_from_spec(frm)
    for key in ("lan_ip", "port", "tls_cert"):
        if not conf.get(key):
            raise InviteError("面板未安装或 %s 缺少 %s，请先运行 install_panel.sh" % (PANEL_CONF, key))
    key_labels = {k["label"] for k in (parse_key_line(l) for l in read_text(os.path.join(KEYS_DIR, project)).splitlines()) if k}
    snap = store.snapshot()
    pending = {r["label"] for r in snap.values() if r.get("project") == project and invite_status(r, now) == "pending"}
    problem = label_conflict(label, key_labels, pending)
    if problem:
        raise InviteError(problem)
    host_files = []
    import glob
    for f in sorted(glob.glob("/etc/ssh/ssh_host_*_key.pub")):
        host_files.append(read_text(f))
    host_keys = parse_host_keys(host_files)
    if not host_keys:
        raise InviteError("读不到 /etc/ssh/ssh_host_*_key.pub，无法生成邀请")
    try:
        fp = cert_sha256_hex(read_text(conf["tls_cert"]))
    except (ValueError, OSError) as e:
        raise InviteError("读取面板证书失败：%s" % e)
    code = new_code()
    ssh_port = (sshd_port_fn or sshd_port)()
    expires = iso_utc(now + hours * 3600)
    payload = {"v": 1, "host": conf["lan_ip"], "ssh_port": ssh_port, "project": project,
               "panel": "https://%s:%s" % (conf["lan_ip"], conf["port"]), "panel_cert_sha256": fp,
               "ssh_host_keys": host_keys, "code": code, "expires": expires, "label": label}
    rec = {"id": secrets.token_hex(4), "code_sha256": code_hash(code), "project": project, "label": label,
           "from": frm, "created_by": created_by, "created": iso_utc(now), "expires": expires,
           "ssh_port": ssh_port, "status": "pending"}
    store.add(rec)
    return encode_invite(payload), rec


def invite_summary(rec, now=None):
    st = invite_status(rec, now)
    s = "%s  %s  %s  %s  过期 %s" % (rec["id"], rec["project"], rec["label"], STATUS_TEXT.get(st, st), rec["expires"])
    if rec.get("from"):
        s += "  限来源 %s" % rec["from"]
    if rec.get("status") == "used":
        s += "  [%s 从 %s 登记，设备 %s，指纹 %s]" % (rec.get("used_at", ""), rec.get("used_ip", ""),
                                                  rec.get("used_device", ""), rec.get("used_fingerprint", ""))
    return s


def process_enroll(state, raw, ip, grant=None, info_fn=None):
    """处理 POST /enroll。返回 (HTTP 状态码, 响应 dict)。同一个码并发提交只会成功一次。"""
    lim = state.enroll_limiter

    def fail(e, count=True, extra=None):
        if count:
            lim.record_failure(ip)
        params = {"result": e.why}
        params.update(extra or {})
        audit(state.log_file, "enroll", params, 1, ip)
        return e.status, {"ok": False, "error": e.msg}

    left = lim.locked_for(ip)
    if left > 0:
        audit(state.log_file, "enroll", {"result": "locked"}, 429, ip)
        return 429, {"ok": False, "error": "尝试次数过多，请 %d 分钟后再试" % ((left + 59) // 60)}
    try:
        code, pubkey, device = parse_enroll_body(raw)
    except EnrollError as e:
        return fail(e)
    with state.enroll_lock:
        try:
            rec = state.invites.find_by_code(code)
        except InviteError as e:
            return fail(EnrollError("服务端邀请库异常", 500, "store_error"), count=False)
        if not rec or invite_status(rec) != "pending":
            return fail(EnrollError(GENERIC_INVALID, 403, "invalid_code"))
        extra = {"invite": rec["id"], "project": rec["project"], "device": device}
        if not invite_from_allows(rec.get("from", ""), ip):
            return fail(EnrollError("来源地址不在邀请允许的范围内", 403, "from_denied"), extra=extra)
        try:
            pub = validate_pubkey(pubkey)
        except ValueError as e:
            return fail(EnrollError(str(e), 400, "bad_pubkey"), extra=extra)
        project = rec["project"]
        uid, member = account_info(project)
        if project not in registered_names() or not member or is_disabled(project):
            return fail(EnrollError("项目不存在或已停用", 409, "project_unavailable"), count=False, extra=extra)
        args = [SRV_PROJECT, "grant", project, "-", "--label", rec["label"]]
        if rec.get("from"):
            args += ["--from", rec["from"]]
        with state.lock:
            rc, out, err = (grant or run_cmd)(args, stdin_text=pub + "\n", timeout=60)
        if rc != 0:
            reason = (err.strip() or out.strip() or "未知错误").splitlines()[0]
            return fail(EnrollError("授权失败：" + reason, 409, "grant_failed"), count=False, extra=extra)
        fp = key_fingerprint(pub)
        try:
            state.invites.mark_used(rec["id"], ip, device, fp)
        except InviteError as e:
            return fail(EnrollError(GENERIC_INVALID, 403, "invalid_code"), extra=extra)
        info = (info_fn or project_info)(project, state.lan_ip)
    lim.record_success(ip)
    audit(state.log_file, "enroll", dict(extra, result="ok", fingerprint=fp), 0, ip)
    return 200, {"ok": True, "project": project, "account": project, "ssh_port": rec.get("ssh_port", 22),
                 "host": state.lan_ip, "fingerprint": fp, "info": info}


def invite_cli(argv):
    """srv_panel.py invite create|list|revoke …（root 运行，直接读写邀请库）。"""
    ap = argparse.ArgumentParser(prog="srv_panel.py invite", description="设备邀请管理")
    ap.add_argument("--invites-file", default=INVITES_FILE)
    ap.add_argument("--panel-conf", default=PANEL_CONF)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="生成邀请")
    c.add_argument("project")
    c.add_argument("--label", required=True)
    c.add_argument("--hours", type=int, default=24)
    c.add_argument("--from", dest="frm", default="")
    sub.add_parser("list", help="列出邀请").add_argument("--project")
    rv = sub.add_parser("revoke", help="作废邀请")
    rv.add_argument("project")
    rv.add_argument("id")
    a = ap.parse_args(argv)
    try:
        store = InviteStore(a.invites_file)
        if a.cmd == "create":
            who = "cli:" + (os.environ.get("SUDO_USER") or "root")
            text, rec = make_invite(store, load_panel_conf(a.panel_conf), a.project, a.label, a.hours, a.frm, who)
            print(text)
            sys.stderr.write("邀请 %s（项目 %s，标签 %s）有效至 %s UTC。上面这一行原样转发给申请方，仅此一次显示。\n"
                             % (rec["id"], rec["project"], rec["label"], rec["expires"]))
        elif a.cmd == "list":
            rows = [r for r in store.snapshot().values() if not a.project or r.get("project") == a.project]
            for r in sorted(rows, key=lambda r: r["created"]):
                print(invite_summary(r))
            if not rows:
                print("（没有邀请）")
        else:
            rec = store.revoke(a.id, project=a.project)
            print("已作废邀请 %s（项目 %s，标签 %s）" % (rec["id"], rec["project"], rec["label"]))
    except InviteError as e:
        sys.exit("错误：%s" % e)
    audit(LOG_FILE, "invite_" + a.cmd, {k: v for k, v in vars(a).items() if k in ("project", "label", "id", "hours", "frm")},
          0, "cli:" + (os.environ.get("SUDO_USER") or "root"))


# ───────────────────────── JSON 接口（srv-project 的 list/status/env/info/invites/logs/svc 转调到这里） ─────────────────────────

def ssh_fingerprint(b64):
    """公钥 blob（base64）→ SHA256:xxxx（与 ssh-keygen -l 同格式），纯计算不起子进程。"""
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode("ascii").rstrip("=")


def key_entries(lines):
    out = []
    for line in lines:
        k = parse_key_line(line)
        if not k:
            continue
        try:
            fp = ssh_fingerprint(k["blob"])
        except (ValueError, TypeError):
            fp = ""
        out.append({"label": k["label"], "fingerprint": fp, "from": k["from"] or None})
    return out


def _port_int(text):
    return int(text) if str(text).isdigit() else None


def light_record(entry, exists, disabled, key_lines):
    """list --json 的一条记录：只来自登记表、shadow 和公钥文件。"""
    return {"name": entry["name"], "port": _port_int(entry["port"]), "contact": entry["contact"],
            "created": entry["created"], "account_exists": bool(exists),
            "disabled": bool(disabled) if exists else False,
            "keys": key_entries(key_lines) if exists else []}


HEAD_RE = re.compile(r"^[0-9a-f]{7,40}\s")


def status_record(light, d):
    """status --json 的一条记录。d 是 collect_project 的结果；账号缺失或没有采集时各字段补成 null / []，结构保持一致。"""
    rec = dict(light)
    rec.update({"listening": False, "listen_owner": None, "services": [], "head": None, "disk_kb": None,
                "problems": [], "notes": [], "services_error": None})
    if d:
        rec["problems"] = list(d.get("problems", []))
        rec["notes"] = list(d.get("notes", []))
    if d and not d.get("missing"):
        owners = d.get("owners") or []
        rec["listening"] = bool(d.get("listening"))
        rec["listen_owner"] = ",".join(owners) if owners else None
        rec["services"] = [{"unit": u["unit"], "active": u["active"], "sub": u["sub"]} for u in d.get("services", [])]
        head = d.get("head") or ""
        rec["head"] = head if HEAD_RE.match(head) else None
        rec["disk_kb"] = d.get("disk_kb")
        rec["services_error"] = d.get("services_err") or None
    return rec


def scan_srv_dirs(registered):
    """/srv 下的目录：对 other 可读的、以及未登记的（不含以 . 开头的，如 .archived）。"""
    world, extra = [], []
    try:
        for n in sorted(x for x in os.listdir(SRV_ROOT) if not x.startswith(".")):
            full = os.path.join(SRV_ROOT, n)
            if os.path.isdir(full) and not os.path.islink(full):
                if os.stat(full).st_mode & stat.S_IROTH:
                    world.append(full)
                if n not in registered:
                    extra.append(full)
    except OSError:
        pass
    return world, extra


def global_record(listeners_, ports, world, extra, sshd_rule, srv_project_ok):
    return {"sshd_rule": bool(sshd_rule), "world_readable": list(world),
            "listeners": [{"port": r["port"], "owner": ",".join(r["owners"]) or None, "project": ports.get(str(r["port"]))}
                          for r in sorted(listeners_, key=lambda r: (r["port"], r["addr"]))],
            "srv_project": bool(srv_project_ok), "unregistered_dirs": list(extra)}


def env_record(name, disabled, tools, findings, decl):
    return {"project": name, "disabled": bool(disabled),
            "tools": [{"name": t, "available": bool((tools[t] or {}).get("path")) if t in tools else False,
                       "version": (tools.get(t) or {}).get("version") or None,
                       "path": (tools.get(t) or {}).get("path") or None} for t in TOOLS] if tools else [],
            "declarations": [{"file": src, "requirement": spec} for src, spec in decl],
            "findings": [text for _, text in findings],
            "findings_detail": [{"level": lvl, "text": text} for lvl, text in findings]}


def info_record(name, entry, host, ssh_port, disabled, keys, text):
    return {"project": name, "host": host, "ssh_port": int(ssh_port), "account": name,
            "dir": "%s/%s" % (SRV_ROOT, name), "port": _port_int(entry["port"]), "disabled": bool(disabled),
            "keys": keys, "text": text}


def invites_record(invites, project, now=None):
    rows = []
    for rec in sorted(invites.values(), key=lambda r: (r.get("created", ""), r.get("id", ""))):
        if rec.get("project") != project:
            continue
        used = None
        if rec.get("status") == "used":
            used = {"at": rec.get("used_at"), "ip": rec.get("used_ip"), "device": rec.get("used_device"),
                    "fingerprint": rec.get("used_fingerprint")}
        rows.append({"id": rec["id"], "project": rec["project"], "label": rec["label"], "from": rec.get("from") or None,
                     "created": rec.get("created"), "expires": rec.get("expires"),
                     "status": invite_status(rec, now), "used": used})
    return {"invites": rows}


def merge_panel_conf(existing, lan_ip, port, tls_cert, enroll_only=None):
    """合并安装时写入的面板配置。enroll_only 为 None 表示保持原值（没有原值则为 False）。"""
    conf = dict(existing or {})
    conf.update({"lan_ip": lan_ip, "port": int(port), "tls_cert": tls_cert})
    conf["enroll_only"] = bool(conf.get("enroll_only", False)) if enroll_only is None else bool(enroll_only)
    return conf


def write_panel_conf(path, lan_ip, port, tls_cert, enroll_only=None):
    conf = merge_panel_conf(load_panel_conf(path), lan_ip, port, tls_cert, enroll_only)
    write_file_atomic(path, json.dumps(conf, ensure_ascii=False, sort_keys=True) + "\n", mode=0o644)
    return conf


def route_allowed(enroll_only, method, path):
    """登记模式只开放 POST /enroll，其余一律 404。"""
    return (not enroll_only) or (method == "POST" and path == "/enroll")


def _account_exists(name):
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except (ImportError, KeyError):
        return False


def light_projects():
    out = []
    for entry in load_registry():
        exists = _account_exists(entry["name"])
        lines = read_text(os.path.join(KEYS_DIR, entry["name"])).splitlines() if exists else []
        out.append(light_record(entry, exists, is_disabled(entry["name"]) if exists else False, lines))
    return out


def default_route_ip():
    rc, out, _ = run_cmd(["ip", "-4", "route", "get", "1.1.1.1"], timeout=5)
    parts = out.split()
    return parts[parts.index("src") + 1] if "src" in parts and parts.index("src") + 1 < len(parts) else ""


class ApiExit(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def _api_project(name):
    """校验项目名并要求已登记；否则退出码 2。返回登记表条目。"""
    if not valid_name(name):
        raise ApiExit("项目名不合法：%s" % name, 2)
    entry = next((r for r in load_registry() if r["name"] == name), None)
    if not entry:
        raise ApiExit("项目不存在：%s" % name, 2)
    return entry


def api_status(project=None):
    reg = load_registry()
    lst = listeners()
    light_by = {l["name"]: l for l in light_projects()}
    if project is not None:
        entry = _api_project(project)
        d = collect_project(entry, lst, keys=False)
        same = [r["name"] for r in reg if r["port"] == entry["port"] and r["name"] != entry["name"]]
        if same:
            d["problems"].append("登记端口 %s 与项目 %s 重复" % (entry["port"], "、".join(same)))
        return {"projects": [status_record(light_by[entry["name"]], d)]}
    projects, lst, reg = collect_all(keys=False)
    recs = [status_record(light_by[p["entry"]["name"]], p) for p in projects]
    ports = {p["entry"]["port"]: p["entry"]["name"] for p in projects}
    world, extra = scan_srv_dirs({r["name"] for r in reg})
    return {"projects": recs,
            "global": global_record(lst, ports, world, extra, os.path.exists(SSHD_DROPIN), os.access(SRV_PROJECT, os.X_OK))}


def api_env(project):
    _api_project(project)
    tools, findings, decl = project_env(project)
    return env_record(project, is_disabled(project), tools, findings, decl)


def api_info(project, host=""):
    entry = _api_project(project)
    host = host or load_panel_conf().get("lan_ip") or default_route_ip() or ""
    args = [SRV_PROJECT, "info", project] + (["--host", host] if host else [])
    rc, out, err = run_cmd(args, timeout=30)
    if rc != 0:
        raise ApiExit("info 失败：%s" % (err.strip() or out.strip()), 1)
    keys = key_entries(read_text(os.path.join(KEYS_DIR, project)).splitlines())
    return info_record(project, entry, host or "", sshd_port(), is_disabled(project), keys, out.strip())


def api_invites(project, invites_file=INVITES_FILE):
    _api_project(project)
    return invites_record(InviteStore(invites_file).snapshot(), project)


def api_cli(argv):
    """srv_panel.py api …（root 运行；srv-project 的新子命令转调到这里）。JSON 只写 stdout，原因写 stderr。"""
    ap = argparse.ArgumentParser(prog="srv_panel.py api")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status").add_argument("project", nargs="?")
    sub.add_parser("env").add_argument("project")
    i = sub.add_parser("info")
    i.add_argument("project")
    i.add_argument("--host", default="")
    sub.add_parser("invites").add_argument("project")
    lg = sub.add_parser("logs")
    lg.add_argument("project")
    lg.add_argument("unit")
    lg.add_argument("--lines", type=int, default=100)
    sv = sub.add_parser("svc")
    sv.add_argument("project")
    sv.add_argument("op", choices=SERVICE_OPS)
    sv.add_argument("unit")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            data = {"projects": light_projects()}
        elif a.cmd == "status":
            data = api_status(a.project)
        elif a.cmd == "env":
            data = api_env(a.project)
        elif a.cmd == "info":
            data = api_info(a.project, a.host)
        elif a.cmd == "invites":
            data = api_invites(a.project)
        elif a.cmd == "logs":
            _api_project(a.project)
            if not valid_unit(a.unit):
                raise ApiExit("服务名格式不对：%s" % a.unit, 1)
            if not 1 <= a.lines <= 1000:
                raise ApiExit("--lines 须为 1–1000", 1)
            rc, out, err = service_log(a.project, a.unit, a.lines)
            sys.stdout.write(out)
            if rc != 0:
                sys.stderr.write(err)
            return rc
        else:
            _api_project(a.project)
            if not valid_unit(a.unit):
                raise ApiExit("服务名格式不对：%s" % a.unit, 1)
            uid, member = account_info(a.project)
            if not member:
                raise ApiExit("%s 不是 %s 项目账号" % (a.project, GROUP), 2)
            rc, out, err = run_cmd(["systemctl", "--user", "--machine=%s@" % a.project, a.op, a.unit], timeout=60)
            audit(LOG_FILE, "api_svc", {"project": a.project, "unit": a.unit, "op": a.op}, rc,
                  "cli:" + (os.environ.get("SUDO_USER") or "root"))
            if rc != 0:
                raise ApiExit("systemctl %s %s 失败（退出码 %s）：%s" % (a.op, a.unit, rc, (err.strip() or out.strip())), 1)
            print("已对项目 %s 的用户服务 %s 执行 %s" % (a.project, a.unit, a.op))
            return 0
    except ApiExit as e:
        sys.stderr.write("错误：%s\n" % e)
        return e.code
    except InviteError as e:
        sys.stderr.write("错误：%s\n" % e)
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


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
    if action == "delete":
        purge = form.get("purge") == "1"
        args = [SRV_PROJECT, "delete", name] + (["--purge"] if purge else [])
        return "delete", {"project": name, "purge": purge}, args, None
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


def project_info(name, lan_ip=""):
    """srv-project info 的文本，供页面复制转发给申请方。"""
    args = [SRV_PROJECT, "info", name] + (["--host", lan_ip] if lan_ip else [])
    rc, out, err = run_cmd(args, timeout=30)
    return out.strip() if rc == 0 else "（接入信息生成失败，退出码 %s）\n%s" % (rc, err.strip())


def service_log(name, unit, lines=100):
    uid, member = account_info(name)
    if not member or name not in registered_names():
        return 1, "", "不是项目账号"
    u = str(uid)
    return run_cmd(["journalctl", "-n", str(int(lines)), "--no-pager", "-o", "short-iso",
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
            links = ('<a href="/">总览</a><a href="/global">全局检查</a><a href="/new">新建项目</a><a href="/invites">邀请</a>'
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


def render_project(p, tools, findings, decl, csrf, info_text=""):
    e = p["entry"]
    name = e["name"]
    body = '<p><a href="/">← 总览</a></p>' + project_card(p, csrf)
    if p.get("missing"):
        return page(name, body, csrf)
    # 操作
    ops = ""
    ops += btn_form(csrf, "启用", "primary", action="enable", project=name) if p["disabled"] else \
        '<form class="inline" method="post" action="/action">%s<button class="danger">停用项目…</button></form>' % hidden(csrf, action="disable", project=name)
    if p["disabled"]:
        ops += (' <form class="inline" method="post" action="/action">%s<label><input type="checkbox" name="purge" value="1"> 连同目录彻底删除（不勾则归档到 /srv/.archived）</label> '
                '<button class="danger">删除项目…</button></form>' % hidden(csrf, action="delete", project=name))
    body += '<div class="card"><h2>项目操作</h2><p>%s</p></div>' % ops
    if info_text:
        body += ('<div class="card"><h2>接入信息</h2><p class="mute">可直接复制转发给申请方；不含密钥内容和密码。</p><pre>%s</pre>'
                 '%s</div>' % (esc(info_text), '' if p["disabled"] else '<p><a href="/invites?project=%s">为此项目生成邀请</a>（新设备凭邀请自助登记公钥）</p>' % esc(urllib.parse.quote(name))))
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
        for n in sorted(x for x in os.listdir(SRV_ROOT) if not x.startswith(".")):
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
    "delete": "删除账号、授权公钥和登记表条目，不可恢复。项目目录默认归档到 /srv/.archived（属主 root）。",
}
PURGE_TEXT = "已勾选「彻底删除」：项目目录 /srv/<项目> 将被直接删除，数据无法找回。"


def render_confirm(form, csrf, post_to="/action", texts=None):
    action = form.get("action", "")
    hid = {k: v for k, v in form.items() if k not in ("csrf", "confirmed")}
    detail = "".join("<li>%s：%s</li>" % (esc(k), esc(v)) for k, v in hid.items() if k != "action")
    text = (texts or CONFIRM_TEXT).get(action, "")
    if action == "delete" and form.get("purge") == "1":
        text = PURGE_TEXT
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
                '<p>新密码（%s，不能与当前密码相同）<input type="password" name="new" autocomplete="new-password" required></p>'
                '<p>再输一次新密码<input type="password" name="confirm" autocomplete="new-password" required></p>'
                '<button class="primary">修改密码</button></form></div>' % (note, err, hidden(csrf), esc(PW_RULE)), csrf)


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


# ───────────────────────── 邀请页面 ─────────────────────────

INVITE_CONFIRM_TEXT = {
    "revoke_invite": "作废后这条邀请立即失效，申请方再用它登记会被拒绝；已经完成的登记不受影响。",
}


def enabled_projects():
    """已登记、账号在 srvproj 组且未停用的项目名。"""
    names = []
    for r in load_registry():
        uid, member = account_info(r["name"])
        if uid is not None and member and not is_disabled(r["name"]):
            names.append(r["name"])
    return names


def render_invites(csrf, projects, invites, preselect="", error=""):
    err = '<div class="problem">%s</div>' % esc(error) if error else ""
    opts = "".join('<option value="%s"%s>%s</option>' % (esc(n), " selected" if n == preselect else "", esc(n)) for n in projects)
    form = ('<div class="card"><h2>生成邀请</h2>%s<form method="post" action="/invites/action">%s'
            '<p>项目（只列已启用的项目）<select name="project" required>%s</select></p>'
            '<p>标签（设备名，字母数字 . _ @ -；登记后成为公钥标签）<input type="text" name="label" required maxlength="64"></p>'
            '<p>有效小时（1–168）<input type="text" name="hours" value="24" maxlength="3"></p>'
            '<p>来源限制（可选，IP 或 CIDR，多个用逗号分隔；同时限制登记来源和这把公钥的登录来源）<input type="text" name="from" maxlength="200"></p>'
            '<button class="primary">生成邀请</button></form></div>'
            % (err, hidden(csrf, action="create_invite"), opts or "<option disabled>（没有已启用的项目）</option>"))
    now = time.time()
    rows = ""
    for rec in sorted(invites.values(), key=lambda r: r.get("created", ""), reverse=True):
        st = invite_status(rec, now)
        cls = {"pending": "ok", "used": "mute", "revoked": "bad", "expired": "warn"}.get(st, "mute")
        used = ""
        if rec.get("status") == "used":
            used = "%s 从 %s，设备 %s，指纹 %s" % (rec.get("used_at", ""), rec.get("used_ip", ""), rec.get("used_device", ""), rec.get("used_fingerprint", ""))
        op = ""
        if st == "pending":
            op = ('<form class="inline" method="post" action="/invites/action">%s<button class="danger">作废…</button></form>'
                  % hidden(csrf, action="revoke_invite", id=rec["id"]))
        rows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (esc(rec["id"]), esc(rec["project"]), esc(rec["label"]), tag(STATUS_TEXT.get(st, st), cls),
                    esc(rec["expires"]), esc(rec.get("from") or "不限"), esc(used or rec.get("created_by", "")), op))
    table = ('<div class="card"><h2>邀请列表</h2><table><tr><th>编号</th><th>项目</th><th>标签</th><th>状态</th><th>过期（UTC）</th>'
             '<th>来源限制</th><th>创建者 / 使用信息</th><th></th></tr>%s</table></div>'
             % (rows or '<tr><td colspan="8" class="mute">还没有邀请</td></tr>'))
    return page("邀请", "<h1>设备邀请</h1>" + form + table, csrf)


def render_invite_result(text, rec, csrf):
    return page("邀请已生成", '<h1>邀请已生成</h1><div class="card"><p>项目 <b>%s</b>，标签 <b>%s</b>，有效至 %s（UTC）%s。</p>'
                '<p class="warn"><b>邀请文本只显示这一次，离开本页后无法再查看</b>（可以作废后重新生成）。</p>'
                '<textarea readonly rows="6" style="font-family:monospace">%s</textarea>'
                '<p>原样发给申请方，让他交给自己的 agent（由 agent 运行 <code>safe_push.py enroll</code>）。</p></div>'
                '<p><a href="/invites">返回邀请页</a></p>'
                % (esc(rec["project"]), esc(rec["label"]), esc(rec["expires"]),
                   "，仅限来源 " + esc(rec["from"]) if rec.get("from") else "", esc(text)), csrf)


# ───────────────────────── HTTP 服务 ─────────────────────────

class State:
    def __init__(self, store, port, extra_hosts=(), log_file=LOG_FILE, allow_nets=(), secure=False,
                 initial_pw_file=INITIAL_PW_FILE, limiter=None, lan_ip="", invites=None, enroll_only=False):
        self.store = store
        self.port = port
        self.extra_hosts = tuple(extra_hosts)
        self.log_file = log_file
        self.allow_nets = list(allow_nets)
        self.secure = secure
        self.lan_ip = lan_ip
        self.invites = invites
        self.enroll_only = enroll_only
        self.enroll_limiter = RateLimiter(5, 900, 900)     # 与登录限流分开计数
        self.enroll_lock = threading.Lock()                 # 同一个码并发提交只成功一次
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
        return self.host_guard()

    def host_guard(self):
        st = self.state
        if not host_ok(self.headers.get("Host", ""), st.port, st.extra_hosts):
            self.send(400, "<h1>Bad Host</h1>")
            return False
        return True

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False), ctype="application/json; charset=utf-8")

    def do_enroll(self):
        """POST /enroll：不需要登录、不检查 CSRF、不受 --allow-from 限制（受邀请自己的 from 限制），Host 头照常校验。"""
        if not self.host_guard():
            return
        try:
            n = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            n = -1
        if n < 0 or n > ENROLL_MAX_BODY:
            return self.send_json(413, {"ok": False, "error": "请求体过大或缺少 Content-Length"})
        raw = self.rfile.read(n)
        code, resp = process_enroll(self.state, raw, self.who())
        self.send_json(code, resp)

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
    def not_found(self):
        self.send(404, "<h1>404</h1>")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found

    def do_GET(self):
        _ctx.user = None
        if not route_allowed(self.state.enroll_only, "GET", urllib.parse.urlsplit(self.path).path):
            return self.not_found()
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
            if u.path == "/invites":
                return self.send(200, render_invites(csrf, enabled_projects(), self.state.invites.snapshot(), q.get("project", "")))
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
                info = project_info(name, self.state.lan_ip) if not p.get("missing") else ""
                return self.send(200, render_project(p, tools, findings, decl, csrf, info))
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
        path = urllib.parse.urlsplit(self.path).path
        if not route_allowed(self.state.enroll_only, "POST", path):
            return self.not_found()
        if path == "/enroll":
            return self.do_enroll()
        if not self.guard():
            return
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
        if path == "/invites/action":
            return self.do_invites_action(form, me, csrf)
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

    def do_invites_action(self, form, me, csrf):
        st = self.state
        action = form.get("action", "")
        try:
            if action == "create_invite":
                conf = load_panel_conf(getattr(st, "panel_conf", PANEL_CONF))
                conf.setdefault("lan_ip", st.lan_ip)
                conf.setdefault("port", st.port)
                conf.setdefault("tls_cert", getattr(st, "tls_cert", ""))
                text, rec = make_invite(st.invites, conf, form.get("project", "").strip(), form.get("label", "").strip(),
                                        form.get("hours", "24").strip() or "24", form.get("from", ""), "panel:" + me)
                audit(st.log_file, "invite_create", {"invite": rec["id"], "project": rec["project"], "label": rec["label"],
                                                      "hours": form.get("hours", "24"), "from": rec["from"], "by": me}, 0, self.who())
                return self.send(200, render_invite_result(text, rec, csrf))
            if action == "revoke_invite":
                inv_id = form.get("id", "").strip()
                if not re.fullmatch(r"[0-9a-f]{8}", inv_id):
                    raise InviteError("邀请编号格式不对")
                if form.get("confirmed") != "1":
                    return self.send(200, render_confirm(form, csrf, post_to="/invites/action", texts=INVITE_CONFIRM_TEXT))
                rec = st.invites.revoke(inv_id)
                audit(st.log_file, "invite_revoke", {"invite": inv_id, "project": rec["project"], "by": me}, 0, self.who())
                return self.send(200, render_invites(csrf, enabled_projects(), st.invites.snapshot(), ""))
            raise InviteError("未知操作：%s" % action)
        except InviteError as e:
            audit(st.log_file, action or "invite", {"by": me}, 1, self.who())
            return self.send(400, render_invites(csrf, enabled_projects(), st.invites.snapshot(), form.get("project", ""), str(e)))

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
    ap.add_argument("--port", type=int, default=None, help="监听端口，默认取 panel.conf 里的 port，再默认 9300")
    ap.add_argument("--panel-conf", default=PANEL_CONF)
    ap.add_argument("--write-panel-conf", action="store_true", help="安装脚本用：合并写入 panel.conf（需要 --lan-ip 与 --tls-cert，端口取 --port）")
    ap.add_argument("--lan-ip", default="")
    ap.add_argument("--enroll-only", action="store_true", help="与 --write-panel-conf 合用：打开登记模式")
    ap.add_argument("--full", action="store_true", help="与 --write-panel-conf 合用：关闭登记模式")
    ap.add_argument("--invites-file", default=INVITES_FILE)
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
    args_in = sys.argv[1:] if argv is None else list(argv)
    if args_in and args_in[0] in ("invite", "api"):
        if os.geteuid() != 0:
            sys.exit("需要 root 运行")
        if args_in[0] == "api":
            sys.exit(api_cli(args_in[1:]))
        return invite_cli(args_in[1:])
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
    if a.write_panel_conf:
        if a.enroll_only and a.full:
            sys.exit("--enroll-only 与 --full 不能同时给")
        if not (a.lan_ip and a.tls_cert and a.port):
            sys.exit("--write-panel-conf 需要 --lan-ip、--port 和 --tls-cert")
        try:
            conf = write_panel_conf(a.panel_conf, a.lan_ip, a.port, a.tls_cert,
                                    True if a.enroll_only else (False if a.full else None))
        except (InviteError, OSError) as e:
            sys.exit(str(e))
        print("enroll_only=%s" % ("true" if conf["enroll_only"] else "false"))
        return
    if a.bootstrap or a.reset_admin:
        try:
            print(bootstrap_store(a.users_file, a.initial_password_file, reset=a.reset_admin))
        except UserError as e:
            sys.exit(str(e))
        return
    try:
        conf = load_panel_conf(a.panel_conf)
    except InviteError as e:
        sys.exit(str(e))
    port = a.port or int(conf.get("port") or 9300)
    enroll_only = bool(conf.get("enroll_only"))
    a.tls_cert = a.tls_cert or conf.get("tls_cert")
    if a.tls_cert and not a.tls_key:
        a.tls_key = conf.get("tls_key") or os.path.splitext(a.tls_cert)[0] + ".key"
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
        store = None if enroll_only else UserStore(a.users_file)    # 登记模式不依赖账号体系
        tls = make_tls_context(a.tls_cert, a.tls_key) if a.tls_cert else None
        invites = InviteStore(a.invites_file)
    except (ValueError, UserError, InviteError, OSError, ssl.SSLError) as e:
        sys.exit("启动检查失败：%s" % e)
    if store is not None and not store.names():
        sys.exit("用户库为空或不存在：请先 sudo bash install_panel.sh（或 python3 srv_panel.py --bootstrap）")
    lan_ip = next((b for b in binds if not is_loopback(b) and ":" not in b), "") or conf.get("lan_ip", "")
    state = State(store, port, a.allowed_host, a.log_file, nets, secure=tls is not None,
                  initial_pw_file=a.initial_password_file, lan_ip=lan_ip, invites=invites,
                  enroll_only=enroll_only)
    state.panel_conf, state.tls_cert = a.panel_conf, a.tls_cert
    try:
        servers = make_servers(binds, port, state, tls)
    except OSError as e:
        sys.exit("无法监听：%s" % e)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("srv-panel 监听 %s:%d（%s，%s）" % ("、".join(binds), port, "HTTPS" if tls else "HTTP",
                                         "登记模式：只开放 POST /enroll" if enroll_only else "完整模式"), flush=True)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
