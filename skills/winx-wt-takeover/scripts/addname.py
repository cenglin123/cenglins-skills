r"""addname.py - insert a NAME_STRING into a .lnk that has none.

WinX entries display the shortcut's NAME_STRING. A .lnk produced by some tools
has HasName clear, so the menu shows nothing useful. This inserts the string at
the correct position per [MS-SHLLINK] StringData ordering and sets the flag.

Usage
-----
    python addname.py <src.lnk> <dst.lnk> "Windows PowerShell"

Caveats
-------
  * Offsets are computed from LinkFlags: LinkTargetIDList (0x1) and LinkInfo
    (0x2) are skipped if present. Files using other optional structures before
    StringData are not handled - always verify the result.
  * Verify with:  python winxhash.py target <dst.lnk>  and by opening the menu.
"""

import struct
import sys


def add_name(src, dst, name):
    data = bytearray(open(src, "rb").read())
    if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        raise SystemExit("not a .lnk (bad HeaderSize)")

    link_flags = struct.unpack_from("<I", data, 0x14)[0]
    if link_flags & 0x4:
        raise SystemExit("HasName is already set - nothing to do")

    off = 0x4C
    if link_flags & 0x1:                              # LinkTargetIDList
        idl_size = struct.unpack_from("<H", data, off)[0]
        off += 2 + idl_size
    if link_flags & 0x2:                              # LinkInfo
        li_size = struct.unpack_from("<I", data, off)[0]
        off += li_size

    # StringData starts here; NAME_STRING comes first in MS-SHLLINK ordering
    insert = struct.pack("<H", len(name)) + name.encode("utf-16-le")
    new = data[:off] + insert + data[off:]
    struct.pack_into("<I", new, 0x14, link_flags | 0x4)   # set HasName
    open(dst, "wb").write(new)
    print("%s: inserted [%s] at 0x%x, size %d -> %d" % (dst, name, off, len(data), len(new)))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    add_name(sys.argv[1], sys.argv[2], sys.argv[3])
