"""
settings.json -- the one file you edit by hand to tune how the game plays.

Written next to the .exe on first run, complete with a built-in explanation of
every value. Hot-reloaded by mtime, so saving the file applies within a few
seconds; no restart needed.

Precedence: environment variable > settings.json > built-in default. (The env
vars stayed supported so older shortcuts/batch files keep working.)

Things the World Manager UI owns -- placed stops/gyms (places.json) and the live
event (events.json) -- deliberately live elsewhere, so hand-edits here are never
fighting the web UI.
"""
import json
import os
import time
import sys
import threading

import datadir

SETTINGS_FILE = datadir.path("settings.json")

# Every tunable, with the shape the file is written in. Keep the comments here in
# sync with the "_readme" block below -- that's what the user actually reads.
DEFAULTS = {
    "catching": {
        "xp_per_catch": 100,
        "candy_per_catch": 3,
        "stardust_per_catch": 100,
        "xp_nice_throw": 500,
        "xp_great_throw": 1000,
        "xp_excellent_throw": 2000,
        "xp_curveball": 10,
        "xp_new_species": 500,      # first catch/hatch of a species (2016 value)
        # Ditto hides as a common Pokemon (Nov 2016: Pidgey, Rattata, Zubat,
        # Magikarp); this share of those spawns turn out to be Ditto when caught.
        "ditto_chance": 0.02,
        "ditto_disguises": [16, 19, 41, 129],
        "fast_catch": False,        # true = one throw, one wobble, no escapes
        "base_catch_rate": 0.55,    # chance a Poke Ball holds a weak Pokemon
        "flee_chance": 0.12,        # chance it runs off after breaking out
        "razz_capture_mult": 1.8,   # how much a Razz Berry helps
        "razz_flee_mult": 0.4,      # ...and how much less likely it is to flee
        # How many times the ball wobbles before a Pokemon breaks out. Each row is
        # [miss_up_to, wobbles, miss_percent]: `miss` is how badly the throw failed
        # (0 = it only just broke free, 1 = it was never close) and the first row
        # whose miss_up_to is above it wins. wobbles is only a label -- the client
        # turns miss_percent into wobbles (< 0.15 = 3, < 0.5 = 2, < 0.75 = 1, else 0).
        # A catch always wobbles 3 times, so a break-out on the 3rd keeps it tense.
        "escape_wobble_bands": [[0.35, 3, 0.05], [0.70, 2, 0.3], [0.92, 1, 0.6]],
        # Ring size needed for each throw bonus, and how much spin counts as a
        # curveball. ZERO means "use the game master", which is what you want:
        # the client reads these same numbers out of ENCOUNTER_SETTINGS to decide
        # which banner to draw, so taking them from anywhere else lets it shout
        # "Excellent!" while the server quietly pays Great.
        # Stock 2016 values are spin 0.5, excellent 1.7, great 1.3, nice 1.0.
        # To make an Excellent genuinely harder, edit ExcellentThrowThreshold in
        # the game master so the client agrees; set a number here only if you
        # deliberately want the server to disagree with the banner.
        "reticle_nice": 0,
        "reticle_great": 0,
        "reticle_excellent": 0,
        "spin_bonus_threshold": 0,
        # ON: a bonus needs the ball to land IN the ring, not merely to hit the
        # Pokemon while the ring happened to be small.
        #
        # normalized_hit_position is a FLAG on this build, only ever 0.0 or 1.0:
        #     1.0 = the ball landed INSIDE the ring   -> bonus earned
        #     0.0 = it missed the circle              -> no bonus
        # so throw_accuracy_sense must stay center_is_one.
        #
        # Without this gate the tier comes from the ring size alone, so holding
        # for a tight ring and throwing wide pays "Excellent" for a ball that
        # never went near the circle -- confirmed in play, and all 8 Excellents
        # in the old logs were that (ring 1.70..1.88 with hitpos=0.0).
        "require_ball_in_circle": True,
        "throw_accuracy_sense": "center_is_zero",   # or "center_is_one"
        "throw_accuracy_tolerance": 0.5,
        # How much each ball helps, by item id: 1 Poke, 2 Great, 3 Ultra.
        # (4 = Master Ball, which always catches.)
        "ball_mult": {"1": 1.0, "2": 1.5, "3": 2.0},
    },
    "pokestops": {
        "per_l15_cell": 1,          # PokeStops per level-15 cell (~300m across)
        "max_per_request": 200,     # safety cap on one GetMapObjects batch
        # false (default) = PokeStops/Gyms sit at fixed geographic spots and stay
        # put as you pass them. true = also drop a trio (2 stops + a gym) right at
        # your feet -- handy standing still, but it re-spawns as you move, so DRIVING
        # spams stops/gyms down the whole road. Leave off unless you play stationary.
        "anchor_near_player": False,
        # true = use real OpenStreetMap places (data/osm_forts.json, made by
        # tools/fetch_osm_pois.py) as your PokeStops/Gyms -- on actual buildings,
        # off the roads. When that file has forts, the random procedural ones are
        # turned off. false (or no file) = fall back to procedural placement.
        "use_osm": True,
        # Real-map (OSM) density. A city centre in Europe tags every shop and
        # office -- up to ~230 named places per ~300m cell in Vienna -- and the
        # 2016 client freezes on the catch screen with ~500+ forts on the map.
        # At most this many forts per ~300m cell (0 = no limit), and never two
        # in the same ~70m cell like the real game. Parks/landmarks are kept first.
        "osm_max_per_cell": 10,
        "osm_one_per_l17": True,
        "xp_per_spin": 50,
        "cooldown_minutes": 5,
        "min_items_per_spin": 5,        # a spin never gives fewer items than this
        "max_items_per_spin": 8,
        # What a spin can drop. Each entry: chance (0-1) it drops at all, and
        # how many (min-max). The FIRST entry tops the haul up to
        # min_items_per_spin. Item names are listed in the _readme.
        # "chance" = each entry rolls on its own; "weighted" = every item in the
        # spin is picked separately, using the chances as odds (real-game style)
        "loot_mode": "chance",
        "loot": {
            "poke_ball":   {"chance": 1.0,  "min": 1, "max": 3},
            "potion":      {"chance": 1.0,  "min": 1, "max": 2},
            "revive":      {"chance": 1.0,  "min": 1, "max": 1},
            "great_ball":  {"chance": 0.30, "min": 1, "max": 2},
            "ultra_ball":  {"chance": 0.10, "min": 1, "max": 1},
            "razz_berry":  {"chance": 0.35, "min": 1, "max": 2},
        },
    },
    "gyms": {
        "chance_per_l15_cell": 0.2,    # procedural forts: ~1 gym per 5 stops
        # Real-map (OSM) forts: one gym for every this many PokeStops, decided per
        # ~300m cell with parks getting the gym first. Hot-reloads.
        "stops_per_gym": 5,
        # true = trainers pick their own team in game at level 5 (Mystic/Valor/
        # Instinct), the real 2016 way. false = everyone is auto-assigned the
        # "team" below and the choice screen never shows. Note: with this on, an
        # account that never chose (all existing ones) is asked once, next time
        # it's level 5+.
        "let_players_choose": True,
        "team": 1,                     # the auto-assigned team when choosing is off
        "defender_minutes": 1440,       # 24 hours
        # Damage a gym defender takes STICKS between attacks, so a strong gym can
        # be worn down over several fights. This is how long a defender takes to
        # heal back to full if it is left alone (0 = never heals).
        "defender_heal_minutes": 30,
        "defender_coins": 20,
        "max_defenders": 6,
        # Rival defenders for gyms nobody holds. Without them a gym is EMPTY,
        # and an empty gym cannot be battled at all -- the client only offers
        # to deploy into it, which is why gym battles did nothing on a fresh
        # server. 0 turns them off and restores the old behaviour.
        "npc_defenders": 2,
        "npc_min_cp": 300,
        "npc_max_cp": 1200,
        # How long a gym stays yours-for-the-taking after you beat the rivals,
        # before a new set moves in.
        "npc_respawn_minutes": 60,
        # The Shop's shield ("defender bonus"): what you collect per gym you're
        # holding, once every `cooldown_hours`, up to `max_gyms` gyms. The 2016
        # values were 10 coins + 500 stardust per gym, 10-gym cap, 21-hour timer.
        "defender_bonus_coins": 10,
        "defender_bonus_stardust": 500,
        "defender_bonus_max_gyms": 10,
        "defender_bonus_cooldown_hours": 21,
        # Prestige economy: how fast training raises a gym and attacking drains it.
        "prestige_gain_mult": 1.0,
        "prestige_loss_mult": 1.0,
        "battle_win_coins": 0,          # coins for winning a gym battle (0 = none)
    },
    "spawns": {
        "per_l15_cell": 20,         # wild Pokemon per level-15 cell (~300m across)
        "max_per_request": 120,     # HARD cap per map refresh. The phone has to
                                    # draw every one of these; ~180 was enough to
                                    # crash the 2016 client on a real device.
        "refresh_minutes": 15,
        "how_many_near_you": 5,
        "per_stop": 8,              # spawn points around each stop (~1 in 4 is up at a time)
        # Parks get this many times per_stop, spread over the park (15-120m).
        "park_spawn_multiplier": 3,
        "nearest_distance_m": 25,
        "farthest_distance_m": 65,
        # Cells further than this from you get no wild Pokemon (0 = no limit).
        # Cuts the map payload hard, which is what a VPN on cellular needs.
        "radius_m": 300,
        # The Sightings / nearby panel lists wild Pokemon within this distance of
        # you, each at its real distance (the 2016 tracker covered ~200m).
        "sightings_radius_m": 200,
        "min_cp": 100,
        "max_cp": 1200,
        "allow_legendaries": False,
        # true = spawn exactly like 2016: real per-species rates (Pidgey ~16% ...
        # Lapras 0.006%, no wild Ditto/legendaries), regionals on their own
        # continent, and wild levels up to your trainer level (max 30) with the
        # real CP formula. false = the older rarity tiers + flat min/max CP.
        "realistic_2016": True,
        # 2016 spawn timing: each spawn point appears at its own fixed minute every
        # hour and lasts 15 minutes, instead of the whole map re-rolling at once.
        # (Needs realistic_2016.)
        "hourly_spawn_points": True,
        # Rare pockets: a few regions in every area are "good spots" where rare and
        # very rare Pokemon show up far more often -- somewhere worth walking to.
        "rare_pocket_chance": 0.07,     # share of regions that are a pocket
        "rare_pocket_boost": 5,         # how much likelier the scarce tiers are there
        # Spawn rarity tiers: the RELATIVE weight of each tier. A species' odds of
        # being the wild Pokemon rolled = its tier weight / the sum of every
        # species' weight, so higher = more common. Lower the rare tiers to make
        # the trophies genuinely scarce. "very_rare" is the pseudo-legendary /
        # chase list (Dratini line, Lapras, Snorlax, Chansey, the fossils, ...);
        # "rare" is starters and the strong single-stage Pokemon. Hot-reloads.
        "weight_common": 60,
        "weight_uncommon": 12,
        "weight_rare": 3,
        "weight_very_rare": 1,
        # Wild CP can never exceed what a species can actually reach (a level-40,
        # perfect-IV specimen). Without this a Caterpie could roll 1200 CP. Events
        # that WANT beyond-natural Pokemon (High CP, Legendary Hunt) opt out per
        # event, so this only tames ordinary wild spawns.
        "cap_cp_to_species": True,
        # Different areas favour different Pokemon, the way the 2016 biomes did:
        # water types by water, rock/ground on hills, city trash downtown, etc.
        # Each region keeps its biome, so a place always feels the same.
        "biomes": True,
        # How big a biome region is, as an S2 cell level. Lower = bigger regions.
        # 12 is ~3 km across; 13 ~1.5 km, 11 ~6 km.
        "biome_size": 12,
        # Nests: like the real game, parks spawn mostly ONE species, and which
        # species rotates on a cycle so nests are worth re-checking.
        "nests": True,
        # Share of parks that are a nest in any one cycle.
        "park_nest_share": 0.67,
        # Chance a spawn in a nesting park is the nest species (the rest are
        # normal biome spawns, so a nest is a strong bias, not the only thing).
        "nest_chance": 0.6,
        # The old model: ~1 in 8 whole ~3 km regions is a nest. Off -- nests are
        # in parks now.
        "region_nests": False,
        # How often nests rotate to a new species, in days (2016 was ~14).
        "nest_rotation_days": 14,
        # Which species nests pick from (Pokedex numbers). Empty = the classic
        # 2016 nest list (starters, Pikachu, Eevee, Dratini, Magikarp, ...).
        "nest_species": [],
        # How far the Hub's nest list looks for nesting parks, in metres.
        "nest_list_radius_m": 3000,
        # Day/night: nocturnal Pokemon (Zubat, ghosts, ...) favour the dark and
        # day types favour daylight, using local time at your longitude.
        "day_night": True,
    },
    "eggs": {
        "max_eggs": 9,              # egg bag size
        "hatch_xp": 200,            # XP for hatching any egg (2016 value)
        "drop_chance": 0.35,        # chance a PokeStop spin gives an egg
        # How often each egg tier drops (relative weights), and what can hatch from
        # each. Empty species list = the built-in pool for that tier.
        "tier_weights": {"2": 60, "5": 30, "10": 10},
        "species_2km": [],
        "species_5km": [],
        "species_10km": [],
        "iv_floor": 10,             # a hatchling's Attack/Defense/Stamina IVs are each at least this (0-15)
        "min_step_m": 1.0,          # GPS moves smaller than this are jitter
        # Bigger than this between two GPS fixes is treated as a teleport and does
        # NOT count towards eggs. 120 m was too low: DRIVING moves further than that
        # between fixes, so no distance counted and eggs never hatched. 1000 m counts
        # driving while still ignoring a real GPS glitch across town. Lower it to
        # 120 if you want the strict "walk only" behaviour.
        "max_step_m": 1000.0,
    },
    # anticheat.py: a speed lock for everyone but spoofers, who are MARKED instead.
    "anticheat": {
        "enabled": True,
        # the speed lock: above these, spins fail, catches break free and walked
        # distance (eggs, km) doesn't count. 0 = no limit. Spoofers are exempt (marked).
        "spin_max_mph": 35,
        "catch_max_mph": 35,
        "walk_max_mph": 15,
    },
    # The launcher a player needs for the public server. 0 = any launcher. Above the
    # tunnel's LAUNCHER_BUILD (tunnel.py), older launchers are told to update, with a
    # link to update_url (new launchers turn Play into an Update button that opens it).
    "launcher": {
        "min_build": 0,
        "update_url": "https://windstock.org/downloads.html",
    },
    "boosts": {
        "lucky_egg_minutes": 30,    # double XP while it burns
        "incense_minutes": 30,      # extra wild Pokemon around you
        "incense_extra_spawns": 6,  # (unused since 2026-09-25: see incense_spawn_seconds)
        "incense_spawn_seconds": 30,       # a burning Incense brings one Pokemon this often
        "incense_spawn_life_seconds": 30,  # ...and each one flees this long after it appears
        "incense_spawn_distance_m": 5,     # ...within this many metres of the trainer
        "incense_walk_m": 25,              # ...once you have walked this far since the
                                           # last one (0 = no walking needed)
        "incense_still_seconds": 60,       # ...or, standing still, one this often
                                           # (0 = walking only)
        "lure_minutes": 30,         # a Lure on a PokeStop
        "lure_extra_spawns": 4,     # extra Pokemon around a lured stop
        "lure_pokemon_seconds": 90, # the Pokemon sitting ON a lured stop changes this often
        # true = luring an already-lured stop ADDS its time instead of being refused,
        # so a group can keep one stop lit all day ("lure party").
        "lure_stacks": True,
        "lure_max_minutes": 180,    # ...up to this much banked time
    },
    "pokemon": {
        # true = power-ups follow the real 2016 curve: each one raises the Pokemon
        # one half-level (the genuine CP multiplier step) and costs the real,
        # escalating Stardust/Candy the client shows -- gated by your trainer level.
        # false = the simple flat model below (fixed cost, fixed % CP gain).
        "real_powerups": True,
        "powerup_candy": 1,
        "powerup_stardust": 200,
        "powerup_cp_gain": 12,
        "evolve_xp": 500,
        "transfer_candy": 1,
        "faint_after_gym": True,
        # Field/Special Research reward Pokemon: Attack/Defense/Stamina IVs are each
        # at least this (0-15), like the real game's 10/10/10 research floor.
        "research_iv_floor": 10,
    },
    "battles": {
        "attack_damage": 12,
        "special_damage": 30,
        "defender_damage": 8,
        # 2016 XP: per defender you beat -- 50 training your own team's gym, 100
        # attacking someone else's -- plus 150 for taking the whole gym down.
        "train_xp": 50,
        "battle_xp": 100,
        "gym_win_xp": 150,
        "raid_xp": 1000,            # beating a raid boss (raids weren't in 2016)
        # This client (measured) only enters combat for TRAINING-type gym battles;
        # a NORMAL (attack an enemy gym) battle opens then freezes -- you can't
        # attack. true = report enemy battles as TRAINING so the client will fight
        # (you still take the gym on victory). Turn off if enemy battles ever start
        # working natively.
        "attack_as_training": True,
    },
    "raids": {
        "boss_hp_multiplier": 10.0,
        "boss_damage_multiplier": 0.15,
        "respawn_minutes": 5.0,
        "min_damage_percent": 3.0,
        "catch_minutes": 10.0,
        "raid_seconds": 180.0,
        "countdown_seconds": 10.0,
    },
    "shiny": {
        "enabled": True,
        "rate": 0.0022,             # ~1 in 450, the real game's base shiny rate
        "charm_multiplier": 2.0,    # Shiny Charm: permanent, bought once
        "incense_multiplier": 10.0, # Shiny Incense: while it burns
        "incense_minutes": 30.0,
        "incense_spawn_rate": 0.1,  # Shiny Incense: chance each Pokemon IT brings is shiny
    },
    "trading": {
        "enabled": True,
        "daily_limit": 10,          # trades per trainer per day (UTC)
        "min_level": 5,             # both trainers must be at least this level
        "cost": 100,                # stardust each side pays for an ordinary trade
        "special_cost": 20000,      # ... when either Pokemon is shiny or legendary
        "candy": 3,                 # candy each side gets for the species they sent
    },
    "shop": {
        "price_multiplier": 1.0,    # 0.5 = half price, 2.0 = double
        # true once the Android APK has been run through tools/patch_badges.py: its
        # in-game store then has art + names for the Shiny Charm / Shiny Incense too
        # (iOS always does -- the launcher patches its copy)
        "patched_android": False,
    },
    "daily": {
        "enabled": True,
        "catch_xp": 500,
        "catch_dust": 500,
        "catch7_xp": 2000,
        "catch7_dust": 2000,
        "catch7_items": {"2": 5},
        "spin_xp": 500,
        "spin_dust": 500,
        "spin7_xp": 2000,
        "spin7_dust": 2000,
        "spin7_items": {"2": 5, "101": 3},
    },
    "halloween": {
        # The look patched clients (iOS tweak v157+) put on while an event whose
        # theme is "halloween" runs (the Halloween preset; events.py). Nothing
        # here does anything outside such an event.
        "night_all_day": True,      # the map stays in its night look, and the
                                    # night music plays, whatever the clock says
        "fog": True,                # coloured haze over the map and in catches
        "fog_color": "#A6501A",     # dim pumpkin orange
        # how close the haze starts / where it is solid, in map units (the camera
        # sits ~49 from your trainer). Smaller = thicker.
        "fog_start": 55.0,
        "fog_end": 500.0,
        # the haze climbs past the horizon and fades out (iOS tweak v160+): how many
        # degrees up it reaches (25 at most -- the sky picture wraps above that).
        "fog_sky": True,
        "fog_sky_height": 20.0,
        "tint": True,               # recolour the map's night light
        "tint_color": "#7A3CFF",    # violet; brightness stays the game's own
        "tint_amount": 0.6,         # 0 = the game's night light, 1 = all tint
    },
    "weather": {
        "enabled": True,
        "spawn_boost": 3,
        "cp_boost_percent": 20.0,
        "stardust_bonus_percent": 25.0,
        "windy_kmh": 24.0,
        "force": "",
        # true = the weather's types spawn more, with more CP and stardust;
        # false = looks only. (Was "gameplay", off for the v136-v142 tests.)
        "boosts": True,
        # The tweak's 3D rain (v141), sent to the phone with /weather. It rains around
        # the Pokemon in a catch and around you on the map; sizes are SHARES of the
        # camera's distance to that anchor, so both scenes look alike. debug = the
        # camera/anchor numbers on screen. ("rain3d"/"rain" were older, other units.)
        "rainfx": {"hud": False, "count": 500, "area": 1.2, "fall": 1.0,
                   "length": 0.06, "width": 0.001, "speed": 0.9, "near": 0.25,
                   "ahead": 0.3, "wind": 0.18, "gust": 0.12, "alpha": 0.45,
                   "ground_offset": 0, "ripples": 80, "ripple_chance": 0.35,
                   "ripple_size": 0.018, "ripple_life": 0.7, "ripple_alpha": 0.5,
                   "ripple_width": 0.001,
                   # v143: snow (weather "snow") and gusts (weather "windy")
                   "snow_speed": 0.12, "snow_size": 0.006, "snow_alpha": 0.85,
                   "wind_speed": 1.6, "wind_length": 0.15, "wind_alpha": 0.22,
                   # v145: fog sheets behind the Pokemon in catches (hide the backdrop)
                   "mist_fog": 1.0, "mist_rain": 0.35, "mist_snow": 0.45,
                   "mist_cloudy": 0.0,
                   # v148: how much each weather dims the scene by day (0..0.85);
                   # nights get shade_night of it. fog_distance scales every
                   # weather's fog (0.5 = twice as close)
                   "shade_rainy": 0.38, "shade_snow": 0.16, "shade_fog": 0.22,
                   "shade_cloudy": 0.24, "shade_windy": 0.10,
                   "shade_partly_cloudy": 0.08, "shade_night": 0.25,
                   "shade_distance": 0.3, "fog_distance": 1.0},
    },
    "radar": {
        # Seconds you must wait between radar sweeps -- the Help Center's radar,
        # the Radar tab in game and the desktop /radar page all share it, so a
        # sweep in one counts in the others. 0 = no cooldown, sweep as often as
        # you like. The genuine feel was 60; 20 keeps sweeps snappy without
        # seeding spawns for the circle around you on every tap.
        "cooldown_seconds": 20,
        # How far a sweep reaches, in metres.
        "range_m": 500.0,
    },
    "distances": {
        # How far you can reach, in metres. These ship to the client inside
        # GlobalSettings; the client enforces them, the server never checks your
        # position. Defaults are the genuine 2016 values.
        "fort_interaction_m": 40.25,   # spinning a PokeStop / touching a Gym
        "encounter_m": 50.25,          # tapping a wild Pokemon to start a catch
        "pokemon_visible_m": 70.0,     # how far wild Pokemon are DRAWN
        # Raising pokemon_visible_m alone shows nothing new unless Pokemon
        # actually spawn out there -- see spawns.farthest_distance_m.
    },
    "map": {
        # The Google Maps key the client uses to fetch the map's ROADS. It is
        # handed over in GlobalSettings.map_settings.google_maps_api_key, and
        # it was hardcoded to "" for years -- so the client asked Google for
        # tiles with no key, got refused, and drew flat green ground with no
        # roads, water or buildings. Nothing else in the game depends on it.
        # Niantic's own 2016 key is long dead; put a working one here if you
        # have one. Blank keeps the old behaviour.
        "google_maps_api_key": "",
    },
    "progression": {
        # true = brand-new trainers run the real 2016 onboarding (legal screen,
        # avatar, name pick, first starter catch) instead of dropping straight onto
        # the map. Only affects NEW accounts; existing saves are never dragged back
        # through it. Needs a client whose login/first-run flow is intact. Off by
        # default -- the straight-to-map behaviour is the safe, proven one.
        "run_tutorial": False,
        # true = skip the slow SCRIPTED starter catch in onboarding (the drawn-out
        # "Pokemon sits in front of you" tutorial encounter) while keeping the rest
        # of the flow (legal screen, avatar, name, team). Your first catch is then a
        # normal, fast wild encounter instead. Only matters when run_tutorial is on.
        "tutorial_skip_starter": True,
        # Trainer level cap. 40 is the real 2016 ceiling; raise it for a longer
        # grind (500 is supported). Levels 1-40 always use the genuine 2016 XP
        # table -- anything above is generated, see world.LEVEL_XP.
        "max_level": 40,
        # Shape of the generated curve past 40:
        #   xp(L) = xp(40) + step*(L-40) + accel*(L-40)^2
        # The defaults land level 500 at ~1.998 billion XP, just under the
        # 2,147,483,647 int32 ceiling -- past that the client's level ring can
        # wrap into nonsense, so raising these is not free.
        "level_xp_step": 2000000,
        "level_xp_accel": 5000,
    },
    "storage": {
        "pokemon_upgrade_step": 50,
        "pokemon_upgrade_cost": 200,
        "max_pokemon_limit": 1000,
        "items_upgrade_step": 50,
        "items_upgrade_cost": 200,
        "max_items_limit": 1000,
    },
    "avatar": {
        # How your trainer looks. The client picks the clothing art by INDEX --
        # each number chooses one of the outfits baked into the 2016 APK, so the
        # useful range is small (0-4 for most slots) and an index the client has
        # no art for simply falls back to the first one.
        "choose_in_game": False,    # true = the dress-up screen opens at your
                                    # next login and whatever you pick there is
                                    # saved; false = use the numbers below
        "gender": 1,                # 1 = male, 2 = female
        "skin": 1,
        "hair": 1,
        "shirt": 1,
        "pants": 1,
        "hat": 0,                   # 0 = no hat
        "shoes": 1,
        "eyes": 1,
        "backpack": 1,
    },
    "names": {
        # Colored names. Anything you name in game -- your trainer name at the
        # name screen, a Pokemon's nickname -- may carry short tags: [red]Ash,
        # [gold]Ash[/]ketchum, [f80]Ash, [rainbow]Ash (see colortags.py). These
        # colour the names that DON'T carry tags of their own. '' = plain.
        "trainer_color": "",        # e.g. "gold", "ff8800", "rainbow"
        "stop_color": "",           # every PokeStop name
        "gym_color": "",            # every Gym name; "team" = the holding team's
    },
    "server": {
        "world_manager_port": 8080,
        # The online door (online.py): one public port players reach BY IP from
        # the launchers' "Windstock Online" mode -- no DNS, no certificate to
        # install. TLS only (the launcher's tunnel). Open/forward it on your
        # router or VPS firewall. 0 = off. Takes effect the NEXT time the server
        # is started.
        "online_port": 8888,
        # The most trainer accounts this server will hold. Every account is a
        # save file, and signing up is free, so without a ceiling a stranger
        # could fill the disk. 0 = no limit.
        "max_accounts": 5000,
        # false = invite-only: nobody can make a new trainer over the network,
        # only the ones you create in the World Manager ("New beta tester") get
        # in. Trainers that already have a password are not affected, and the
        # server's own machine (the phone's built-in server) can always sign up.
        "open_signups": True,
        # Player-added PokeStop photos: the most disk they may use in total
        # (MB), and the most places one trainer may add in all.
        "photos_max_mb": 300,
        "nominations_per_trainer": 100,
        # EXPERIMENT: serve our branded HTML page at the real PTC login URL
        # (/sso/login). If the client's PTC button opens a WEBVIEW, this replaces
        # the login screen with our own (logo, text, email+password, no Google).
        # If the client's login is NATIVE, this BREAKS login -> flip it back to
        # false. Default false = the proven JSON login. Test on a throwaway account.
        "custom_login_page": False,
        # How the server shows itself when you start the exe:
        #   "bracky" = the Bracky window (status, activity, buttons)
        #   "console"   = the old plain black console window, raw text only
        # Takes effect the NEXT time the server is started.
        "window": "bracky",
    },
}

