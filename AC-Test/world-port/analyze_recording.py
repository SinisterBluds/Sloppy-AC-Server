#!/usr/bin/env python3
"""Analyze ReplayMod .mcpr recordings (1.21.10): extract chunk data, report world stats.

Wire formats (verified against decompiled 1.21.10 client/server):
  tmcpr segments: [i32 time][i32 len][vanilla packet: varint id + data]
  states: LOGIN (login success=0x02 -> CONFIG), CONFIG (finish=0x03 -> PLAY), PLAY (start_config=0x74 -> CONFIG)
  config registry_data (0x07): [varint len][str key][varint count][per: varint len][str name][u8 hasNbt][NBT]
  play chunk packet (0x2c): [i32 x][i32 z][heightmaps][varint len + sections][blockEntities][light]
    heightmaps: varint count, per: [varint type][varint count][count*8 bytes]
    sections: per: [short blockCount][PalettedContainer blocks][PalettedContainer biomes]
    PalettedContainer: [u8 bits][strategy]
      bits=0: [varint entry]
      bits 1-8: [varint palSize][palSize*varint entry][varint longCount][longs]
      bits 9+: [varint longCount][longs]  (entries are raw ids)
"""
import json, os, struct, sys, zipfile
from collections import Counter

CHUNK_PKT, REGISTRY_PKT = 0x2C, 0x07

HERE = os.path.dirname(os.path.abspath(__file__))

def load_state_map():
    """state id -> block name, from minecraft-data blocks.json (classic id scheme)."""
    p = os.path.join(HERE, 'blocks-1.21.9.json')
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    m = {}
    for b in d:
        name = b['name'] if ':' in b['name'] else 'minecraft:' + b['name']
        for sid in range(b['minStateId'], b['maxStateId'] + 1):
            m[sid] = name
    return m

def read_varint(d, i):
    v = 0
    for shift in range(0, 35, 7):
        b = d[i]; i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
    raise ValueError("varint too long")

class NBT:
    def __init__(self, data, start=0):
        self.d, self.i = data, start
    def _u8(self): v = self.d[self.i]; self.i += 1; return v
    def _u16(self): v = struct.unpack('>H', self.d[self.i:self.i+2])[0]; self.i += 2; return v
    def _i32(self): v = struct.unpack('>i', self.d[self.i:self.i+4])[0]; self.i += 4; return v
    def _i64(self): v = struct.unpack('>q', self.d[self.i:self.i+8])[0]; self.i += 8; return v
    def _str(self):
        n = self._u16(); s = self.d[self.i:self.i+n].decode('utf-8', 'replace'); self.i += n; return s
    def read_tag(self, t, named=False):
        name = self._str() if named else None
        if t == 1: v = self._u8(); v = v - 256 if v > 127 else v
        elif t == 2: v = struct.unpack('>h', self.d[self.i:self.i+2])[0]; self.i += 2
        elif t == 3: v = self._i32()
        elif t == 4: v = self._i64()
        elif t == 5: v = struct.unpack('>f', self.d[self.i:self.i+4])[0]; self.i += 4
        elif t == 6: v = struct.unpack('>d', self.d[self.i:self.i+8])[0]; self.i += 8
        elif t == 7:
            n = self._i32(); v = self.d[self.i:self.i+n]; self.i += n
        elif t == 8: v = self._str()
        elif t == 9:
            et, n = self._u8(), self._i32()
            v = [self.read_tag(et) for _ in range(n)]
        elif t == 10:
            v = {}
            while True:
                t2 = self._u8()
                if t2 == 0: break
                k, val = self.read_tag(t2, named=True)
                v[k] = val
        elif t == 11:
            n = self._i32(); v = list(struct.unpack('>%di' % n, self.d[self.i:self.i+4*n])); self.i += 4*n
        elif t == 12:
            n = self._i32(); v = list(struct.unpack('>%dq' % n, self.d[self.i:self.i+8*n])); self.i += 8*n
        else:
            raise ValueError(f"unknown tag {t}")
        return (name, v) if named else v
    def read_root(self):
        t = self._u8()
        assert t == 10, f"root is tag {t}"
        return self.read_tag(10, named=True)[1]

class Buf:
    def __init__(self, data, i=0):
        self.d, self.i = data, i
    def u8(self): v = self.d[self.i]; self.i += 1; return v
    def i16(self): v = struct.unpack('>h', self.d[self.i:self.i+2])[0]; self.i += 2; return v
    def i32(self): v = struct.unpack('>i', self.d[self.i:self.i+4])[0]; self.i += 4; return v
    def varint(self): v, self.i = read_varint(self.d, self.i); return v
    def skip(self, n): self.i += n
    def string(self):
        ln = self.varint()
        s = self.d[self.i:self.i+ln].decode('utf-8', 'replace'); self.i += ln
        return s
    def nbt(self):
        """Read NBT in wire format (1.21.5+ registry values): [tag byte][fields], no root name, no length."""
        t = self.u8()
        if t == 0:
            return None
        n = NBT(self.d, self.i)
        v = n.read_tag(t)
        self.i = n.i
        return v
    def remain(self): return len(self.d) - self.i

