#!/usr/bin/env python3
"""Extract a ReplayMod .mcpr recording into a 1.8.8 anvil world.

Pipeline: tmcpr -> chunk packets -> paletted sections -> state ids
          -> (Chunker 1.21.10 blocks.json: state id -> name+properties)
          -> 1.8.8 block id:data mapping (with substitutes for post-1.8 blocks)
          -> anvil region files + level.dat (world shifted up 64: modern y -64.. -> 0..)

Verification: `--verify` re-parses the written region files and compares
block-by-block against the recording data (same mapping), reporting mismatches.
"""
import json, os, struct, sys, zipfile, zlib, gzip, math
from collections import Counter
from analyze_recording import read_varint, Buf, NBT, parse_chunk, parse_paletted

HERE = os.path.dirname(os.path.abspath(__file__))
SHIFT = 64  # modern minY = -64 -> 1.8.8 y 0
MAX_Y = 256  # 1.8.8 build height
DATA_VERSION = 1343  # 1.8.8

# ---------------- state id -> (name, properties) ----------------
def load_states():
    ch = json.load(open(os.path.join(HERE, 'chunker-1.21.10-blocks.json')))
    m = {}
    for name, b in ch.items():
        for s in b['states']:
            m[s['id']] = (name, s.get('properties', {}))
    return m

# ---------------- 1.8.8 block mapping ----------------
WOOL = {'white':0,'orange':1,'magenta':2,'light_blue':3,'yellow':4,'lime':5,'pink':6,'gray':7,'light_gray':8,'cyan':9,'purple':10,'blue':11,'brown':12,'green':13,'red':14,'black':15}

def stair_data(facing, half):
    d = {'north':3,'south':2,'west':1,'east':0}[facing]
    return d | (4 if half == 'top' else 0)

def full_index(facing):
    """EnumFacing.getIndex(): down=0, up=1, north=2, south=3, west=4, east=5 (chest/ladder/furnace/dispenser/piston)."""
    return {'north':2,'south':3,'west':4,'east':5}[facing]

def horiz_index(facing):
    """EnumFacing.getHorizontalIndex(): east=0, west=1, south=2, north=3 (gate/bed/anvil/hook/cocoa/rail-end/comparator)."""
    return {'east':0,'west':1,'south':2,'north':3}[facing]

def trapdoor_data(facing, half, open_=False):
    d = {'north':0,'south':1,'west':2,'east':3}[facing]
    if open_ == 'true': d |= 4
    if half == 'top': d |= 8
    return d

def slab_data(t):
    return {'bottom':0,'top':8,'double':8}[t]

def log_data(props):
    wood = {'oak':0,'spruce':1,'birch':2,'jungle':3,'acacia':0,'dark_oak':1}.get(props.get('wood_type','oak'), 0)
    axis = {'y':0,'x':4,'z':8}[props.get('axis','y')]
    return wood | axis

def vine_data(props):
    v = 0
    if props.get('north') == 'true': v |= 1
    if props.get('east') == 'true': v |= 2
    if props.get('south') == 'true': v |= 4
    if props.get('west') == 'true': v |= 8
    return v

def wall_connect(props):
    v = 0
    if props.get('north') in ('low','tall'): v |= 1
    if props.get('east') in ('low','tall'): v |= 2
    if props.get('south') in ('low','tall'): v |= 4
    if props.get('west') in ('low','tall'): v |= 8
    return v

def flower_pot_data(name):
    return 0  # contents live in tile entity; plain pot

