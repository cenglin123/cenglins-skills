r"""winxhash.py - read/compute/write the WinX shortcut hash that TWINUI validates.

Only needed if you insist on putting *custom* entries into the Win+X menu
(%LOCALAPPDATA%\Microsoft\Windows\WinX\GroupN). The supported takeover in
install.ps1 does not touch the WinX folder at all.

Mechanism
---------
Each .lnk in the WinX folder must carry a valid hash in its property store:

    PKEY {FB8D2D7B-90D1-4E34-BF60-6EAC09922BBF}:2   (VT_UI4)

    hash = HashData(shlwapi)( utf16le( lower( generalize(TargetParsingPath)
                                              + Arguments + SALT ) ) )

"generalize" replaces exactly three path prefixes with GUIDs (see GENERALIZE).
An entry whose hash does not validate simply disappears from the menu.

The salt trap
-------------
Public hashlnk sources use

    "do not prehash links. this should only be done by the user."   (one space)

but Windows 10 22H2 twinui.dll actually uses

    "Do not prehash links.  This should only be done by the user."  (two spaces)

Never trust the constant - extract it from the local twinui.dll and prove it
against a Microsoft-shipped .lnk before using it:

    python winxhash.py salt
    python winxhash.py verify

Usage
-----
    python winxhash.py salt   [twinui.dll]      # extract candidate salts locally
    python winxhash.py verify [winx-dir]        # oracle test: stored vs computed
    python winxhash.py target <lnk>             # show TargetParsingPath/Arguments
    python winxhash.py read   <lnk>             # show the stored hash
    python winxhash.py hash   <target> [args]   # compute a hash
    python winxhash.py write  <lnk> <target> [args]   # compute + store
    python winxhash.py writeval <lnk> <hex>     # store a raw hash value

Requires: Windows, Python 3, `pip install comtypes`.
"""

import os
import sys
import glob
import ctypes
from ctypes import wintypes, POINTER, byref, c_wchar_p

try:
    from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
    HAVE_COMTYPES = True
except ImportError:  # `salt` and `hash` still work without it
    HAVE_COMTYPES = False


def require_com():
    if not HAVE_COMTYPES:
        sys.exit("this command needs the property store API:  pip install comtypes")

# --- the salt actually used by Win10 22H2 twinui.dll (two spaces after "links.")
SALT = "Do not prehash links.  This should only be done by the user."

GENERALIZE = [
    ("ProgramFiles", "", "{905E63B6-C1BF-494E-B29C-65B732D3D21A}"),
    ("SystemRoot", r"\System32", "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"),
    ("SystemRoot", "", "{F38BF404-1D43-42F2-9305-67DE0B28FC23}"),
]


shell32 = ctypes.windll.shell32
shlwapi = ctypes.windll.shlwapi

# ---------- IPropertyStore (only when comtypes is available) ----------
if HAVE_COMTYPES:

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.WORD),
                    ("wReserved1", wintypes.WORD),
                    ("wReserved2", wintypes.WORD),
                    ("wReserved3", wintypes.WORD),
                    ("ulVal", ctypes.c_void_p),
                    ("extra", ctypes.c_byte * 8)]

    class IPropertyStore(IUnknown):
        _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "cProps")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], wintypes.DWORD, "iProp"), (["out"], POINTER(PROPERTYKEY), "pkey")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["out"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["in"], POINTER(PROPVARIANT), "pv")),
            COMMETHOD([], HRESULT, "Commit"),
        ]

    def _pkey(fmtid, pid):
        k = PROPERTYKEY()
        k.fmtid = GUID(fmtid)
        k.pid = pid
        return k

    PKEY_WINX_HASH = _pkey("{FB8D2D7B-90D1-4E34-BF60-6EAC09922BBF}", 2)
    PKEY_TARGET = _pkey("{B9B4B3FC-2B51-4A42-B5D8-324146AFCF25}", 2)
    PKEY_ARGS = _pkey("{436BF266-43E4-4B1C-92B2-88F52888B7B1}", 2)