_README = [
    "==================== POKEMON GO SERVER SETTINGS ====================",
    "Edit the numbers below, save the file, and they apply within a few",
    "seconds. You do NOT need to restart the server.",
    "",
    "To reset everything: delete this file. It will be recreated.",
    "",
    "catching:",
    "   fast_catch ........... true = a catch takes one throw instead of three",
    "                          or four. The Pokemon never breaks out and never",
    "                          runs, and the ball is reported as a sure thing,",
    "                          which also cuts the wobbling short. Most of the",
    "                          time a slow catch takes is repeat throws.",
    "   xp_per_catch ......... XP for catching anything at all",
    "   xp_nice_throw ........ bonus XP for a Nice throw",
    "   xp_great_throw ....... bonus XP for a Great throw",
    "   xp_excellent_throw ... bonus XP for an Excellent throw",
    "   xp_curveball ......... small extra bonus for a curveball",
    "   xp_new_species ....... bonus XP the first time you catch, hatch or",
    "                          evolve into a species (a new Pokedex entry)",
    "   ditto_chance ......... share of disguise spawns that are secretly",
    "                          Ditto (0.02 = 1 in 50; 0 = no Ditto)",
    "   ditto_disguises ...... what Ditto hides as (Pokedex numbers)",
    "",
    "pokestops:",
    "   xp_per_spin .......... XP each time you spin a stop",
    "   cooldown_minutes ..... how long a stop stays purple before reuse",
    "   min/max_items_per_spin  how many items a spin gives",
    "   loot ................. the drop table: \"item\": {chance, min, max}.",
    "                          chance 1.0 = always, 0 = never. Add, remove or",
    "                          reorder entries freely; the first one is used to",
    "                          top a spin up to min_items_per_spin. Items:",
    "                          poke_ball great_ball ultra_ball master_ball",
    "                          potion super_potion hyper_potion max_potion",
    "                          revive max_revive lucky_egg incense lure",
    "                          razz_berry  (or a raw item id number as the name)",
    "                          Also editable in the World Manager website.",
    "   loot_mode ............ \"chance\" = each loot entry rolls on its own, as",
    "                          above. \"weighted\" = a spin gives min-max items and",
    "                          EACH one is picked using the chances as odds (like",
    "                          the real game: 0.56 poke_ball = 56% of items);",
    "                          per-entry min/max are ignored in this mode.",
    "   anchor_near_player ... false = stops/gyms stay at fixed spots (best for",
    "                          driving). true = also drops a trio at your feet that",
    "                          follows you -- spams the road when you move.",
    "   osm_max_per_cell ..... most real-map stops/gyms per ~300m cell (10;",
    "                          0 = all). Dense cities froze the client above ~500.",
    "   osm_one_per_l17 ...... true = never two real-map stops within one ~70m",
    "                          cell, like the real game",
    "",
    "gyms:",
    "   team ................. your team: 1=Blue, 2=Red, 3=Yellow",
    "   defender_minutes ..... how long a Pokemon guards before coming home",
    "   defender_heal_minutes  battle damage on a defender STICKS between",
    "                          attacks; this is how long it takes to heal back",
    "                          to full when left alone (0 = it never heals).",
    "   defender_coins ....... PokeCoins paid when it returns",
    "   max_defenders ........ how many Pokemon fit in one gym",
    "   defender_bonus_coins/stardust  Shop shield payout per gym held",
    "   defender_bonus_max_gyms ...... cap on gyms the shield pays for",
    "   defender_bonus_cooldown_hours  how often the shield can be collected",
    "",
    "spawns:",
    "   refresh_minutes ...... how often all wild Pokemon change",
    "   how_many_near_you .... wild Pokemon around you (0-60)",
    "   nearest/farthest_distance_m   how spread out they are, in metres",
    "   min_cp / max_cp ...... how strong wild Pokemon are",
    "   cap_cp_to_species .... true = a wild Pokemon is never stronger than that",
    "                          species can really get (no 1200 CP Caterpie)",
    "   biomes ............... true = different areas favour different Pokemon",
    "                          (water by water, rock on hills, city trash downtown)",
    "   biome_size ........... how large each biome region is (S2 cell level; 12",
    "                          is ~3 km, lower = bigger regions)",
    "   per_stop ............. spawn points around each PokeStop",
    "   park_spawn_multiplier  parks get this many times more spawn points",
    "   nests ................ true = parks spawn mostly one species, rotating",
    "                          on a cycle (like the real game's nests)",
    "   park_nest_share ...... 0..1 share of parks that are nests each cycle",
    "   nest_chance .......... 0..1 chance a spawn in a nest is the nest species",
    "   region_nests ......... true = also the old whole-region (~3 km) nests",
    "   nest_rotation_days ... how often a nest rotates species (2016 was ~14);",
    "                          migrations land on Thursdays 00:00 UTC for 7 or 14",
    "   nest_species ......... Pokedex numbers nests pick from ([] = 2016 list)",
    "   nest_list_radius_m ... how far the Hub's NESTS list looks (metres)",
    "   day_night ............ true = nocturnal Pokemon favour night, day types",
    "                          favour daylight (local time at your longitude)",
    "",
    " catching",
    "   base_catch_rate ...... Pokemon can now BREAK OUT. Raise towards 1.0 to",
    "                          make catching easier, lower it to make it a fight.",
    "                          Strong Pokemon resist more; Great/Ultra Balls,",
    "                          good throws and Razz Berries all help.",
    "   flee_chance .......... chance it runs away after breaking out",
    "   reticle_nice/great/excellent, spin_bonus_threshold",
    "                          0 = take them from the game master, which is what",
    "                          the client itself reads. Set one only to make the",
    "                          server disagree with the banner on purpose; to",
    "                          really make Excellent harder, edit the game",
    "                          master's ExcellentThrowThreshold instead.",
    "   require_ball_in_circle  ON. A bonus needs the ball to land IN the ring,",
    "                          not just to hit the Pokemon while the ring was",
    "                          small. This client sends hit_position as 0.0 (in",
    "                          the ring) or 1.0 (clipped it outside), so leave",
    "                          throw_accuracy_sense on center_is_zero. Off = edge",
    "                          clips score Nice/Great again.",
    "",
    " eggs",
    "   hatch_xp ............. XP for hatching an egg (any distance)",
    "   drop_chance .......... how often a PokeStop hands you an egg (0-1)",
    "   max_step_m ........... distance only counts if you really walked it --",
    "                          a GPS jump bigger than this is ignored, so you",
    "                          cannot hatch an egg by teleporting",
    "   per_l15_cell ......... wild Pokemon in each level-15 cell (~300m across).",
    "                          The client asks for several cells at once, so this",
    "                          multiplies -- lower it if the map gets sluggish.",
    "   max_per_request ...... hard cap per map refresh, so a big per_l17_cell",
    "                          cannot produce a batch the client chokes on",
    "   radius_m ............. cells further away than this get no wild Pokemon",
    "                          (0 = no limit). The cell you stand in is always",
    "                          filled. Lower it if the map fails to load over a",
    "                          VPN or on cellular; raise it to see more around you",
    "   allow_legendaries .... true = Mewtwo etc. can appear in the wild",
    "",
    "pokemon:  (powering up, evolving, transferring)",
    "   real_powerups ........ true = the real 2016 curve (one half-level per",
    "                          power-up, genuine escalating Stardust/Candy the game",
    "                          shows, capped by your trainer level). false = the",
    "                          simple flat model below.",
    "   powerup_candy / powerup_stardust   flat-model cost of one power-up",
    "   powerup_cp_gain ...... flat-model % CP added per power-up",
    "   evolve_xp ............ XP for evolving one Pokemon",
    "   transfer_candy ....... candy you get for transferring",
    "   faint_after_gym ...... true = gym defenders come home with 0 HP",
    "",
    "battles:  (attacking a Gym)",
    "   attack_damage ........ damage your normal tap does",
    "   special_damage ....... damage your charged move does",
    "   defender_damage ...... damage the gym Pokemon does back to you",
    "   train_xp ............. XP per defender beaten in your own team's Gym",
    "   battle_xp ............ XP per defender beaten in someone else's Gym",
    "   gym_win_xp ........... extra XP for taking down a whole Gym",
    "   raid_xp .............. XP for beating a raid boss",
    "",
    "raids:  (Raid mode in the World Manager -- multiplayer bosses)",
    "   boss_hp_multiplier ... the boss has this many times its normal HP, in ONE",
    "                          pool shared by every trainer fighting it",
    "   boss_damage_multiplier  the boss's counter-attacks do this fraction of their",
    "                          normal damage (1.0 = full; a CP 9999 boss one-shots",
    "                          most Pokemon at full strength)",
    "   respawn_minutes ...... after it falls, the gym's boss comes back at full HP",
    "                          this many minutes later",
    "   min_damage_percent ... share of the boss's HP a trainer must deal to get",
    "                          the catch (stops a single last-second tap counting)",
    "   catch_minutes ........ how long the dropped boss stays catchable",
    "   raid_seconds ......... raid timer shown on the raid screen; when it runs out",
    "                          the raid fails and the boss heals to full",
    "   countdown_seconds .... lobby countdown after someone presses Start",
    "",
    "shiny:  (shiny Pokemon -- only species with a shiny model, see make_shiny_bundle.py)",
    "   enabled .............. false = no shinies",
    "   rate ................. chance a spawn is shiny (0.0022 = ~1 in 450). Decided ONCE",
    "                          per spawn, so re-tapping or re-finding it never rerolls",
    "   charm_multiplier ..... Shiny Charm (shop, bought once, permanent) scales the rate",
    "   incense_multiplier ... Shiny Incense (shop) scales the rate while it burns; it",
    "                          stacks with the charm, and only affects spawns you meet",
    "                          while it is running",
    "   incense_minutes ...... how long a Shiny Incense lasts",
    "   incense_spawn_rate ... a Shiny Incense also brings Pokemon like an Incense does",
    "                          (boosts.incense_spawn_seconds); each one it brings is shiny",
    "                          this often (0.1 = 1 in 10)",
    "",
    "trading:  (Pokemon trades between trainers, made in Discord through Windbot)",
    "   enabled .............. false = no trades",
    "   daily_limit .......... trades each trainer may make per day (UTC)",
    "   min_level ............ both trainers need at least this level",
    "   cost ................. stardust EACH trainer pays for an ordinary trade",
    "   special_cost ......... ... when either Pokemon is shiny or legendary",
    "   candy ................ candy each trainer gets for the species they sent",
    "",
    "shop:",
    "   price_multiplier ..... scales every price in the in-game shop",
    "   patched_android ...... true if the Android APK went through tools/patch_badges.py:",
    "                          sell the Shiny Charm / Shiny Incense in its in-game shop too",
    "",
    "daily:  (daily catch/spin streaks, like the real game's daily bonus)",
    "   enabled .............. false = no daily bonuses at all",
    "   catch_xp / catch_dust  first catch of the day",
    "   catch7_xp / catch7_dust / catch7_items   ...and every 7th day in a row",
    "   spin_xp / spin_dust .. first PokeStop spin of the day",
    "   spin7_xp / spin7_dust / spin7_items      ...and every 7th day in a row",
    "   the *_items are {\"item id\": count} -- 2 = Great Ball, 101 = Potion",
    "",
    "halloween:  (the map's look during a Halloween-themed event; iOS tweak v157+)",
    "   night_all_day ........ true = night map and night music at any hour",
    "   fog / fog_color ...... coloured haze (\"#RRGGBB\"); fog_start / fog_end =",
    "                          where it begins / is solid (smaller = thicker)",
    "   fog_sky .............. the haze rises into the sky and fades out (v160+);",
    "                          fog_sky_height = degrees above the horizon (2-25)",
    "   tint / tint_color .... recolour the night light; tint_amount 0..1",
    "",
    "weather:  (real weather from Open-Meteo -- the only feature that uses the internet)",
    "   enabled .............. false = no weather, nothing is ever sent out",
    "   spawn_boost .......... Pokemon of the weather's types spawn this many times",
    "                          as often (very rare Pokemon are never boosted)",
    "   cp_boost_percent ..... boosted wild Pokemon get this much more CP (still",
    "                          capped at what the species can reach)",
    "   stardust_bonus_percent  extra stardust for catching a boosted Pokemon",
    "   windy_kmh ............ wind speed that turns dry weather into 'Windy'",
    "   force ................ '' = real weather; or sunny, clear, partly_cloudy,",
    "                          cloudy, rainy, snow, fog, windy to fix it (testing)",
    "   boosts ............... true = the weather's types spawn more, with more CP",
    "                          and stardust; false = looks only",
    "   rainfx ............... the 3D rain, around the Pokemon (catch) or you (map).",
    "                          Sizes are shares of the camera's distance to it:",
    "                          area, fall, length, width, speed, near, ahead,",
    "                          ripple_size, ripple_width; also count, wind, gust,",
    "                          alpha, ground_offset, ripples, ripple_chance,",
    "                          ripple_life, ripple_alpha; hud = camera numbers on screen;",
    "                          snow_speed, snow_size, snow_alpha (snow), wind_speed,",
    "                          wind_length, wind_alpha (windy); mist_fog, mist_rain,",
    "                          mist_snow, mist_cloudy = how much the catch screen's",
    "                          fog sheets hide the backdrop (0..1); shade_rainy,",
    "                          shade_snow, shade_fog, shade_cloudy, shade_windy,",
    "                          shade_partly_cloudy = how dark the scene gets by day",
    "                          (0..0.85), shade_night = share of that after dark;",
    "                          fog_distance = scales the fog (0.5 = twice as close)",
    "",
    "avatar:  (what your trainer wears)",
    "   choose_in_game ....... true = the game's own dress-up screen opens the",
    "                          next time you log in, and your choice is saved",
    "   gender ............... 1 = male, 2 = female",
    "   skin/hair/shirt/pants/hat/shoes/eyes/backpack",
    "                          which outfit piece to wear, by number. Most slots",
    "                          only have a handful (try 0-4); hat 0 = bare head.",
    "",
    "names:  (colored names)",
    "   Type colour tags into any name in game: [red]Ash, [gold]Ash[/]ketchum,",
    "   [f80]Ash (hex), [rainbow]Ash. Colours: red orange yellow gold green lime",
    "   teal cyan blue navy purple pink magenta brown white gray black mystic",
    "   valor instinct. Only the letters count toward the name length limits.",
    "   trainer_color ........ colour for your trainer name when it has no tags",
    "   stop_color ........... colour for every PokeStop name ('' = plain)",
    "   gym_color ............ colour for every Gym name; 'team' = the colour",
    "                          of the team holding it",
    "",
    "storage:  (bought from the World Manager with gym PokeCoins)",
    "   pokemon_upgrade_step/cost  space added per purchase, and its price",
    "   items_upgrade_step/cost    same for the item bag",
    "   max_pokemon_limit / max_items_limit   the ceiling you can buy up to",
    "",
    "server:",
    "   world_manager_port ... the http://127.0.0.1:PORT control panel",
    "   online_port .......... public port for the launchers' Windstock Online",
    "                          mode (players type IP:PORT); 0 = off; next start",
    "   max_accounts ......... most trainer accounts the server will hold (0 = no limit)",
    "   open_signups ......... false = invite-only: new trainers only from the",
    "                          World Manager's 'New beta tester' button",
    "   photos_max_mb ........ disk the player-added stop photos may use in total",
    "   nominations_per_trainer  most places one trainer may add in all",
    "   window ............... 'bracky' = the server window, 'console' = the",
    "                          old black console window (applies on next start)",
    "====================================================================",
]