# name -> (id, data) for property-free blocks and simple cases
SIMPLE = {
    'minecraft:air': (0,0), 'minecraft:stone': (1,0), 'minecraft:grass_block': (2,0),
    'minecraft:granite': (1,1), 'minecraft:polished_granite': (1,2), 'minecraft:diorite': (1,3),
    'minecraft:polished_diorite': (1,4), 'minecraft:andesite': (1,5), 'minecraft:polished_andesite': (1,6),
    'minecraft:deepslate': (1,0), 'minecraft:dirt': (3,0), 'minecraft:coarse_dirt': (3,1), 'minecraft:podzol': (3,2),
    'minecraft:cobblestone': (4,0), 'minecraft:bedrock': (7,0), 'minecraft:sand': (12,0),
    'minecraft:red_sand': (12,1), 'minecraft:gravel': (13,0), 'minecraft:gold_ore': (14,0),
    'minecraft:iron_ore': (15,0), 'minecraft:coal_ore': (16,0), 'minecraft:sponge': (19,0),
    'minecraft:wet_sponge': (19,1), 'minecraft:glass': (20,0), 'minecraft:lapis_ore': (21,0),
    'minecraft:lapis_block': (22,0), 'minecraft:sandstone': (24,0), 'minecraft:chiseled_sandstone': (24,1),
    'minecraft:cut_sandstone': (24,1), 'minecraft:smooth_sandstone': (24,2),
    'minecraft:note_block': (25,0),    'minecraft:cobweb': (30,0), 'minecraft:short_grass': (31,1), 'minecraft:tall_grass': (31,2),
    'minecraft:fern': (31,3), 'minecraft:large_fern': (31,3), 'minecraft:seagrass': (31,1),
    'minecraft:tall_seagrass': (31,2), 'minecraft:kelp': (31,1), 'minecraft:kelp_plant': (31,1),
    'minecraft:dead_bush': (32,0), 'minecraft:piston_head': (34,0),
    'minecraft:moving_piston': (36,0), 'minecraft:dandelion': (37,0), 'minecraft:poppy': (38,0),
    'minecraft:blue_orchid': (38,1), 'minecraft:allium': (38,2), 'minecraft:azure_bluet': (38,3),
    'minecraft:red_tulip': (38,4), 'minecraft:orange_tulip': (38,5), 'minecraft:white_tulip': (38,6),
    'minecraft:pink_tulip': (38,7), 'minecraft:oxeye_daisy': (38,8), 'minecraft:torchflower': (38,0),
    'minecraft:cornflower': (38,0), 'minecraft:lily_of_the_valley': (38,0), 'minecraft:wither_rose': (38,0),
    'minecraft:closed_eyeblossom': (38,0), 'minecraft:open_eyeblossom': (38,0),
    'minecraft:wildflowers': (38,0), 'minecraft:pink_petals': (38,0), 'minecraft:firefly_bush': (38,0),
    'minecraft:frogspawn': (38,0), 'minecraft:turtle_egg': (38,0), 'minecraft:sniffer_egg': (38,0),
    'minecraft:sea_pickle': (38,0), 'minecraft:torchflower_crop': (38,0),
    'minecraft:brown_mushroom': (39,0), 'minecraft:red_mushroom': (40,0),
    'minecraft:gold_block': (41,0), 'minecraft:iron_block': (42,0), 'minecraft:bricks': (45,0),
    'minecraft:tnt': (46,0), 'minecraft:bookshelf': (47,0), 'minecraft:mossy_cobblestone': (48,0),
    'minecraft:obsidian': (49,0), 'minecraft:crying_obsidian': (49,0),    'minecraft:fire': (51,0),
    'minecraft:soul_fire': (51,0), 'minecraft:spawner': (52,0), 'minecraft:mob_spawner': (52,0),
    'minecraft:trial_spawner': (52,0),    'minecraft:diamond_ore': (56,0), 'minecraft:diamond_block': (57,0),
    'minecraft:crafting_table': (58,0), 'minecraft:barrel': (54,0), 'minecraft:composter': (58,0),
    'minecraft:lectern': (58,0), 'minecraft:loom': (58,0), 'minecraft:cartography_table': (58,0),
    'minecraft:fletching_table': (58,0), 'minecraft:smithing_table': (58,0), 'minecraft:stonecutter': (58,0),
    'minecraft:crafter': (58,0),    'minecraft:blast_furnace': (61,0), 'minecraft:smoker': (61,0),    'minecraft:stone_pressure_plate': (70,0),
    'minecraft:wooden_pressure_plate': (72,0), 'minecraft:redstone_ore': (73,0),
    'minecraft:deepslate_redstone_ore': (73,0),    'minecraft:snow_block': (80,0), 'minecraft:ice': (79,0), 'minecraft:packed_ice': (174,0),
    'minecraft:blue_ice': (174,0), 'minecraft:cactus': (81,0), 'minecraft:cactus_flower': (37,0),
    'minecraft:clay': (82,0), 'minecraft:sugar_cane': (83,0), 'minecraft:bamboo': (83,0),
    'minecraft:bamboo_block': (17,0), 'minecraft:jukebox': (84,0),
    'minecraft:oak_fence': (85,0), 'minecraft:pumpkin': (86,0), 'minecraft:carved_pumpkin': (86,0),
    'minecraft:jack_o_lantern': (91,0), 'minecraft:netherrack': (87,0),
    'minecraft:crimson_nylium': (87,0), 'minecraft:warped_nylium': (87,0),
    'minecraft:soul_sand': (88,0), 'minecraft:soul_soil': (88,0),
    'minecraft:glowstone': (89,0), 'minecraft:light': (89,0), 'minecraft:lantern': (89,0),
    'minecraft:soul_lantern': (89,0), 'minecraft:magma_block': (89,0), 'minecraft:shroomlight': (89,0),
    'minecraft:ochre_froglight': (89,0), 'minecraft:pearlescent_froglight': (89,0),
    'minecraft:verdant_froglight': (89,0), 'minecraft:respawn_anchor': (89,0),
    'minecraft:copper_lantern': (89,0), 'minecraft:copper_bulb': (89,0),
    'minecraft:nether_portal': (90,0), 'minecraft:cake': (92,0), 'minecraft:candle': (50,0),
    'minecraft:candle_cake': (92,0),
    'minecraft:stone_bricks': (98,0), 'minecraft:mossy_stone_bricks': (98,1),
    'minecraft:cracked_stone_bricks': (98,2), 'minecraft:chiseled_stone_bricks': (98,3),
    'minecraft:iron_bars': (101,0), 'minecraft:iron_chain': (101,0), 'minecraft:copper_chain': (101,0),
    'minecraft:glass_pane': (102,0), 'minecraft:copper_grate': (101,0),
    'minecraft:melon': (103,0), 'minecraft:weeping_vines_plant': (106,0),
    'minecraft:twisting_vines_plant': (106,0), 'minecraft:weeping_vines': (106,0),
    'minecraft:twisting_vines': (106,0), 'minecraft:glow_lichen': (106,0), 'minecraft:sculk_vein': (106,0),
    'minecraft:hanging_roots': (106,0), 'minecraft:cave_vines': (106,0), 'minecraft:cave_vines_plant': (106,0),
    'minecraft:leaf_litter': (106,0), 'minecraft:pale_hanging_moss': (106,0),
    'minecraft:mycelium': (110,0), 'minecraft:lily_pad': (111,0),
    'minecraft:big_dripleaf': (111,0), 'minecraft:big_dripleaf_stem': (111,0),
    'minecraft:nether_brick': (112,0), 'minecraft:nether_bricks': (112,0),
    'minecraft:chiseled_nether_bricks': (112,0), 'minecraft:cracked_nether_bricks': (112,0),
    'minecraft:red_nether_brick': (112,0), 'minecraft:red_nether_bricks': (112,0),
    'minecraft:warped_wart_block': (112,0), 'minecraft:nether_wart_block': (112,0),
    'minecraft:nether_brick_fence': (113,0), 'minecraft:nether_wart': (115,0),
    'minecraft:enchanting_table': (116,0), 'minecraft:brewing_stand': (117,0),
    'minecraft:end_portal': (119,0), 'minecraft:end_stone': (121,0), 'minecraft:end_rod': (121,0),
    'minecraft:chorus_plant': (121,0), 'minecraft:chorus_flower': (121,0),
    'minecraft:purpur_block': (121,0), 'minecraft:purpur_pillar': (155,2),
    'minecraft:end_stone_bricks': (121,0), 'minecraft:dragon_egg': (122,0),
    'minecraft:redstone_lamp': (124,0), 'minecraft:emerald_ore': (129,0),
    'minecraft:deepslate_emerald_ore': (129,0),    'minecraft:emerald_block': (133,0), 'minecraft:command_block': (137,0),
    'minecraft:repeating_command_block': (210,0), 'minecraft:chain_command_block': (211,0),
    'minecraft:jigsaw': (137,0), 'minecraft:structure_block': (137,0), 'minecraft:test_block': (137,0),
    'minecraft:test_instance_block': (137,0), 'minecraft:beacon': (138,0),
    'minecraft:cobblestone_wall': (139,0),
    'minecraft:stone_brick_wall': (139,0), 'minecraft:sandstone_wall': (139,0),
    'minecraft:red_sandstone_wall': (139,0), 'minecraft:nether_brick_wall': (139,0),
    'minecraft:mossy_cobblestone_wall': (139,0), 'minecraft:mossy_stone_brick_wall': (139,0),
    'minecraft:granite_wall': (139,0), 'minecraft:diorite_wall': (139,0), 'minecraft:andesite_wall': (139,0),
    'minecraft:prismarine_wall': (139,0), 'minecraft:blackstone_wall': (139,0),
    'minecraft:polished_blackstone_wall': (139,0), 'minecraft:polished_blackstone_brick_wall': (139,0),
    'minecraft:deepslate_brick_wall': (139,0), 'minecraft:deepslate_tile_wall': (139,0),
    'minecraft:cobbled_deepslate_wall': (139,0), 'minecraft:polished_deepslate_wall': (139,0),
    'minecraft:brick_wall': (139,0), 'minecraft:end_stone_brick_wall': (139,0),
    'minecraft:mud_brick_wall': (139,0), 'minecraft:red_nether_brick_wall': (139,0),
    'minecraft:tuff_wall': (139,0), 'minecraft:tuff_brick_wall': (139,0),
    'minecraft:polished_tuff_wall': (139,0), 'minecraft:resin_brick_wall': (139,0),
    'minecraft:flower_pot': (140,0), 'minecraft:decorated_pot': (140,0),
    'minecraft:quartz_block': (155,0), 'minecraft:quartz_bricks': (155,0),
    'minecraft:chiseled_quartz_block': (155,1), 'minecraft:smooth_quartz': (155,0),
    'minecraft:hardened_clay': (172,0), 'minecraft:terracotta': (172,0),
    'minecraft:coal_block': (173,0),    'minecraft:slime_block': (165,0), 'minecraft:honey_block': (165,0),
    'minecraft:barrier': (166,0),    'minecraft:prismarine': (168,0), 'minecraft:prismarine_bricks': (168,1),
    'minecraft:dark_prismarine': (168,2), 'minecraft:sea_lantern': (169,0),
    'minecraft:conduit': (169,0), 'minecraft:red_sandstone': (179,0),
    'minecraft:chiseled_red_sandstone': (179,1), 'minecraft:smooth_red_sandstone': (179,2),
    'minecraft:cut_red_sandstone': (179,1),    'minecraft:spruce_fence': (188,0), 'minecraft:birch_fence': (189,0),
    'minecraft:jungle_fence': (190,0), 'minecraft:dark_oak_fence': (191,0),
    'minecraft:acacia_fence': (192,0), 'minecraft:end_gateway': (119,0),
    'minecraft:structure_void': (0,0),
    'minecraft:observer': (1,0), 'minecraft:sculk_sensor': (1,0),
    'minecraft:calibrated_sculk_sensor': (1,0), 'minecraft:sculk_shrieker': (1,0),
    'minecraft:sculk_catalyst': (1,0), 'minecraft:sculk': (1,0),
    'minecraft:scaffolding': (65,0), 'minecraft:campfire': (87,0), 'minecraft:soul_campfire': (87,0),
    'minecraft:bell': (25,0), 'minecraft:grindstone': (145,0),
    'minecraft:amethyst_block': (35,10), 'minecraft:budding_amethyst': (35,10),
    'minecraft:amethyst_cluster': (35,10), 'minecraft:large_amethyst_bud': (35,10),
    'minecraft:medium_amethyst_bud': (35,10), 'minecraft:small_amethyst_bud': (35,10),
    'minecraft:azalea': (18,0), 'minecraft:flowering_azalea': (18,0),
    'minecraft:azalea_leaves': (18,0), 'minecraft:flowering_azalea_leaves': (18,0),
    'minecraft:moss_block': (2,0), 'minecraft:pale_moss_block': (2,0), 'minecraft:powder_snow': (80,0),
    'minecraft:mud': (3,0), 'minecraft:packed_mud': (3,0), 'minecraft:mud_bricks': (4,0),
    'minecraft:muddy_mangrove_roots': (3,0), 'minecraft:rooted_dirt': (3,0), 'minecraft:dirt_path': (3,0),
    'minecraft:mangrove_roots': (3,0), 'minecraft:smooth_basalt': (1,5),
    'minecraft:basalt': (4,0), 'minecraft:polished_basalt': (4,0),
    'minecraft:blackstone': (4,0), 'minecraft:gilded_blackstone': (4,0),
    'minecraft:polished_blackstone': (4,0), 'minecraft:polished_blackstone_bricks': (4,0),
    'minecraft:chiseled_polished_blackstone': (4,0), 'minecraft:cracked_polished_blackstone_bricks': (4,0),
    'minecraft:cobbled_deepslate': (4,0), 'minecraft:deepslate_bricks': (1,0),
    'minecraft:cracked_deepslate_bricks': (1,0), 'minecraft:deepslate_tiles': (1,0),
    'minecraft:cracked_deepslate_tiles': (1,0), 'minecraft:chiseled_deepslate': (1,0),
    'minecraft:polished_deepslate': (1,0), 'minecraft:reinforced_deepslate': (1,0),
    'minecraft:infested_deepslate': (1,0), 'minecraft:tuff': (1,0), 'minecraft:tuff_bricks': (1,0),
    'minecraft:chiseled_tuff': (1,0), 'minecraft:chiseled_tuff_bricks': (1,0),
    'minecraft:polished_tuff': (1,0), 'minecraft:calcite': (1,0),
    'minecraft:tinted_glass': (20,0), 'minecraft:infested_stone': (97,0),
    'minecraft:infested_cobblestone': (97,1), 'minecraft:infested_stone_bricks': (97,2),
    'minecraft:infested_mossy_stone_bricks': (97,3), 'minecraft:infested_cracked_stone_bricks': (97,4),
    'minecraft:infested_chiseled_stone_bricks': (97,5),
    'minecraft:tube_coral_fan': (32,0), 'minecraft:tube_coral_wall_fan': (32,0),
    'minecraft:tube_coral': (32,0), 'minecraft:tube_coral_block': (32,0),
    'minecraft:bubble_coral': (32,0), 'minecraft:fire_coral': (32,0), 'minecraft:horn_coral': (32,0),
    'minecraft:brain_coral': (32,0), 'minecraft:dead_tube_coral': (32,0), 'minecraft:dead_bubble_coral': (32,0),
    'minecraft:dead_fire_coral': (32,0), 'minecraft:dead_horn_coral': (32,0), 'minecraft:dead_brain_coral': (32,0),
    'minecraft:dead_tube_coral_fan': (32,0), 'minecraft:dead_bubble_coral_fan': (32,0),
    'minecraft:dead_fire_coral_fan': (32,0), 'minecraft:dead_horn_coral_fan': (32,0),
    'minecraft:dead_brain_coral_fan': (32,0), 'minecraft:dead_tube_coral_block': (32,0),
    'minecraft:dead_bubble_coral_block': (32,0), 'minecraft:dead_fire_coral_block': (32,0),
    'minecraft:dead_horn_coral_block': (32,0), 'minecraft:dead_brain_coral_block': (32,0),
    'minecraft:bubble_column': (9,0), 'minecraft:warped_fungus': (39,0),
    'minecraft:crimson_fungus': (40,0), 'minecraft:warped_roots': (31,1),
    'minecraft:crimson_roots': (31,1), 'minecraft:nether_sprouts': (31,1),
    'minecraft:purpur_stairs': (156,0), 'minecraft:frosted_ice': (79,0),
    'minecraft:ancient_debris': (4,0), 'minecraft:netherite_block': (42,0),
    'minecraft:lodestone': (49,0), 'minecraft:dried_kelp_block': (35,13),
    'minecraft:honeycomb_block': (35,14), 'minecraft:resin_block': (35,14),
    'minecraft:resin_bricks': (35,14), 'minecraft:chiseled_resin_bricks': (35,14),
    'minecraft:resin_clump': (35,14), 'minecraft:target': (35,14),
    'minecraft:raw_iron_block': (15,0), 'minecraft:raw_gold_block': (14,0),
    'minecraft:raw_copper_block': (14,0), 'minecraft:copper_ore': (14,0),
    'minecraft:deepslate_copper_ore': (14,0), 'minecraft:deepslate_iron_ore': (15,0),
    'minecraft:deepslate_gold_ore': (14,0), 'minecraft:deepslate_diamond_ore': (56,0),
    'minecraft:deepslate_lapis_ore': (21,0), 'minecraft:deepslate_coal_ore': (16,0),
    'minecraft:nether_quartz_ore': (153,0), 'minecraft:nether_gold_ore': (14,0),
    'minecraft:copper_block': (159,1), 'minecraft:exposed_copper': (159,1),
    'minecraft:weathered_copper': (159,1), 'minecraft:oxidized_copper': (159,1),
    'minecraft:cut_copper': (159,1), 'minecraft:exposed_cut_copper': (159,1),
    'minecraft:weathered_cut_copper': (159,1), 'minecraft:oxidized_cut_copper': (159,1),
    'minecraft:chiseled_copper': (159,1), 'minecraft:exposed_chiseled_copper': (159,1),
    'minecraft:weathered_chiseled_copper': (159,1), 'minecraft:oxidized_chiseled_copper': (159,1),
    'minecraft:waxed_copper_block': (159,1), 'minecraft:waxed_cut_copper': (159,1),
    'minecraft:waxed_exposed_copper': (159,1), 'minecraft:waxed_weathered_copper': (159,1),
    'minecraft:waxed_oxidized_copper': (159,1), 'minecraft:waxed_exposed_cut_copper': (159,1),
    'minecraft:waxed_weathered_cut_copper': (159,1), 'minecraft:waxed_oxidized_cut_copper': (159,1),
    'minecraft:waxed_chiseled_copper': (159,1), 'minecraft:waxed_exposed_chiseled_copper': (159,1),
    'minecraft:waxed_weathered_chiseled_copper': (159,1), 'minecraft:waxed_oxidized_chiseled_copper': (159,1),
    'minecraft:waxed_copper_grate': (101,0), 'minecraft:exposed_copper_grate': (101,0),
    'minecraft:weathered_copper_grate': (101,0), 'minecraft:oxidized_copper_grate': (101,0),
    'minecraft:waxed_copper_chain': (101,0), 'minecraft:exposed_copper_chain': (101,0),
    'minecraft:weathered_copper_chain': (101,0), 'minecraft:oxidized_copper_chain': (101,0),
    'minecraft:waxed_copper_bulb': (89,0), 'minecraft:exposed_copper_bulb': (89,0),
    'minecraft:weathered_copper_bulb': (89,0), 'minecraft:oxidized_copper_bulb': (89,0),
    'minecraft:waxed_copper_lantern': (89,0), 'minecraft:exposed_copper_lantern': (89,0),
    'minecraft:weathered_copper_lantern': (89,0), 'minecraft:oxidized_copper_lantern': (89,0),
    'minecraft:copper_golem_statue': (159,1), 'minecraft:waxed_copper_golem_statue': (159,1),
    'minecraft:exposed_copper_golem_statue': (159,1), 'minecraft:weathered_copper_golem_statue': (159,1),
    'minecraft:oxidized_copper_golem_statue': (159,1),
    'minecraft:waxed_exposed_copper_golem_statue': (159,1), 'minecraft:waxed_weathered_copper_golem_statue': (159,1),
    'minecraft:waxed_oxidized_copper_golem_statue': (159,1),
    'minecraft:dried_ghast': (35,15), 'minecraft:heavy_core': (4,0),
    'minecraft:creaking_heart': (17,0), 'minecraft:vault': (52,0),
    'minecraft:lightning_rod': (101,0), 'minecraft:exposed_lightning_rod': (101,0),
    'minecraft:weathered_lightning_rod': (101,0), 'minecraft:oxidized_lightning_rod': (101,0),
    'minecraft:waxed_lightning_rod': (101,0), 'minecraft:waxed_exposed_lightning_rod': (101,0),
    'minecraft:waxed_weathered_lightning_rod': (101,0), 'minecraft:waxed_oxidized_lightning_rod': (101,0),
    'minecraft:copper_chest': (54,0), 'minecraft:exposed_copper_chest': (54,0),
    'minecraft:weathered_copper_chest': (54,0), 'minecraft:oxidized_copper_chest': (54,0),
    'minecraft:waxed_copper_chest': (54,0), 'minecraft:waxed_exposed_copper_chest': (54,0),
    'minecraft:waxed_weathered_copper_chest': (54,0), 'minecraft:waxed_oxidized_copper_chest': (54,0),
    'minecraft:beehive': (17,0), 'minecraft:bee_nest': (17,0),
    'minecraft:pointed_dripstone': (1,0), 'minecraft:dripstone_block': (1,0),
    'minecraft:suspicious_sand': (12,0), 'minecraft:suspicious_gravel': (13,0),
    'minecraft:sweet_berry_bush': (31,1), 'minecraft:bush': (31,1),
    'minecraft:short_dry_grass': (31,1), 'minecraft:tall_dry_grass': (31,2),
    'minecraft:pitcher_crop': (31,1), 'minecraft:pitcher_plant': (175,4),
    'minecraft:mangrove_propagule': (6,0), 'minecraft:oak_sapling': (6,0),
    'minecraft:spruce_sapling': (6,1), 'minecraft:birch_sapling': (6,2),
    'minecraft:jungle_sapling': (6,3), 'minecraft:acacia_sapling': (6,4),
    'minecraft:dark_oak_sapling': (6,5), 'minecraft:cherry_sapling': (6,0),
    'minecraft:pale_oak_sapling': (6,0), 'minecraft:bamboo_sapling': (6,0),
    'minecraft:powder_snow_cauldron': (118,3), 'minecraft:lava_cauldron': (118,0),
    'minecraft:brown_mushroom_block': (99,0),
    'minecraft:red_mushroom_block': (100,0), 'minecraft:mushroom_stem': (100,0),
    'minecraft:spore_blossom': (38,0), 'minecraft:crimson_stem': (17,0),
    'minecraft:warped_stem': (17,0), 'minecraft:bamboo_mosaic': (83,0),
    'minecraft:smooth_stone': (43,8), 'minecraft:chiseled_bookshelf': (47,0),
    'minecraft:birch_shelf': (47,0), 'minecraft:cherry_shelf': (47,0), 'minecraft:crimson_shelf': (47,0),
    'minecraft:dark_oak_shelf': (47,0), 'minecraft:jungle_shelf': (47,0), 'minecraft:mangrove_shelf': (47,0),
    'minecraft:oak_shelf': (47,0), 'minecraft:pale_oak_shelf': (47,0), 'minecraft:spruce_shelf': (47,0),
    'minecraft:warped_shelf': (47,0), 'minecraft:resin_brick_wall': (139,0),
    'minecraft:black_candle': (50,0), 'minecraft:blue_candle': (50,0), 'minecraft:brown_candle': (50,0),
    'minecraft:cyan_candle': (50,0), 'minecraft:gray_candle': (50,0), 'minecraft:green_candle': (50,0),
    'minecraft:light_blue_candle': (50,0), 'minecraft:light_gray_candle': (50,0),
    'minecraft:lime_candle': (50,0), 'minecraft:magenta_candle': (50,0), 'minecraft:orange_candle': (50,0),
    'minecraft:pink_candle': (50,0), 'minecraft:purple_candle': (50,0), 'minecraft:red_candle': (50,0),
    'minecraft:white_candle': (50,0), 'minecraft:yellow_candle': (50,0),
    'minecraft:black_candle_cake': (92,0), 'minecraft:blue_candle_cake': (92,0),
    'minecraft:brown_candle_cake': (92,0), 'minecraft:cyan_candle_cake': (92,0),
    'minecraft:gray_candle_cake': (92,0), 'minecraft:green_candle_cake': (92,0),
    'minecraft:light_blue_candle_cake': (92,0), 'minecraft:light_gray_candle_cake': (92,0),
    'minecraft:lime_candle_cake': (92,0), 'minecraft:magenta_candle_cake': (92,0),
    'minecraft:orange_candle_cake': (92,0), 'minecraft:pink_candle_cake': (92,0),
    'minecraft:purple_candle_cake': (92,0), 'minecraft:red_candle_cake': (92,0),
    'minecraft:white_candle_cake': (92,0), 'minecraft:yellow_candle_cake': (92,0),
    'minecraft:small_dripleaf': (111,0), 'minecraft:acacia_sign': (63,0),
    'minecraft:birch_sign': (63,0), 'minecraft:dark_oak_sign': (63,0), 'minecraft:jungle_sign': (63,0),
    'minecraft:spruce_sign': (63,0), 'minecraft:mangrove_sign': (63,0), 'minecraft:cherry_sign': (63,0),
    'minecraft:pale_oak_sign': (63,0), 'minecraft:crimson_sign': (63,0), 'minecraft:warped_sign': (63,0),
    'minecraft:bamboo_sign': (63,0),  
    'minecraft:birch_wall_sign': (68,0), 
     
     
         
    'minecraft:acacia_hanging_sign': (63,0), 'minecraft:birch_hanging_sign': (63,0),
    'minecraft:dark_oak_hanging_sign': (63,0), 'minecraft:jungle_hanging_sign': (63,0),
    'minecraft:spruce_hanging_sign': (63,0), 'minecraft:mangrove_hanging_sign': (63,0),
    'minecraft:cherry_hanging_sign': (63,0), 'minecraft:pale_oak_hanging_sign': (63,0),
    'minecraft:crimson_hanging_sign': (63,0), 'minecraft:warped_hanging_sign': (63,0),
    'minecraft:bamboo_hanging_sign': (63,0), 'minecraft:oak_hanging_sign': (63,0),
    'minecraft:acacia_wall_hanging_sign': (68,0), 'minecraft:birch_wall_hanging_sign': (68,0),
    'minecraft:dark_oak_wall_hanging_sign': (68,0), 'minecraft:jungle_wall_hanging_sign': (68,0),
    'minecraft:spruce_wall_hanging_sign': (68,0), 'minecraft:mangrove_wall_hanging_sign': (68,0),
    'minecraft:cherry_wall_hanging_sign': (68,0), 'minecraft:pale_oak_wall_hanging_sign': (68,0),
    'minecraft:crimson_wall_hanging_sign': (68,0), 'minecraft:warped_wall_hanging_sign': (68,0),
    'minecraft:bamboo_wall_hanging_sign': (68,0), 'minecraft:oak_wall_hanging_sign': (68,0),
    'minecraft:white_banner': (176,0), 'minecraft:orange_banner': (176,0),
    'minecraft:magenta_banner': (176,0), 'minecraft:light_blue_banner': (176,0),
    'minecraft:yellow_banner': (176,0), 'minecraft:lime_banner': (176,0),
    'minecraft:pink_banner': (176,0), 'minecraft:gray_banner': (176,0),
    'minecraft:light_gray_banner': (176,0), 'minecraft:cyan_banner': (176,0),
    'minecraft:purple_banner': (176,0), 'minecraft:blue_banner': (176,0),
    'minecraft:brown_banner': (176,0), 'minecraft:green_banner': (176,0),
    'minecraft:red_banner': (176,0), 'minecraft:black_banner': (176,0),
    'minecraft:white_wall_banner': (177,0), 'minecraft:orange_wall_banner': (177,0),
    'minecraft:magenta_wall_banner': (177,0), 'minecraft:light_blue_wall_banner': (177,0),
    'minecraft:yellow_wall_banner': (177,0), 'minecraft:lime_wall_banner': (177,0),
    'minecraft:pink_wall_banner': (177,0), 'minecraft:gray_wall_banner': (177,0),
    'minecraft:light_gray_wall_banner': (177,0), 'minecraft:cyan_wall_banner': (177,0),
    'minecraft:purple_wall_banner': (177,0), 'minecraft:blue_wall_banner': (177,0),
    'minecraft:brown_wall_banner': (177,0), 'minecraft:green_wall_banner': (177,0),
    'minecraft:red_wall_banner': (177,0), 'minecraft:black_wall_banner': (177,0),
    'minecraft:wheat': (59,0), 'minecraft:carrots': (141,0), 'minecraft:potatoes': (142,0),
    'minecraft:beetroots': (207,0), 'minecraft:melon_stem': (105,0), 'minecraft:pumpkin_stem': (104,0),
    'minecraft:attached_melon_stem': (105,0), 'minecraft:attached_pumpkin_stem': (104,0),
    'minecraft:shulker_box': (159,0), 'minecraft:lime_shulker_box': (159,5),
    'minecraft:pink_shulker_box': (159,6), 'minecraft:green_shulker_box': (159,13),
    'minecraft:red_shulker_box': (159,14), 'minecraft:purple_shulker_box': (159,10),
    'minecraft:blue_shulker_box': (159,11), 'minecraft:cyan_shulker_box': (159,9),
    'minecraft:gray_shulker_box': (159,7), 'minecraft:light_gray_shulker_box': (159,8),
    'minecraft:black_shulker_box': (159,15), 'minecraft:brown_shulker_box': (159,12),
    'minecraft:white_shulker_box': (159,0), 'minecraft:orange_shulker_box': (159,1),
    'minecraft:yellow_shulker_box': (159,4), 'minecraft:magenta_shulker_box': (159,2),
    'minecraft:light_blue_shulker_box': (159,3),
    'minecraft:green_glazed_terracotta': (159,13),
    'minecraft:creeper_wall_head': (144,4), 'minecraft:skeleton_wall_skull': (144,0),
    'minecraft:wither_skeleton_wall_skull': (144,1), 'minecraft:zombie_wall_head': (144,2),
    'minecraft:piglin_head': (144,3), 'minecraft:piglin_wall_head': (144,3),
    'minecraft:dragon_wall_head': (144,5),
    'minecraft:acacia_pressure_plate': (72,0), 'minecraft:birch_pressure_plate': (72,0),
    'minecraft:jungle_pressure_plate': (72,0), 'minecraft:spruce_pressure_plate': (72,0),
    'minecraft:dark_oak_pressure_plate': (72,0), 'minecraft:mangrove_pressure_plate': (72,0),
    'minecraft:cherry_pressure_plate': (72,0), 'minecraft:pale_oak_pressure_plate': (72,0),
    'minecraft:crimson_pressure_plate': (72,0), 'minecraft:warped_pressure_plate': (72,0),
    'minecraft:bamboo_pressure_plate': (72,0), 'minecraft:polished_blackstone_pressure_plate': (72,0),
    'minecraft:bamboo_fence': (85,0), 'minecraft:cherry_fence': (85,0),
    'minecraft:mangrove_fence': (85,0), 'minecraft:pale_oak_fence': (85,0),
    'minecraft:crimson_fence': (85,0), 'minecraft:warped_fence': (85,0),
    'minecraft:pale_moss_carpet': (171,13), 'minecraft:moss_carpet': (171,13),
    'minecraft:farmland': (60,0), 'minecraft:acacia_wood': (17,0),
    'minecraft:birch_wood': (17,1), 'minecraft:dark_oak_wood': (17,1),
    'minecraft:oak_wood': (17,0), 'minecraft:spruce_wood': (17,1),
    'minecraft:cherry_wood': (17,0), 'minecraft:pale_oak_wood': (17,0),
    'minecraft:mangrove_wood': (17,0),}