def parse_paletted(b, is_biome=False):
    """PalettedContainer -> list of entry ids (4096 or 64 values). 1.21.10 wire format."""
    bits = b.u8()
    size = 64 if is_biome else 4096
    if bits == 0:
        entry = b.varint()
        return [entry] * size
    has_palette = bits <= (3 if is_biome else 8)
    pal = []
    if has_palette:
        n = b.varint()
        pal = [b.varint() for _ in range(n)]
    per_long = 64 // bits
    ln = (size + per_long - 1) // per_long   # derived, not on the wire
    longs = struct.unpack('>%dq' % ln, b.d[b.i:b.i+8*ln]); b.skip(8*ln)
    mask = (1 << bits) - 1
    vals = []
    for i in range(size):
        vals.append((longs[i // per_long] >> ((i % per_long) * bits)) & mask)
    if not has_palette:
        return vals  # raw ids
    return [pal[v] if v < len(pal) else -1 for v in vals]

def parse_chunk(payload):
    """Return dict(x, z, sections) or None."""
    try:
        b = Buf(payload)
        x, z = b.i32(), b.i32()
        for _ in range(b.varint()):          # heightmaps
            b.varint()                        # type
            b.skip(8 * b.varint())            # longs
        ln = b.varint()
        sec_buf = Buf(b.d[b.i:b.i+ln]); b.skip(ln)
        sections = []
        while sec_buf.remain() > 0:
            block_count = sec_buf.i16()
            block_ids = parse_paletted(sec_buf, False)
            biome_ids = parse_paletted(sec_buf, True)
            sections.append({'block_count': block_count, 'blocks': block_ids, 'biomes': biome_ids})
        return {'x': x, 'z': z, 'sections': sections}
    except Exception:
        return None

def chunk_sections_to_blocks(c):
    """Expand sections into a (minY, [4096 state ids per section]) list."""
    out = []
    for i, s in enumerate(c['sections']):
        y = -64 + 16 * i
        out.append((y, s['blocks']))
    return out

def analyze(path):
    zf = zipfile.ZipFile(path)
    meta = json.loads(zf.read('metaData.json'))
    f = zf.open('recording.tmcpr')
    state = 'LOGIN'
    stats = {'play': 0, 'chunks': 0, 'registry_data': 0, 'errors': 0}
    registries = {}   # key -> {name: (id_or_none, value_nbt)}
    chunks = {}
    min_x = min_z = 10**9; max_x = max_z = -10**9
    while True:
        hdr = f.read(8)
        if len(hdr) < 8: break
        ts, ln = struct.unpack('>ii', hdr)
        if ln < 1 or ln > 50_000_000:
            print(f"  bad length {ln}"); break
        body = f.read(ln)
        if len(body) < ln: break
        pid, i = read_varint(body, 0)
        if state == 'LOGIN':
            if pid == 0x02: state = 'CONFIG'
        elif state == 'CONFIG':
            if pid == 0x03: state = 'PLAY'
            elif pid == REGISTRY_PKT:
                stats['registry_data'] += 1
                try:
                    b = Buf(body[i:])
                    key = b.string()
                    n = b.varint()
                    entries = []
                    for _ in range(n):
                        name = b.string()
                        has = b.u8()
                        val = b.nbt() if has else None
                        entries.append((name, val))
                    registries[key] = entries
                except Exception:
                    stats['errors'] += 1
        else:
            if pid == 0x74: state = 'CONFIG'; continue
            stats['play'] += 1
            if pid == CHUNK_PKT:
                stats['chunks'] += 1
                c = parse_chunk(body[i:])
                if c is None:
                    stats['errors'] += 1
                    continue
                chunks[(c['x'], c['z'])] = c
                min_x = min(min_x, c['x']); max_x = max(max_x, c['x'])
                min_z = min(min_z, c['z']); max_z = max(max_z, c['z'])
    f.close()
    return meta, stats, registries, chunks, (min_x, max_x, min_z, max_z)

def main():
    path = sys.argv[1]
    state_map = load_state_map()
    if state_map is None:
        print("missing blocks-1.21.9.json next to this script"); sys.exit(1)
    print(f"=== {path}")
    meta, stats, registries, chunks, (min_x, max_x, min_z, max_z) = analyze(path)
    print(f"server: {meta.get('serverName')}  duration: {meta.get('duration')/1000:.0f}s  players: {len(meta.get('players', []))}")
    print(f"play packets: {stats['play']}, chunk packets: {stats['chunks']}, registry_data: {stats['registry_data']}, errors: {stats['errors']}")
    print(f"unique chunks: {len(chunks)}, x: {min_x}..{max_x}, z: {min_z}..{max_z}")
    print("registries:", list(registries.keys()))

    # block statistics
    block_chunks = Counter()      # name -> chunks containing it
    ground = {}                   # (x,z) -> lowest non-empty section names
    section_usage = Counter()     # y -> chunks with content
    heightmaps = []
    for (x, z), c in chunks.items():
        for y, ids in chunk_sections_to_blocks(c):
            names = {state_map.get(i, f'?{i}') for i in ids}
            if names and names != {'minecraft:air'}:
                section_usage[y] += 1
                if (x, z) not in ground:
                    ground[(x, z)] = sorted(names)
                for n in names:
                    block_chunks[n] += 1
        # top solid block per column-ish: max block id y at x=8,z=8
        col = []
        for y, ids in chunk_sections_to_blocks(c):
            sid = ids[8 + 8 * 16]  # x=8, y local, z=8 -> index y*256 + z*16 + x
            col.append((y, sid))
        top = [s for y, s in col if s != 0]
        if top:
            top_y = max(y for y, s in col if s != 0) + 1
            heightmaps.append((x, z, top_y))
    print("sections with content by y:", dict(sorted(section_usage.items())))
    cg = Counter(tuple(v) for v in ground.values())
    print("ground palettes (chunks):")
    for pal, n in cg.most_common(8):
        print(f"  {n:5d}x  {pal}")
    print("block presence (chunks containing, top 15):")
    for name, n in block_chunks.most_common(15):
        print(f"  {n:5d}x  {name}")
    if heightmaps:
        ys = sorted(set(h[2] for h in heightmaps))
        print(f"top solid y range across chunks: {ys[0]}..{ys[-1]}")
    return chunks, ground

if __name__ == '__main__':
    main()