_lock = threading.Lock()
_cache = {"mtime": None, "data": None, "checked": 0.0}


def _merged(user):
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for section, vals in (user or {}).items():
        if section.startswith("_") or section not in out or not isinstance(vals, dict):
            continue
        for k, v in vals.items():
            if not k.startswith("_") and k in out[section]:
                out[section][k] = v
    return out


def _write_default_file():
    try:
        doc = {"_readme": _README}
        doc.update({k: dict(v) for k, v in DEFAULTS.items()})
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
    except OSError:
        pass


# How often we're willing to stat settings.json. get() is called hundreds of
# times while building ONE map response, and stat'ing the file every time cost
# ~590 syscalls and a third of the total time. A second of staleness is
# imperceptible for a hot-reload; the saving is not.
_RECHECK_SEC = 1.0


def all():
    """The full settings dict, hot-reloaded when settings.json changes."""
    with _lock:
        now = time.monotonic()
        if _cache["data"] is not None and now - _cache.get("checked", 0.0) < _RECHECK_SEC:
            return _cache["data"]
        _cache["checked"] = now
        try:
            m = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            _write_default_file()               # first run: create it for the user
            try:
                m = os.path.getmtime(SETTINGS_FILE)
            except OSError:
                m = None
        if _cache["data"] is None or m != _cache["mtime"]:
            try:
                # utf-8-SIG: Notepad, PowerShell's Set-Content and friends write a
                # BOM. Plain utf-8 chokes on it, and every setting in the file was
                # then silently ignored in favour of the defaults.
                with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as fh:
                    user = json.load(fh)
            except OSError:
                user = {}
            except ValueError as e:
                user = {}
                print(f"!! settings.json could not be read ({e}) -- using DEFAULTS "
                      f"for everything. Fix the file, or delete it to start over: "
                      f"{SETTINGS_FILE}")
            _cache["data"] = _merged(user)
            # A settings.json written by an older build is missing whatever has
            # been added since, and nothing ever put it there -- so new settings
            # were invisible unless you deleted the file. Top it up in place,
            # keeping every value you had.
            if _top_up(user, _cache["data"]):
                try:
                    m = os.path.getmtime(SETTINGS_FILE)
                except OSError:
                    pass
            _cache["mtime"] = m
        return _cache["data"]