def map_block(name, props):
    if name in SIMPLE:
        return SIMPLE[name]
    base = name[len('minecraft:'):]
    if base.startswith('potted_'):
        return (140, 0)
    # colored blocks: <color>_<base> (lime_terracotta, gray_concrete, ...)
    for base2, bid in (('wool',35), ('terracotta',159), ('concrete',159), ('stained_glass',95),
                       ('stained_glass_pane',160), ('carpet',171), ('shulker_box',159),
                       ('glazed_terracotta',159)):
        if base.endswith('_' + base2) and base[:-len(base2)-1] in WOOL:
            return (bid, WOOL[base[:-len(base2)-1]])
    # planks
    if base.endswith('_planks'):
        return (5, {'oak':0,'spruce':1,'birch':2,'jungle':3,'acacia':4,'dark_oak':5}.get(base[:-7], 0))
    # logs
    if base.endswith('_log') or base.endswith('_hyphae') or base.startswith('stripped_'):
        wood = {'oak':0,'spruce':1,'birch':2,'jungle':3,'acacia':0,'dark_oak':1}.get(props.get('wood_type','oak'), 0)
        bid = 162 if props.get('wood_type') in ('acacia','dark_oak') else 17
        return (bid, wood | {'y':0,'x':4,'z':8}[props.get('axis','y')])
    # leaves
    if base.endswith('_leaves'):
        wood = {'oak':0,'spruce':1,'birch':2,'jungle':3,'acacia':0,'dark_oak':1}.get(base[:-7], 0)
        return (18 if wood < 4 else 161, wood % 4)
    # stairs
    if base.endswith('_stairs'):
        bid = {'oak_stairs':53,'cobblestone_stairs':67,'brick_stairs':108,'stone_brick_stairs':109,
               'nether_brick_stairs':114,'sandstone_stairs':128,'spruce_stairs':134,'birch_stairs':135,
               'jungle_stairs':136,'quartz_stairs':156,'acacia_stairs':163,'dark_oak_stairs':164,
               'red_sandstone_stairs':180,'prismarine_stairs':156,'prismarine_brick_stairs':156,
               'dark_prismarine_stairs':156,'smooth_sandstone_stairs':128,'purpur_stairs':156,
               'polished_blackstone_stairs':67,'blackstone_stairs':67}.get(base, 53)
        return (bid, stair_data(props.get('facing','north'), props.get('half','bottom')))
    # slabs
    if base.endswith('_slab'):
        t = props.get('type', 'bottom')
        if base == 'smooth_stone_slab':  # stone slab family
            d = slab_data(t)
            return (43 if t == 'double' else 44, 8 if t == 'double' else d)
        if base == 'oak_slab': d = 0
        elif base == 'spruce_slab': d = 1
        elif base == 'birch_slab': d = 2
        elif base == 'jungle_slab': d = 3
        elif base == 'acacia_slab': d = 4
        elif base == 'dark_oak_slab': d = 5
        elif base == 'stone_slab': d = 0
        elif base == 'sandstone_slab': d = 1
        elif base == 'cobblestone_slab': d = 3
        elif base == 'brick_slab': d = 4
        elif base == 'stone_brick_slab': d = 5
        elif base == 'nether_brick_slab': d = 6
        elif base == 'quartz_slab': d = 7
        elif base in ('prismarine_slab', 'dark_prismarine_slab', 'purpur_slab'): d = 7
        elif base == 'red_sandstone_slab': d = 0
        else: d = 0
        if base == 'red_sandstone_slab':
            return (181 if t == 'double' else 182, slab_data(t))
        if base in ('oak_slab','spruce_slab','birch_slab','jungle_slab','acacia_slab','dark_oak_slab'):
            return (125 if t == 'double' else 126, d | (8 if t == 'top' else 0))
        return (43 if t == 'double' else 44, d | (8 if t == 'top' else 0))
    # doors (1.8.8 BlockDoor: lower meta = facing.e().b() => N=0, E=2, S=1, W=3; open +4;
    # upper: 8 | right-hinge(1) | open(2))
    if base.endswith('_door'):
        bid = {'oak_door':64,'iron_door':71,'spruce_door':193,'birch_door':194,'jungle_door':195,
               'acacia_door':196,'dark_oak_door':197}.get(base, 64)
        half = props.get('half', 'lower')
        if half == 'upper':
            d = 8
            if props.get('hinge') == 'right': d |= 1
            if props.get('open') == 'true': d |= 2
            return (bid, d)
        facing = {'north':0,'east':2,'south':1,'west':3}[props.get('facing','north')]
        d = facing | (4 if props.get('open') == 'true' else 0)
        return (bid, d)
    # trapdoors (1.8.8 akh: N=0, S=1, W=2, E=3; open +4; top +8)
    if base.endswith('_trapdoor'):
        bid = 96 if base != 'iron_trapdoor' else 167
        return (bid, trapdoor_data(props.get('facing','north'), props.get('half','bottom'),
                                   props.get('open','false')))
    # fence gates (1.8.8 agu: E=0, W=1, S=2, N=3; open +4; powered +8)
    if base.endswith('_fence_gate'):
        bid = {'oak_fence_gate':107,'spruce_fence_gate':183,'birch_fence_gate':184,
               'jungle_fence_gate':185,'dark_oak_fence_gate':186,'acacia_fence_gate':187}.get(base, 107)
        d = horiz_index(props.get('facing','north'))
        if props.get('open') == 'true': d |= 4
        if props.get('powered') == 'true': d |= 8
        return (bid, d)
    # buttons (1.8.8 afn: wall E=1, W=2, S=3, N=4; floor=5; ceiling=0; powered +8)
    if base in ('stone_button', 'wooden_button', 'oak_button', 'spruce_button', 'birch_button',
                'jungle_button', 'acacia_button', 'dark_oak_button', 'mangrove_button',
                'cherry_button', 'pale_oak_button', 'crimson_button', 'warped_button',
                'bamboo_button', 'polished_blackstone_button'):
        bid = 77 if base == 'stone_button' else 143
        face = props.get('face', 'wall')
        facing = props.get('facing', 'north')
        if face == 'floor': d = 5
        elif face == 'ceiling': d = 0
        else: d = {'east':1,'west':2,'south':3,'north':4}[facing]
        return (bid, d | (8 if props.get('powered') == 'true' else 0))
    # lever (1.8.8 ahu: wall E=1, W=2, S=3, N=4; floor up_x=6 (facing E/W) / up_z=5; ceiling down_x=0 / down_z=7)
    if base == 'lever':
        face = props.get('face', 'wall')
        facing = props.get('facing', 'north')
        if face == 'floor':
            d = 6 if facing in ('east','west') else 5
        elif face == 'ceiling':
            d = 0 if facing in ('east','west') else 7
        else:
            d = {'east':1,'west':2,'south':3,'north':4}[facing]
        return (69, d | (8 if props.get('powered') == 'true' else 0))
    # pressure plates
    if base in ('light_weighted_pressure_plate','heavy_weighted_pressure_plate'):
        bid = 147 if base == 'light_weighted_pressure_plate' else 148
        return (bid, int(props.get('power', 0)))
    if base == 'stone_pressure_plate': return (70, int(props.get('powered') == 'true'))
    if base == 'oak_pressure_plate': return (72, int(props.get('powered') == 'true'))
    if base in ('dark_oak_pressure_plate','jungle_pressure_plate'):
        return (72, int(props.get('powered') == 'true'))
    # water / lava (1.8.8: source=9:0; flowing level 1-7 = depth; 8+ (falling) = 8:0)
    if base == 'water':
        lvl = int(props.get('level', '0'))
        if lvl == 0: return (9, 0)
        if lvl <= 7: return (8, lvl)
        return (8, 0)
    if base == 'lava':
        lvl = int(props.get('level', '0'))
        if lvl == 0: return (11, 0)
        if lvl <= 7: return (10, lvl)
        return (10, 0)
    if base == 'water_cauldron': return (118, int(props.get('level', 0)))
    if base == 'cauldron': return (118, 0)
    # snow
    if base == 'snow':
        return (78, min(7, int(props.get('layers', 1)) - 1))
    # vines / walls / cocoa / tripwire
    if base == 'vine': return (106, vine_data(props))
    if base == 'cobblestone_wall':
        d = wall_connect(props)
        if props.get('mossy') == 'true': d |= 16
        return (139, d)
    # cocoa (1.8.8 afu: facing E=0, W=1, S=2, N=3 in low bits; age 0-2 in bits 2-3)
    if base == 'cocoa':
        age = int(props.get('age', 0))
        return (127, horiz_index(props.get('facing','north')) | (age << 2))
    if base == 'tripwire':
        d = 0
        if props.get('powered') == 'true': d |= 1
        if props.get('attached') == 'true': d |= 4
        if props.get('disarmed') == 'true': d |= 8
        return (132, d)
    # tripwire hook (1.8.8 akj: E=0, W=1, S=2, N=3; attached +4; powered +8)
    if base == 'tripwire_hook':
        d = horiz_index(props.get('facing','north'))
        if props.get('attached') == 'true': d |= 4
        if props.get('powered') == 'true': d |= 8
        return (131, d)
    # end portal frame (1.8.8 ago: E=0, W=1, S=2, N=3; eye +4)
    if base == 'end_portal_frame':
        d = horiz_index(props.get('facing','south'))
        if props.get('eye') == 'true': d |= 4
        return (120, d)
    if base == 'redstone_lamp':
        return (124 if props.get('lit') == 'true' else 123, 0)
    # repeater (1.8.8 ajf: facing E=0, W=1, S=2, N=3 in low bits; delay-1 in bits 2-3)
    if base == 'repeater':
        d = horiz_index(props.get('facing','north'))
        d |= (int(props.get('delay', 1)) - 1) << 2
        return (94 if props.get('powered') == 'true' else 93, d)
    # comparator (1.8.8 afx: facing E=0, W=1, S=2, N=3; subtract +4; powered +8)
    if base == 'comparator':
        d = horiz_index(props.get('facing','north'))
        if props.get('mode') == 'subtract': d |= 4
        return (150 if props.get('powered') == 'true' else 149, d)
    if base == 'daylight_detector':
        d = int(props.get('power', 0))
        return (178 if props.get('inverted') == 'true' else 151, d)
    if base == 'redstone_block': return (152, 0)
    # beds (1.8.8 afg: E=0, W=1, S=2, N=3; head +8)
    if base == 'bed' or base.endswith('_bed'):
        d = horiz_index(props.get('facing','north'))
        if props.get('part') == 'head': d |= 8
        return (26, d)
    # signs (1.8.8: standing 63 = rotation 0-15; wall 68 = N=2, S=3, W=4, E=5)
    if base.endswith('_sign') and not base.endswith('_wall_sign') and 'hanging' not in base:
        return (63, int(props.get('rotation', 0)) & 15)
    if base.endswith('_wall_sign'):
        return (68, full_index(props.get('facing','north')))
    if 'hanging_sign' in base:
        return (63, 0)
    if base in ('magenta_wall_banner','purple_wall_banner'): return (177, 0)
    if base == 'banner': return (176, 0)
    if base == 'standing_banner': return (176, 0)
    if base in ('oak_log','stripped_oak_log','spruce_log','birch_log','jungle_log','dark_oak_log',
                'acacia_log','stripped_dark_oak_log','jungle_wood','stripped_warped_hyphae'):
        return (162 if 'acacia' in base or 'dark_oak' in base else 17,
                {'y':0,'x':4,'z':8}[props.get('axis','y')])
    if base == 'hay_block':
        return (170, {'y':0,'x':4,'z':8}[props.get('axis','y')])
    if base == 'quartz_pillar':
        return (155, {'y':2,'x':3,'z':4}[props.get('axis','y')])
    if base == 'bone_block':
        return (155, {'y':2,'x':3,'z':4}[props.get('axis','y')])
    # rails (1.8.8 aix: 0=NS,1=EW,2=ascE,3=ascW,4=ascN,5=ascS,6-9 corners; powered +8)
    RAIL_SHAPE = {'north_south':0,'east_west':1,'ascending_east':2,'ascending_west':3,
                  'ascending_north':4,'ascending_south':5,'corner_north_east':6,'corner_south_east':7,
                  'corner_south_west':8,'corner_north_west':9}
    if base == 'rail':
        return (66, RAIL_SHAPE.get(props.get('shape','north_south'), 0))
    if base == 'powered_rail':
        d = RAIL_SHAPE.get(props.get('shape','north_south'), 0)
        if props.get('powered') == 'true': d |= 8
        return (27, d)
    if base == 'activator_rail':
        d = RAIL_SHAPE.get(props.get('shape','north_south'), 0)
        if props.get('powered') == 'true': d |= 8
        return (157, d)
    if base == 'detector_rail':
        return (28, RAIL_SHAPE.get(props.get('shape','north_south'), 0))
    # hopper (1.8.8 ahn: facing full index down=0, N=2, S=3, W=4, E=5; disabled +8)
    if base == 'hopper':
        d = {'down':0,'north':2,'south':3,'west':4,'east':5}[props.get('facing','down')]
        if props.get('enabled') == 'false': d |= 8
        return (154, d)
    # furnace (1.8.8 ahb: N=2, S=3, W=4, E=5)
    if base == 'furnace':
        return (61, full_index(props.get('facing','north')))
    # dispenser / dropper (1.8.8: full index down=0, up=1, N=2, S=3, W=4, E=5; triggered +8)
    if base in ('dispenser', 'dropper'):
        bid = 23 if base == 'dispenser' else 158
        d = {'down':0,'up':1,'north':2,'south':3,'west':4,'east':5}[props.get('facing','north')]
        if props.get('triggered') == 'true': d |= 8
        return (bid, d)
    # pistons (1.8.8 als: full index + extended 8)
    if base in ('piston', 'sticky_piston'):
        bid = 29 if base == 'sticky_piston' else 33
        d = {'down':0,'up':1,'north':2,'south':3,'west':4,'east':5}[props.get('facing','north')]
        if props.get('extended') == 'true': d |= 8
        return (bid, d)
    # anvil (1.8.8 aez: E=0, W=1, S=2, N=3; damage 0/1/2 << 2)
    if base == 'anvil':
        return (145, horiz_index(props.get('facing','north')))
    if base == 'chipped_anvil':
        return (145, horiz_index(props.get('facing','north')) | 4)
    if base == 'damaged_anvil':
        return (145, horiz_index(props.get('facing','north')) | 8)
    # skulls (1.8.8 ajm: floor=0 (type in tile entity); wall N=2, S=3, W=4, E=5)
    if base in ('skeleton_skull','wither_skeleton_skull','zombie_head','player_head','creeper_head','dragon_head'):
        return (144, 0)
    if base in ('skeleton_wall_skull','wither_skeleton_wall_skull','zombie_wall_head',
                'player_wall_head','creeper_wall_head','dragon_wall_head','piglin_wall_head'):
        return (144, full_index(props.get('facing','north')))
    if base == 'piglin_head':
        return (144, 0)
    # double plants (1.8.8 agi: lower = variant 0-5; upper = variant | 8)
    if base in ('sunflower','lilac','tall_grass','large_fern','rose_bush','peony','pitcher_plant'):
        variant = {'sunflower':0,'lilac':1,'tall_grass':2,'large_fern':3,'rose_bush':4,'peony':5,
                   'pitcher_plant':4}[base]
        if props.get('half') == 'upper': variant |= 8
        return (175, variant)
    # chests (1.8.8 afs: N=2, S=3, W=4, E=5; doubles detected from neighbors)
    if base == 'chest':
        return (54, full_index(props.get('facing','north')))
    if base == 'trapped_chest':
        return (146, full_index(props.get('facing','north')))
    if base == 'ender_chest':
        return (130, full_index(props.get('facing','north')))
    # ladder (1.8.8 ahr: N=2, S=3, W=4, E=5)
    if base == 'ladder':
        return (65, full_index(props.get('facing','north')))
    # torches (1.8.8 akf: floor=5; wall E=1, W=2, S=3, N=4)
    if base == 'torch' or base == 'soul_torch' or base == 'copper_torch':
        return (50, 5)
    if base == 'wall_torch' or base == 'soul_wall_torch' or base == 'copper_wall_torch':
        return (50, {'east':1,'west':2,'south':3,'north':4}[props.get('facing','north')])
    if base == 'redstone_torch' or base == 'redstone_wall_torch':
        bid = 76 if props.get('lit') in (None, 'true') else 75
        if base == 'redstone_torch':
            return (bid, 5)
        return (bid, {'east':1,'west':2,'south':3,'north':4}[props.get('facing','north')])
    if base == 'redstone_wire':
        return (55, min(15, int(props.get('power', 0))))
    if base == 'moss_carpet': return (171, 13)
    # fallback
    return (1, 0)