def open_store(path, readwrite):
    require_com()
    store = POINTER(IPropertyStore)()
    hr = shell32.SHGetPropertyStoreFromParsingName(
        c_wchar_p(os.path.abspath(path)), None, 2 if readwrite else 0,
        byref(IPropertyStore._iid_), byref(store))
    if hr < 0:
        raise OSError("SHGetPropertyStoreFromParsingName hr=%08X" % (hr & 0xFFFFFFFF))
    return store


def get_str(store, key):
    pv = store.GetValue(byref(key))
    if pv.vt == 31 and pv.ulVal:          # VT_LPWSTR
        return ctypes.wstring_at(pv.ulVal)
    return None


def get_u4(store, key):
    pv = store.GetValue(byref(key))
    if pv.vt == 19:                        # VT_UI4
        return (pv.ulVal or 0) & 0xFFFFFFFF
    return None


# ---------- pure-python .lnk StringData reader ----------
def lnk_strings(path):
    r"""Return {name, relative_path, working_dir, arguments, icon_location}.

    Needed because the property store does not expose System.Link.Arguments for
    every shortcut, and the hash covers TargetParsingPath + Arguments. Parsed
    per [MS-SHLLINK]; unknown/odd files raise, callers should tolerate that.
    """
    import struct
    data = open(path, "rb").read()
    if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        raise ValueError("not a .lnk")
    flags = struct.unpack_from("<I", data, 0x14)[0]
    unicode_str = bool(flags & 0x80)
    off = 0x4C
    if flags & 0x1:                                     # HasLinkTargetIDList
        off += 2 + struct.unpack_from("<H", data, off)[0]
    if flags & 0x2:                                     # HasLinkInfo
        off += struct.unpack_from("<I", data, off)[0]

    out = {}
    for bit, key in ((0x4, "name"), (0x8, "relative_path"), (0x10, "working_dir"),
                     (0x20, "arguments"), (0x40, "icon_location")):
        if not (flags & bit):
            out[key] = None
            continue
        count = struct.unpack_from("<H", data, off)[0]
        off += 2
        if unicode_str:
            out[key] = data[off:off + count * 2].decode("utf-16-le", "replace")
            off += count * 2
        else:
            out[key] = data[off:off + count].decode("mbcs", "replace")
            off += count
    return out


def lnk_arguments(path):
    try:
        return lnk_strings(path).get("arguments") or ""
    except Exception:
        return ""


# ---------- hashing ----------
def generalize(path):
    for env, suffix, guid in GENERALIZE:
        base = os.environ.get(env)
        if not base:
            continue
        folder = base + suffix
        if path.lower().startswith(folder.lower()) and (len(path) == len(folder) or path[len(folder)] == "\\"):
            return guid + path[len(folder):]
    return path


def compute_hash(target, args, salt=SALT):
    blob = (generalize(target) + (args or "") + salt).lower()
    data = blob.encode("utf-16-le")
    out = (ctypes.c_ubyte * 4)()
    hr = shlwapi.HashData(data, len(data), out, 4)
    if hr < 0:
        raise OSError("HashData hr=%08X" % (hr & 0xFFFFFFFF))
    return int.from_bytes(bytes(out), "little")