def _top_up(user, merged):
    """Add any missing section/key to settings.json. Returns True if it wrote."""
    if not isinstance(user, dict) or not user:
        return False                       # no file yet, or unreadable: leave it
    missing = any(s not in user or not isinstance(user.get(s), dict)
                  or any(k not in user[s] for k in keys)
                  for s, keys in merged.items())
    if not missing:
        return False
    doc = {"_readme": _README}
    for section, keys in merged.items():
        doc[section] = dict(keys)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
    except OSError:
        return False
    return True


def set(section, key, value):
    """Persist one setting to settings.json (created/merged). all() hot-reloads it
    by mtime, so the change applies within a few seconds -- no restart."""
    all()                                   # ensure the file exists first
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    doc.setdefault(section, {})[key] = value
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    with _lock:                             # force the very next all() to re-read
        _cache["checked"] = 0.0
        _cache["mtime"] = None
    return value


def set_values(section, values):
    """Write keys into one section of settings.json (used by the World Manager),
    keeping everything else in the file as it was."""
    with _lock:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError):
            user = {}
        doc = {"_readme": _README}
        doc.update(_merged(user))
        for k, v in values.items():
            if k in DEFAULTS[section]:
                doc[section][k] = v
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        _cache["data"] = None                  # reload on the next get()


def get(section, key, env=None, cast=None):
    """One setting. An environment variable of the same name still wins, so old
    shortcuts keep working."""
    data = all()            # always first, so settings.json is created even when
                            # an env var would short-circuit the lookup below
    if env:
        raw = os.environ.get(env)
        if raw not in (None, ""):
            try:
                return (cast or type(DEFAULTS[section][key]))(raw)
            except (TypeError, ValueError):
                pass
    val = data.get(section, {}).get(key, DEFAULTS[section][key])
    if cast:
        try:
            return cast(val)
        except (TypeError, ValueError):
            return DEFAULTS[section][key]
    return val