UNMAPPED = set()

def convert_block(sid, state_map):
    """state id -> (1.8.8 id, data). Unknowns -> stone + logged."""
    if sid in state_map:
        name, props = state_map[sid]
        return map_block(name, props)
    return (1, 0)

# ---------------- NBT writer ----------------
def nbt_write(out, tag_id, name, value, named=True, with_tag=True):
    if with_tag:
        out.append(struct.pack('>B', tag_id))
    if named:
        nb = name.encode()
        out.append(struct.pack('>H', len(nb))); out.append(nb)
    if tag_id == 1: out.append(struct.pack('>b', value))
    elif tag_id == 2: out.append(struct.pack('>h', value))
    elif tag_id == 3: out.append(struct.pack('>i', value))
    elif tag_id == 4: out.append(struct.pack('>q', value))
    elif tag_id == 5: out.append(struct.pack('>f', value))
    elif tag_id == 6: out.append(struct.pack('>d', value))
    elif tag_id == 7: out.append(struct.pack('>i', len(value))); out.append(value)
    elif tag_id == 8:
        nb = value.encode(); out.append(struct.pack('>H', len(nb))); out.append(nb)
    elif tag_id == 9:
        et, items = value
        out.append(struct.pack('>B', et)); out.append(struct.pack('>i', len(items)))
        for it in items:
            nbt_write(out, et, '', it, named=False, with_tag=False)
    elif tag_id == 10:
        for k, (t, v) in value.items():
            nbt_write(out, t, k, v)
        out.append(b'\x00')
    elif tag_id == 11:
        out.append(struct.pack('>i', len(value)))
        out.append(struct.pack('>%di' % len(value), *value))
    elif tag_id == 12:
        out.append(struct.pack('>i', len(value)))
        out.append(struct.pack('>%dq' % len(value), *value))

