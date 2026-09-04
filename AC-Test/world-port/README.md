# AC Test Server — World Port Pipeline (complete documentation)

Ports a superflat anticheat-test world **out of a ReplayMod .mcpr recording** (Minecraft 1.21.10)
and **down into a 1.8.8 PandaSpigot anvil world**, verified block-for-block.

Source recording: `minecraft.vagdedes.com` (the UniversalAntiCheat author's server) —
`2026_08_17_21_06_57.mcpr` in
`ACServer/References/Recordings of existing anticheat servers as reference/replay_recordings/`.

Result: `ACServer/AC-Test/world/` — 421 recorded chunks (24×19 chunk area, x 24..47 / z -38..-20)
with a perfect superflat ground (bedrock@y0, dirt@y1-2, grass@y3 after the +64 shift) plus the
server's anticheat test arena (barriers, terracotta, water, soul sand, slime, honey, redstone
machines, end portals, heads, command blocks). The 1.8.8 server's flat generator fills any gaps
the recording didn't cover, so the world is seamless.

---

## 1. Files in this directory

| File | Purpose |
|---|---|
| `analyze_recording.py` | Parse a .mcpr, decode all chunk packets, report world stats (block palettes, ground composition, structure layout). Used to pick the best recording. |
| `extract_world.py` | Full pipeline: .mcpr → 1.8.8 anvil regions + `level.dat`. |
| `verify_world.py` | Re-reads the written region files and compares **every block** against the recording-derived data. |
| `check_flat.py` | Independent flat-world checker (scans region files for bedrock/dirt/grass layer pattern). |
| `chunker-1.21.10-blocks.json` | Chunker's 1.21.10 block registry: state id → block name + property values. THE authoritative state→properties mapping. |
| `blocks-1.21.9.json` | minecraft-data state-id scheme cross-check (classic state ids). |
| `mc-1.21.9-protocol.json` | minecraft-data packet registry (used during reverse engineering; note: its chunk packet spec is WRONG for 1.21.10 — see §5). |

## 2. Server setup (ACServer/AC-Test)

- `server.jar` = `latest-pandaspigot-220.jar` (PandaSpigot 1.8.8 paperclip — downloads vanilla
  1.8.8 from Mojang, patches it on first run, caches it; offline-safe afterwards).
- `run.sh` launches with Java 8 (`~/.sdkman/candidates/java/8.0.492.fx-zulu`), `-Xmx2G`.
- `server.properties`: `level-type=FLAT`, `generator-settings=2;7,2x3,2;1;`, no structures/mobs,
  `allow-flight=true`, offline mode, port 25565.
- `bukkit.yml` copied from the bedwars reference (spawn limits 0, ticks-per -1 — NOTE: `-1`
  autosave means chunks stay in memory until shutdown; this confused earlier verification, see §8).
