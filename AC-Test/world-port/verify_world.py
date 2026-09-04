#!/usr/bin/env python3
"""Verify extract_world output: read back written region files, compare EVERY block
against the recording-derived data (state id -> mapped 1.8.8 id:data, shifted +64)."""
import json, struct, sys, zipfile, zlib
from collections import Counter
from analyze_recording import read_varint, Buf, parse_chunk
import extract_world as ew

def read_region_chunk(path, x, z):
    with open(path, 'rb') as f:
        f.seek((x & 31) * 4 + (z & 31) * 128)
        val = struct.unpack('>I', f.read(4))[0]
        off, size = val >> 8, val & 0xFF
        if off == 0 or size == 0:
            return None
        f.seek(off * 4096)
        data = f.read(size * 4096)
    ln = struct.unpack('>I', data[:4])[0]
    return zlib.decompress(data[5:5 + ln - 1])

class R:
    def __init__(self, d): self.d, self.i = d, 0
    def u8(self): v = self.d[self.i]; self.i += 1; return v
    def u16(self): v = struct.unpack('>H', self.d[self.i:self.i+2])[0]; self.i += 2; return v
    def i32(self): v = struct.unpack('>i', self.d[self.i:self.i+4])[0]; self.i += 4; return v
    def skip(self, n): self.i += n
    def _parse(self, t):
        if t == 1: return self.u8()
        if t == 2: v = struct.unpack('>h', self.d[self.i:self.i+2])[0]; self.i += 2; return v
        if t == 3: return self.i32()
        if t == 4: v = struct.unpack('>q', self.d[self.i:self.i+8])[0]; self.i += 8; return v
        if t == 7:
            n = self.i32(); v = self.d[self.i:self.i+n]; self.i += n; return v
        if t == 8:
            n = self.u16(); v = self.d[self.i:self.i+n].decode(); self.i += n; return v
        if t == 9:
            et, n = self.u8(), self.i32()
            return [self._parse(et) for _ in range(n)]
        if t == 10:
            out = {}
            while True:
                t2 = self.u8()
                if t2 == 0: break
                nl = self.u16(); name = self.d[self.i:self.i+nl].decode(); self.i += nl
                out[name] = self._parse(t2)
            return out
        if t == 11:
            n = self.i32(); v = list(struct.unpack('>%di' % n, self.d[self.i:self.i+4*n])); self.i += 4*n; return v
        raise ValueError(f'tag {t}')
    def root(self):
        t = self.u8()
        nl = self.u16(); self.skip(nl)
        return self._parse(t)

def main():
    mcpr = sys.argv[1]
    world_dir = sys.argv[2]
    state_map = ew.load_states()

    # pass A: re-parse recording
    zf = zipfile.ZipFile(mcpr)
    f = zf.open('recording.tmcpr')
    st = 'LOGIN'
    chunks = {}
    while True:
        hdr = f.read(8)
        if len(hdr) < 8: break
        ts, ln = struct.unpack('>ii', hdr)
        if ln < 1 or ln > 50_000_000: break
        body = f.read(ln)
        if len(body) < ln: break
        pid, i = read_varint(body, 0)
        if st == 'LOGIN':
            if pid == 0x02: st = 'CONFIG'
        elif st == 'CONFIG':
            if pid == 0x03: st = 'PLAY'
        else:
            if pid == 0x74: st = 'CONFIG'; continue
            if pid == 0x2c:
                c = parse_chunk(body[i:])
                if c is not None:
                    chunks[(c['x'], c['z'])] = c
    f.close()

    # pass B: read back written regions
    import glob, os
    written = {}
    for p in glob.glob(os.path.join(world_dir, 'region', '*.mca')):
        rx, rz = (int(v) for v in os.path.basename(p)[2:-4].split('.'))
        for lx in range(32):
            for lz in range(32):
                raw = read_region_chunk(p, lx, lz)
                if raw is None: continue
                r = R(raw)
                root = r.root()
                level = root['Level']
                written[(level['xPos'], level['zPos'])] = level

    mism = Counter()
    checked = 0
    compared = 0
    for (x, z), c in chunks.items():
        lvl = written.get((x, z))
        if lvl is None:
            mism['missing_chunk'] += 1
            continue
        checked += 1
        secs = {s['Y']: s for s in lvl['Sections']}
        # compare per section
        for i, s in enumerate(c['sections']):
            dst_y = (-64 + 16 * i) + ew.SHIFT
            if dst_y < 0 or dst_y >= ew.MAX_Y:
                continue
            sec = secs.get(dst_y // 16)
            if sec is None:
                # expected empty if no blocks
                if any(sid != 0 for sid in s['blocks']):
                    mism['missing_section'] += 1
                continue
            blocks = sec['Blocks']; data = sec['Data']
            for bi, sid in enumerate(s['blocks']):
                bid, d = ew.convert_block(sid, state_map)
                wb = blocks[bi]
                wd = (data[bi >> 1] >> (4 if bi % 2 == 0 else 0)) & 0xF
                compared += 1
                if (wb, wd) != (bid, d):
                    mism[(f'{wb}:{wd} vs {bid}:{d}')] += 1
        # extra written sections that shouldn't exist
        for y, sec in secs.items():
            src_y = y * 16 - ew.SHIFT
            idx = (src_y + 64) // 16
            if idx < 0 or idx >= len(c['sections']):
                mism['extra_section'] += 1
    print(f"chunks in recording: {len(chunks)}, written: {len(written)}")
    print(f"chunks compared: {checked}, blocks compared: {compared}")
    if mism:
        print("MISMATCHES:")
        for k, v in mism.most_common(20):
            print(f"  {v:6d}x  {k}")
    else:
        print("VERIFICATION PASSED: all blocks match")
    # also verify level.dat parses
    import gzip
    try:
        with open(os.path.join(world_dir, 'level.dat'), 'rb') as f:
            lr = R(gzip.decompress(f.read()))
        root = lr.root()
        data = root['Data']
        print(f"level.dat OK: spawn=({data['SpawnX']},{data['SpawnY']},{data['SpawnZ']}) generator={data['generatorName']}")
    except Exception as e:
        print("level.dat FAILED:", e)

if __name__ == '__main__':
    main()