def nbt_blob(root_name, compound):
    out = []
    out.append(struct.pack('>B', 10))
    nb = root_name.encode(); out.append(struct.pack('>H', len(nb))); out.append(nb)
    for k, (t, v) in compound.items():
        nbt_write(out, t, k, v)
    out.append(b'\x00')
    return b''.join(out)

# ---------------- world writer ----------------
def build_section(blocks_flat, y_off):
    """blocks_flat: 4096 (id,data) for a 1.8.8 section; returns section dict or None if empty."""
    has = any(bid != 0 for bid, _ in blocks_flat)
    if not has:
        return None
    blocks = bytearray(4096); data = bytearray(2048)
    for i, (bid, d) in enumerate(blocks_flat):
        blocks[i] = bid & 0xFF
        if i % 2 == 0:
            data[i // 2] = (d & 0xF) << 4
        else:
            data[i // 2] |= d & 0xF
    # heightmap: top solid y in this section
    hm = [0] * 256
    for x in range(16):
        for z in range(16):
            for ly in range(15, -1, -1):
                if blocks[ly * 256 + z * 16 + x] != 0:
                    hm[z * 16 + x] = y_off + ly
                    break
    return {
        'Y': (1, y_off),
        'Blocks': (7, bytes(blocks)),
        'Data': (7, bytes(data)),
        'BlockLight': (7, bytes(2048)),
        'SkyLight': (7, b'\xff' * 2048),
        'HeightMap': (11, hm),
    }

def write_chunk_region(chunks, out_dir, state_map):
    """chunks: {(x,z): {'sections': [{'blocks': [4096 state ids], ...}]}}"""
    by_region = {}
    for (x, z), c in chunks.items():
        by_region.setdefault((x >> 5, z >> 5), {})[(x, z)] = c
    stats = Counter()
    for (rx, rz), cs in sorted(by_region.items()):
        entries = {}
        timestamps = {}
        blob_cache = {}
        for (x, z), c in sorted(cs.items()):
            sections = []
            # modern sections: i -> y = -64 + 16*i ; shift +64 -> 1.8.8 y = 16*i
            for i, s in enumerate(c['sections']):
                src_y = -64 + 16 * i
                dst_y = src_y + SHIFT
                if dst_y < 0 or dst_y >= MAX_Y:
                    continue
                flat = []
                for sid in s['blocks']:
                    bid, d = convert_block(sid, state_map)
                    if bid == 1 and state_map.get(sid, ('x',))[0] != 'minecraft:stone':
                        stats['substituted->stone'] += 1
                    flat.append((bid, d))
                sec = build_section(flat, dst_y // 16)
                if sec:
                    sections.append(sec)
            # full heightmap
            hm = [0] * 256
            top = -1
            for i, s in enumerate(c['sections']):
                src_y = -64 + 16 * i
                dst_y = src_y + SHIFT
                if dst_y >= MAX_Y:
                    continue
                for x_ in range(16):
                    for z_ in range(16):
                        for ly in range(15, -1, -1):
                            if s['blocks'][ly * 256 + z_ * 16 + x_] != 0:
                                y = dst_y + ly
                                if y > hm[z_ * 16 + x_]:
                                    hm[z_ * 16 + x_] = y
                                break
                if s['block_count'] > 0 and dst_y > top:
                    top = dst_y
            level = {
                'xPos': (3, x), 'zPos': (3, z),
                'LastUpdate': (4, 0), 'InhabitedTime': (4, 0),
                'TerrainPopulated': (1, 1), 'LightPopulated': (1, 1),
                'V': (1, 1), 'Status': (8, 'full'),
                'HeightMap': (11, hm),
                'Biomes': (7, bytes([1]) * 256),
                'Sections': (9, (10, sections)),
                'Entities': (9, (10, [])),
                'TileEntities': (9, (10, [])),
            }
            blob = zlib.compress(nbt_blob('', {'Level': (10, level)}), 6)
            blob = struct.pack('>I', len(blob) + 1) + b'\x02' + blob  # length + zlib type + payload
            blob_cache[(x, z)] = blob
            stats['chunks'] += 1
        # write region file
        os.makedirs(os.path.join(out_dir, 'region'), exist_ok=True)
        path = os.path.join(out_dir, 'region', f'r.{rx}.{rz}.mca')
        with open(path, 'wb') as f:
            f.write(b'\x00' * 8192)
            # first pass: assign sectors (data starts after 8KB locations + 8KB timestamps)
            sectors = {}
            used = 4
            for (x, z), blob in blob_cache.items():
                n = (len(blob) + 4095) // 4096
                sectors[(x, z)] = (used, n)
                used += n
            for (x, z), (off, n) in sectors.items():
                lx, lz = x & 31, z & 31
                f.seek((lx + lz * 32) * 4)
                f.write(struct.pack('>I', (off << 8) | n))
            for (x, z), blob in blob_cache.items():
                off, _ = sectors[(x, z)]
                f.seek(off * 4096)
                f.write(blob)
                f.seek(8192 + ((x & 31) + (z & 31) * 32) * 4)
                f.write(struct.pack('>I', 0))
            f.truncate(used * 4096)
        stats['regions'] += 1
    return stats

def write_level_dat(out_dir, spawn):
    level = {
        'LevelName': (8, 'world'),
        'generatorName': (8, 'flat'),
        'generatorOptions': (8, '2;7,2x3,2;1;'),
        'RandomSeed': (4, 0),
        'SpawnX': (3, spawn[0]), 'SpawnY': (3, spawn[1]), 'SpawnZ': (3, spawn[2]),
        'Time': (4, 0), 'DayTime': (4, 6000), 'LastPlayed': (4, 0), 'SizeOnDisk': (4, 0),
        'GameType': (3, 0), 'Hardcore': (1, 0), 'Difficulty': (1, 1),
        'allowCommands': (1, 1), 'raining': (1, 0), 'thundering': (1, 0),
        'rainTime': (3, 0), 'thunderTime': (3, 0), 'version': (3, 19133),
        'initialized': (1, 1), 'borderCenterX': (5, 0.0), 'borderCenterZ': (5, 0.0),
        'borderSize': (5, 5.9999968e7), 'borderSafeZone': (5, 5.0),
        'GameRules': (10, {'doFireTick': (8, 'true'), 'doMobSpawning': (8, 'true'),
                           'mobGriefing': (8, 'true'), 'doTileDrops': (8, 'true'),
                           'commandBlockOutput': (8, 'true'), 'naturalRegeneration': (8, 'true'),
                           'doDaylightCycle': (8, 'true'), 'logAdminCommands': (8, 'true')}),
        'DataVersion': (3, DATA_VERSION),
    }
    blob = gzip.compress(nbt_blob('', {'Data': (10, level)}), 6)
    with open(os.path.join(out_dir, 'level.dat'), 'wb') as f:
        f.write(blob)

# ---------------- main ----------------
def main():
    mcpr = sys.argv[1]
    out_dir = sys.argv[2]
    state_map = load_states()
    zf = zipfile.ZipFile(mcpr)
    f = zf.open('recording.tmcpr')
    state = 'LOGIN'
    chunks = {}
    dim_info = {}
    while True:
        hdr = f.read(8)
        if len(hdr) < 8: break
        ts, ln = struct.unpack('>ii', hdr)
        if ln < 1 or ln > 50_000_000: break
        body = f.read(ln)
        if len(body) < ln: break
        pid, i = read_varint(body, 0)
        if state == 'LOGIN':
            if pid == 0x02: state = 'CONFIG'
        elif state == 'CONFIG':
            if pid == 0x03: state = 'PLAY'
            elif pid == 0x07:
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
                    if key == 'minecraft:dimension_type':
                        dim_info = {k: v for k, v in entries}
                except Exception:
                    pass
        else:
            if pid == 0x74: state = 'CONFIG'; continue
            if pid == 0x2c:
                c = parse_chunk(body[i:])
                if c is not None:
                    chunks[(c['x'], c['z'])] = c
    f.close()
    print(f"{len(chunks)} chunks extracted")

    # verify dimension info
    for name, val in dim_info.items():
        if isinstance(val, dict):
            print(f"dimension {name}: min_y={val.get('min_y')} height={val.get('height')}")
    # spawn = centroid of chunks with structure content (sections beyond ground)
    struct_chunks = [c for c in chunks.values() if any(s['block_count'] > 0 for s in c['sections'][2:])]
    if struct_chunks:
        cx = sum(c['x'] for c in struct_chunks) // len(struct_chunks)
        cz = sum(c['z'] for c in struct_chunks) // len(struct_chunks)
    else:
        xs = [c['x'] for c in chunks.values()]; zs = [c['z'] for c in chunks.values()]
        cx = (min(xs) + max(xs)) // 2; cz = (min(zs) + max(zs)) // 2
    spawn = (cx * 16 + 8, 5, cz * 16 + 8)
    print(f"spawn at {spawn}")

    stats = write_chunk_region(chunks, out_dir, state_map)
    write_level_dat(out_dir, spawn)
    print(f"wrote {stats['chunks']} chunks in {stats['regions']} regions to {out_dir}")
    if stats.get('substituted->stone'):
        print(f"WARNING: {stats['substituted->stone']} blocks fell back to stone")

if __name__ == '__main__':
    main()