- Stop the server with `kill -TERM <pid>` — the shutdown hook saves everything (stdin is
  /dev/null under the harness, so the console can't be reached).

## 3. Choosing the recording (analysis)

Three recordings existed. `analyze_recording.py` decoded all chunk packets in each and reported
the ground block palettes per chunk:

| Recording | Server | Chunks | Ground |
|---|---|---|---|
| 20_58_36 | eu.loyisa.cn | 823 | dirt-flat + big PvP arenas |
| 21_06_57 | **minecraft.vagdedes.com** | 421 | **grass superflat + AC test arena** ← winner |
| 21_08_47 | tree.ac | 878 | floating built arena, NOT flat |

vagdedes.com won: every one of its 421 chunks has ground = `air/bedrock/dirt/grass_block`,
340 of them are completely untouched superflat, the rest carry the anticheat practice facility.

## 4. The .mcpr recording format (reverse-engineered)

The ReplayStudio git submodule in ReplayMod-sourcecode was empty, so the format was determined
empirically from file bytes + the ReplayMod/ReplayStudio source on GitHub:

```
zip container (.mcpr):
  metaData.json        — server name, mcversion 1.21.10, protocol 773, duration, players
  recording.tmcpr      — packet stream (below)
  recording.tmcpr.crc32

recording.tmcpr — sequence of segments, NO header:
  [i32 timestamp ms][i32 payload length][vanilla packet bytes]
  vanilla packet = [varint packet id][packet data]  (as on the wire, protocol 773)

Packet ids are STATE-DEPENDENT (ReplayInputStream switches registry):
  LOGIN  state: 0x02 = login success   → enter CONFIGURATION
  CONFIG state: 0x03 = finish config   → enter PLAY
               0x07 = registry data    (parsed for dimension info / verification)
  PLAY   state: 0x74 = start config    → back to CONFIGURATION
                0x2c = level chunk with light (the one we care about)
```

## 5. The 1.21.10 chunk packet (reverse-engineered)

**minecraft-data's protocol spec is wrong here** — it models the pre-1.21.5 format. The truth
came from decompiling the actual 1.21.10 client and server jars (Vineflower — see §9):

```
level_chunk_with_light (0x2c):
  i32 x, i32 z
  heightmaps: varint count, per entry: [varint type][varint longCount][longCount × i64]
              (9 bits/entry, 7 entries per long, longCount = ceil(256 / (64//9)) = 37)
  section buffer: varint byteLength + raw bytes
  block entities, light data  (unused by us)
```

Section buffer (per 16³ section, bottom-up, all 24 present even when empty):
```
  i16 blockCount (non-air block count)
  PalettedContainer blockStates
  PalettedContainer biomes
```

PalettedContainer (1.21.10, **long array has NO length prefix — count derived from bits**):
```
  u8 bits
  bits == 0 : varint single entry id
  bits 1-8  : varint paletteSize + paletteSize × varint entry id
              + packed longs (count = ceil(size / (64//bits)), no prefix!)
  bits 9+   : packed longs of raw ids (no palette)
  unpacking: entry i at long[i // (64//bits)] >> ((i % (64//bits)) * bits) — never spans longs
  block size = 4096, biome size = 64; biomes use palette only for bits 1-3
```

Palette entry ids are **classic block state ids** (verified empirically: 0=air, 9=grass_block
snowy=true, 10=dirt, 85=bedrock — matches both minecraft-data's minStateId scheme and
Chunker's 1.21.10 blocks.json state ids). The server does NOT send the block registry in
registry_data (it's in the client's "known pack"), so the id→state mapping comes from
`chunker-1.21.10-blocks.json`.

Registry data values use raw NbtIo NBT with **no root name and no length prefix**
(`NbtIo.read` variant — `[tag][fields...][0x00]`), unlike the classic `[varint len][NBT]`.

## 6. The extraction pipeline (extract_world.py)

1. Stream the tmcpr, track packet state (LOGIN/CONFIG/PLAY), keep the LAST chunk packet per (x,z).
2. Decode sections → 4096 state ids per section (24 sections, y = -64 + 16·i).
3. Verify dimension from registry_data: `min_y=-64, height=384` (confirms the shift math).
4. Map every state id → (name, properties) via Chunker's blocks.json.
5. Map every (name, properties) → 1.8.8 `(id, data)` via the table in `extract_world.py`
   (`SIMPLE` dict + rule functions for stairs/slabs/doors/trapdoors/colors/logs/...).
6. Shift +64 (modern y -64 → 1.8.8 y 0; sections above 1.8.8's 256-height are dropped).
7. Write anvil: zlib-compressed chunk NBT in region files
   (`[i32 length][u8 compression=2][NbtIo root compound with name][0x00]`, `[i32 timestamp]`
   entries; **data sectors start at sector 4** — sectors 2-3 belong to the timestamp table).
8. Write `level.dat` (gzip NBT, DataVersion 1343, flat generator, spawn at arena centroid).

NBT notes learned the hard way:
- list elements carry **no tag byte** and **no name**
- compound values = `[tag][u16 namelen][name][fields][0x00]`

Block mapping policy: exact 1:1 for the ~250 blocks that exist in 1.8.8 (with correct data
values: wool/clay/glass colors, plank variants, stair orientations, slab halves, door/trapdoor
facing, log axes, flower types, skull types, bed facing, pressure plate power, etc.), sensible
substitutes for the rest (concrete→stained clay, honey→slime, blue_ice→packed_ice,
scaffolding→ladder, shulker→clay, light→glowstone, coral→dead bush, warped/crimson→nether
blocks, copper→orange clay, ...). Only 3 distinct block types in the whole world end up as
plain stone (sculk_sensor, observer, deepslate — they have no 1.8.8 equivalent at all).

## 7. Verification (all passes green)

| # | Pass | How | Result |
|---|---|---|---|
| 1 | Recording analysis | state ids vs Chunker registry | consistent |
| 2 | Extraction determinism | same code path, re-run | identical output |
| 3 | Block-for-block | `verify_world.py`: re-reads written regions, compares all 3,760,128 blocks against recording-derived mapping (id AND data nibble, incl. y-shift) | all match |
| 4 | Ground layers | `check_flat.py`-style column check on the written world: (7,0),(3,0),(3,0),(2,0),(0,0) at x=8,z=8 | 421/421 chunks intact |
| 5 | Server boot | PandaSpigot starts with the world, `Preparing start region` → `Done`, 0 errors, listens on 25565 | clean |
| 6 | Server save cycle | SIGTERM shutdown (server re-saves chunks), then re-run `verify_world.py` against the server-touched world | all 421 chunks still match |

## 8. Known limitations

- **+64 y-shift**: modern world sits at y -64..320; 1.8.8 only has y 0..255. Ground → y0-3,
  structures above modern y=191 are clipped. This includes the top of one 256-block-tall
  water tower (modern y 192..255, shifted 256..319) — its top 64 blocks are lost by the
  format, not by the simulation (verified: the remaining pillar survives server ticks intact).
- **Block entities dropped**: chest contents, sign text, command block commands, skull types
  (all skulls render as skeletons), banner patterns, spawner data are lost (blocks remain).
- `waterlogged` property ignored (1.8.8 has no waterlogging). Bubble columns become plain water.
- 1.9+ block states (stairs shapes inner/outer, door hinges, double slab variants beyond 1.8's)
  collapse to their 1.8.8 approximation.

## 8b. Data-value conventions (source of truth: decompiled vanilla 1.8.8 server)

Block meta values were verified against the decompiled 1.8.8 Mojang server jar
(`AC-Test/cache/mojang_1.8.8.jar`, decompiled with Vineflower; classes obfuscated but
decodable — full block registry is in `afh.S()`). Conventions:

| Block | 1.8.8 meta |
|---|---|
| standing sign (63) | rotation 0-15 |
| wall sign (68) | facing N=2, S=3, W=4, E=5 |
| button (77/143) | wall E=1, W=2, S=3, N=4; floor=5; ceiling=0; +8 powered |
| lever (69) | wall E=1, W=2, S=3, N=4; floor up_x=6/up_z=5; ceiling down_x=0/down_z=7; +8 powered |
| chest/trapped/ender (54/146/130) | facing N=2, S=3, W=4, E=5 |
| ladder (65) | facing N=2, S=3, W=4, E=5 |
| torch (50) | floor=5; wall E=1, W=2, S=3, N=4 |
| redstone torch | 76=lit, 75=unlit; same facing scheme |
| rails (66/27/28/157) | 0=NS,1=EW,2=ascE,3=ascW,4=ascN,5=ascS,6-9=corners; +8 powered |
| fence/bars/pane (85/101/102/160) | data 0 — connections are COMPUTED at runtime |
| vine (106) | N=1, E=2, S=4, W=8 |
| gate (107/183-187) | facing E=0,W=1,S=2,N=3; +4 open; +8 powered |
| trapdoor (96/167) | facing N=0,S=1,W=2,E=3; +4 open; +8 top |
| repeater (93/94) | facing E=0,W=1,S=2,N=3 (bits 0-1); delay-1 (bits 2-3) |
| comparator (149/150) | facing E=0,W=1,S=2,N=3; +4 subtract; +8 powered |
| redstone wire (55) | power 0-15 |
| cocoa (127) | facing E=0,W=1,S=2,N=3 (bits 0-1); age 0-2 (bits 2-3) |
| tripwire hook (131) | facing E=0,W=1,S=2,N=3; +4 attached; +8 powered |
| end portal frame (120) | facing E=0,W=1,S=2,N=3; +4 eye |
| furnace (61) | facing N=2, S=3, W=4, E=5 |
| dispenser/dropper (23/158) | full index down=0,up=1,N=2,S=3,W=4,E=5; +8 triggered |
| piston (33/29) | same full index; +8 extended |
| hopper (154) | down=0, N=2, S=3, W=4, E=5; +8 disabled |
| anvil (145) | facing E=0,W=1,S=2,N=3; +4 chipped; +8 damaged |
| command block (137) | meta = triggered bit only; facing is tile-entity data (dropped) |
| bed (26) | facing E=0,W=1,S=2,N=3; +8 head |
| door (64/71/193-197) | lower: facing N=0,E=2,S=1,W=3; +4 open; upper: 8 \| right-hinge(1) \| open(2) |
| skull (144) | floor=0 (type is tile-entity data); wall N=2, S=3, W=4, E=5 |
| double plant (175) | variant 0-5 (sunflower, lilac, tall grass, large fern, rose bush, peony); +8 upper |
| water | source 9:0; flowing level 1-7 = depth (8:1-7); falling (modern level 8+) = 8:0 |

**Pitfalls that caused the v1 bugs** (all fixed): property-aware rules were *shadowed* by
generic entries in the `SIMPLE` dict (buttons, levers, vines, chests, ladders, torches, rails,
fences gates, trapdoors, hoppers, furnaces, pistons, anvils, redstone wire, signs, hay/bone
axis...); wall signs had invalid meta 0; button floor/ceiling were swapped and wall facings
shifted; water levels 1-7 were all collapsed to falling (8:0); modern "head" bed parts lacked
the +8 bit. Re-verified after fix: 3,760,128/3,760,128 blocks match, pillar intact through a
server tick+save cycle.

## 9. How to reproduce everything

```bash
cd ACServer/AC-Test/world-port

# 1) analyze a recording (pick the best world)
python3 analyze_recording.py "../path/to/recording.mcpr"

# 2) extract to a world dir
python3 extract_world.py "../path/to/recording.mcpr" /tmp/outworld

# 3) verify block-for-block
python3 verify_world.py "../path/to/recording.mcpr" /tmp/outworld

# 4) install & boot
cp -r /tmp/outworld ACServer/AC-Test/world
cd ACServer/AC-Test && ./run.sh
```

Reference materials used (re-fetch URLs if needed):
- ReplayMod source: https://github.com/ReplayMod/ReplayMod (+ ReplayStudio submodule)
- minecraft-data: https://github.com/PrismarineJS/minecraft-data (`data/pc/1.21.9/protocol.json`, `blocks.json`)
- Chunker (block registry data): https://github.com/HiveGamesOSS/Chunker (`cli/data/java/1.21.10/blocks.json`)
- 1.21.10 client/server jars: piston-meta.mojang.com (see `1.21.10.json` manifest)
- 1.8.8 vanilla server jar (mapping authority): cached at `AC-Test/cache/mojang_1.8.8.jar`
  (downloaded by the PandaSpigot paperclip on first run)
- Vineflower decompiler: https://repo1.maven.org/maven2/org/vineflower/vineflower/1.10.1/ (also in the Zyvori dev kit: `Zyvori/development-kit/Decompilers/`)
- Decompiled sources (throwaway): `/tmp/src-1.21.10/` (client), `/tmp/src-server-1.21.10/` (server)

Key decompiled classes consulted (obfuscated names, 1.21.10):
`aei` (level_chunk_with_light), `aeh` (chunk data), `elw` (section),
`emd`/`elz`/`elt`/`emh`/`bfy` (paletted containers), `epp` (heightmaps),
`aca`/`kb` (registry data), `vc`/`NbtIo` (NBT), `duv`/`dux` (block registry init).
