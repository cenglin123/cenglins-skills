r"""patchlnk.py - retarget a .lnk by in-place binary patching (structure preserving).

Why not just rewrite the shortcut with WScript.Shell? Because a rewrite drops
the property store (AppUserModelID and friends) that the shell attached to the
original file. Patching the embedded path strings in place - NUL-padded so no
offset moves - keeps the file byte-compatible with itself.

Usage
-----
    python patchlnk.py <src.lnk> <dst.lnk> <new-target> [old-target ...]

If no old-target is given, the usual PowerShell paths are tried:
    %windir%\system32\WindowsPowerShell\v1.0\powershell.exe
    %SystemRoot%\system32\WindowsPowerShell\v1.0\powershell.exe
    C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe   (expanded)

Constraint: the new path must not be LONGER than the old one (it is padded with
NULs into the original slot). Use the 8.3 short form if it does not fit:
    python -c "import ctypes,sys;b=ctypes.create_unicode_buffer(512);ctypes.windll.kernel32.GetShortPathNameW(sys.argv[1],b,512);print(b.value)" "C:\long\path\powershell.exe"

ALWAYS read the result back before trusting it:
    powershell -c "(New-Object -ComObject WScript.Shell).CreateShortcut('dst.lnk').TargetPath"

If the shell still resolves the old target, the path also lives in the link's
ID list and a patch is not enough - fall back to a rebuild (install.ps1 does
this automatically).
"""

import os
import sys

DEFAULT_OLD = [
    r"%windir%\system32\WindowsPowerShell\v1.0\powershell.exe",
    r"%SystemRoot%\system32\WindowsPowerShell\v1.0\powershell.exe",
    os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                 r"System32\WindowsPowerShell\v1.0\powershell.exe"),
]


def replace_padded(data: bytes, old: str, new: str, enc: str):
    old_b = old.encode(enc, errors="ignore")
    new_b = new.encode(enc, errors="ignore")
    if len(new_b) > len(old_b):
        return data, 0, "too long for this slot"
    padded = new_b + b"\x00" * (len(old_b) - len(new_b))
    count = data.count(old_b)
    return data.replace(old_b, padded), count, None


def patch(src_path, dst_path, new_path, old_paths):
    with open(src_path, "rb") as f:
        data = f.read()
    total = 0
    for enc in ("utf-16-le", "mbcs"):
        for old in old_paths:
            data, count, err = replace_padded(data, old, new_path, enc)
            if err:
                print("  %-9s skip %r (%s)" % (enc, old, err))
                continue
            if count:
                print("  %-9s %d x %r" % (enc, count, old))
                total += count
    if total == 0:
        print("NO MATCH - nothing patched; the target string is not stored as plain text here")
        return 1
    with open(dst_path, "wb") as f:
        f.write(data)
    print("patched %s (%d replacement(s)) -> %s" % (dst_path, total, new_path))
    print("now VERIFY the target by reading it back (see module docstring)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    src, dst, new = sys.argv[1], sys.argv[2], sys.argv[3]
    olds = sys.argv[4:] or DEFAULT_OLD
    sys.exit(patch(src, dst, new, olds))
