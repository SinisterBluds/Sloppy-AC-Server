#!/usr/bin/env python3
"""Verify a PandaSpigot 1.8.8 world region chunk is superflat (2;7,2x3,2;1;)."""
import glob, struct, sys, zlib

BLOCKS_MARKER = bytes.fromhex('070006426c6f636b73') + struct.pack('>i', 4096)  # TAG_Byte_Array 'Blocks' len 4096

def read_mca(path, gx, gz):
    """Return decompressed chunk NBT bytes for global chunk (gx, gz), or None."""
    with open(path, 'rb') as f:
        f.seek((gx & 31) * 4 + (gz & 31) * 128)
        val = struct.unpack('>I', f.read(4))[0]
        off, size = val >> 8, val & 0xFF
    if off == 0 or size == 0:
        return None
    with open(path, 'rb') as f:
        f.seek(off * 4096)
        data = f.read(size * 4096)
    return zlib.decompress(data[5:])

def check(path, gx, gz):
    raw = read_mca(path, gx, gz)
    if raw is None:
        return True  # not present, skip
    i = raw.find(BLOCKS_MARKER)
    assert i != -1, f"chunk ({gx},{gz}): no Blocks array"
    b = raw[i + len(BLOCKS_MARKER):i + len(BLOCKS_MARKER) + 4096]
    assert len(b) == 4096
    layers = [(7, b[0:256]), (3, b[256:512]), (3, b[512:768]), (2, b[768:1024]), (0, b[1024:1280])]
    ok = all(set(v) <= {k} for k, v in layers)
    print(f"chunk ({gx},{gz}): " + ("FLAT OK" if ok else "NOT FLAT") +
          "  y0={} y1={} y2={} y3={} y4={}".format(*(sorted(set(v))[:3] for _, v in layers)))
    return ok

if __name__ == '__main__':
    world = sys.argv[1]
    ok = True
    checked = 0
    for p in sorted(glob.glob(f"{world}/region/r.*.mca")):
        rx, rz = (int(v) for v in p.rsplit('/', 1)[-1][2:-4].split('.'))
        with open(p, 'rb') as f:
            hdr = f.read(8192)
        for i in range(1024):
            val = struct.unpack('>I', hdr[i*4:i*4+4])[0]
            if val >> 8 and val & 0xFF:
                checked += 1
                ok &= check(p, rx * 32 + i % 32, rz * 32 + i // 32)
    print(f"{checked} chunks checked")
    sys.exit(0 if ok else 1)