# ---------- commands ----------
def cmd_salt(dll=None):
    """Extract candidate salt strings from the local twinui.dll (ground truth)."""
    dll = dll or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "twinui.dll")
    print("scanning: %s" % dll)
    data = open(dll, "rb").read()
    needle = "prehash".encode("utf-16-le")
    found, at = [], data.find(needle)
    while at != -1:
        # expand to the surrounding NUL-terminated UTF-16 string
        start = at
        while start >= 2 and data[start - 2:start] != b"\x00\x00":
            start -= 2
        end = at
        while end + 2 <= len(data) and data[end:end + 2] != b"\x00\x00":
            end += 2
        try:
            s = data[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            s = None
        if s and s not in found:
            found.append(s)
        at = data.find(needle, at + 2)

    if not found:
        print("no candidate found - this build may store the salt differently")
        return 1
    for s in found:
        mark = "  <- matches the SALT constant in this script" if s == SALT else ""
        print("candidate: %r%s" % (s, mark))
    return 0


def cmd_verify(winx_dir=None):
    """Oracle test: Microsoft's own WinX shortcuts must validate with our salt."""
    winx_dir = winx_dir or os.path.join(os.environ["LOCALAPPDATA"], r"Microsoft\Windows\WinX")
    files = sorted(glob.glob(os.path.join(winx_dir, "Group*", "*.lnk")))
    if not files:
        print("no shortcuts under %s" % winx_dir)
        return 1
    ok = bad = skipped = 0
    for f in files:
        store = open_store(f, False)
        target = get_str(store, PKEY_TARGET)
        args = get_str(store, PKEY_ARGS) or lnk_arguments(f)
        stored = get_u4(store, PKEY_WINX_HASH)
        name = os.path.basename(f)

        # Entries whose target is not a real file (ms-settings: URIs, shell
        # folders, .msc handled through a shell verb ...) are hashed by TWINUI
        # over a parsing path we cannot reconstruct - they are not oracles.
        if target is None or stored is None:
            print("SKIP  %-44s (no target or no stored hash)" % name)
            skipped += 1
            continue
        if not os.path.exists(os.path.expandvars(target)):
            print("SKIP  %-44s (non-filesystem target: %s)" % (name, target))
            skipped += 1
            continue

        calc = compute_hash(target, args)
        if calc == stored:
            ok += 1
            state = "OK  "
        else:
            bad += 1
            state = "FAIL"
        print("%s  %-44s stored=0x%08X computed=0x%08X  %s" % (state, name, stored, calc, target))

    print("\n%d ok, %d mismatched, %d skipped" % (ok, bad, skipped))
    if bad:
        print("Mismatches on real file targets mean the salt/algorithm is wrong for THIS build.")
        print("Run `python winxhash.py salt` and fix SALT before writing anything.")
        return 1
    if ok == 0:
        print("No usable oracle found - do not trust the hash writer on this machine yet.")
        return 1
    print("Salt and algorithm validated against %d Microsoft-shipped shortcut(s)." % ok)
    return 0


def cmd_read(path):
    print("stored: 0x%08X" % (get_u4(open_store(path, False), PKEY_WINX_HASH) or 0))


def cmd_target(path):
    store = open_store(path, False)
    print("TargetParsingPath: [%s]" % get_str(store, PKEY_TARGET))
    print("Arguments        : [%s]" % (get_str(store, PKEY_ARGS) or ""))


def cmd_hash(target, args=""):
    print("computed: 0x%08X" % compute_hash(target, args))


def _write_value(path, value):
    store = open_store(path, True)
    pv = PROPVARIANT()
    pv.vt = 19  # VT_UI4
    pv.ulVal = value
    store.SetValue(byref(PKEY_WINX_HASH), byref(pv))
    store.Commit()


def cmd_write(path, target, args=""):
    h = compute_hash(target, args)
    _write_value(path, h)
    print("OK: wrote hash 0x%08X to %s" % (h, path))


def cmd_writeval(path, hexval):
    h = int(hexval, 16)
    _write_value(path, h)
    print("OK: wrote raw hash 0x%08X" % h)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode = argv[1]
    try:
        if mode == "salt":
            return cmd_salt(argv[2] if len(argv) > 2 else None)
        if mode == "verify":
            return cmd_verify(argv[2] if len(argv) > 2 else None)
        if mode == "read":
            cmd_read(argv[2])
        elif mode == "target":
            cmd_target(argv[2])
        elif mode == "hash":
            cmd_hash(argv[2], argv[3] if len(argv) > 3 else "")
        elif mode == "write":
            cmd_write(argv[2], argv[3], argv[4] if len(argv) > 4 else "")
        elif mode == "writeval":
            cmd_writeval(argv[2], argv[3])
        else:
            print(__doc__)
            return 2
    except IndexError:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
