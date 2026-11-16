# CYK Games

Small games built by AI agents under human direction — one numbered repo per
title. Every game ships full source + a downloadable build, and keeps its dev
log in `docs/`.

## Catalog

| # | Game | Author | Genre | Engine | Platform | Status | Download |
|---|------|--------|-------|--------|----------|--------|----------|
| 001 | [Voxel Parkour](https://github.com/cykgames/001-parkour) | cykbot9 | runner | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.3](https://github.com/cykgames/001-parkour/releases/download/v0.1.3/001-parkour-v0.1.3.apk) |
| 002 | [Obby World](https://github.com/cykgames/002-obby) | cykbot9 | obby | Godot 4.7.2 | Android (arm64) | playable | [APK v0.2.3](https://github.com/cykgames/002-obby/releases/download/v0.2.3/002-obby-v0.2.3.apk) |
| 003 | [Outpost Assault](https://github.com/cykgames/003-outpost) | cykbot12 | tower-defense | Godot 4.7.2 | Android (arm64) | prototype | [APK v0.1.2](https://github.com/cykgames/003-outpost/releases/download/v0.1.2/003-outpost-v0.1.2.apk) |
| 004 | [Match3](https://github.com/cykgames/004-match3) | cykbot4 | puzzle | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.1](https://github.com/cykgames/004-match3/releases/download/v0.1.1/004-match3-v0.1.1.apk) |
| 005 | [Marble Runner](https://github.com/cykgames/005-marble) | cykbot4 | roller | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.2](https://github.com/cykgames/005-marble/releases/download/v0.1.2/005-marble-v0.1.2.apk) |
| 006 | [Tomb Explorer](https://github.com/cykgames/006-tomb) | cykbot7 | adventure | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.2](https://github.com/cykgames/006-tomb/releases/download/v0.1.2/006-tomb-v0.1.2.apk) |
| 007 | [Fruit Merge](https://github.com/cykgames/007-merge) | cykbot6 | puzzle | Godot 4.7.2 | Android (arm64) | prototype | [APK v0.1.1](https://github.com/cykgames/007-merge/releases/download/v0.1.1/007-merge-v0.1.1.apk) |
| 008 | [Space Shooter](https://github.com/cykgames/008-shmup) | cykbot4 | arcade | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.1](https://github.com/cykgames/008-shmup/releases/download/v0.1.1/008-shmup-v0.1.1.apk) |
| 009 | [CykShooter](https://github.com/cykgames/009-shooter) | cykbot1 | shooter | Godot 4.7.2 | Android (arm64) | prototype | [APK v0.1.2](https://github.com/cykgames/009-shooter/releases/download/v0.1.2/009-shooter-v0.1.2.apk) |

### 001 · Voxel Parkour

Minecraft-style auto-runner: floating analog joystick, jump the gaps, coins,
checkpoints, 3 themed levels (Grassland / Canyon / Frost). Zero external
assets — terrain, SFX and music are all procedural.

<img src="https://raw.githubusercontent.com/cykgames/001-parkour/main/docs/screenshot.png" width="240">

### 002 · Obby World

Roblox-style block obstacle course: omnidirectional joystick + double jump,
16 levels across 4 worlds (Meadow / Canyon / Frost / Volcano), checkpoints,
coins, par times, 3-star ratings and sequential unlock. Moving platforms, lava, bounce
pads, vanishing blocks, rotating bars and ice. Character & decorations from
Kenney's CC0 Starter Kit 3D Platformer.

<img src="https://raw.githubusercontent.com/cykgames/002-obby/main/docs/screenshots/volcano_gameplay.png" width="240">

### 003 · Outpost Assault

2D tile-based tower defense: defend the outpost (500 HP) against 3 escalating
waves across 12 turret slots — Gatling / Single / Missile turrets vs infantry,
tanks and helicopters. Tap a slot to build, drag to pan the battlefield, zoom
buttons. Mobile adaptation of quiver-dev/tower-defense-godot4 (MIT code,
CC BY 4.0 art); upstream ships no SFX, so silence is expected.

<img src="https://raw.githubusercontent.com/cykgames/003-outpost/main/docs/screenshots/gameplay.png" width="240">

### 004 · Match3

Halloween match-3 puzzle: drag adjacent pieces to match 3+, cascades and
combos, levels, shuffle; portrait and touch-first. From luiz734/match3_game
(MIT); author-owned art, OFL font.

<img src="https://raw.githubusercontent.com/cykgames/004-match3/main/docs/screenshots/gameplay.png" width="240">

### 005 · Marble Runner

Torque-driven marble roller: 60 courses in the sky, gems / stars /
checkpoints, powerups and a shop; drag the left half to roll, JUMP button.
From kaushalmeena/marble-runner (MIT); fully procedural assets, OFL font.

<img src="https://raw.githubusercontent.com/cykgames/005-marble/main/docs/screenshots/gameplay.png" width="240">

### 006 · Tomb Explorer

Tomb-Raider-style blocky 3D action-adventure: explore 6 tomb chambers, climb
ledges (auto-mantle), dodge spike pits / sliding blades / crumbling floors,
pull levers and step on pressure plates, and collect 3 relics to unlock the
Treasure Sanctum. Joystick + double jump, coins and 3-star ratings. Built
in-house on the 002-obby core; Kenney CC0 models, procedural audio.

### 007 · Fruit Merge

Suika-like fruit-merge puzzle: drop and merge same-tier fruit up an 11-step
chain (cherry → watermelon), chain multipliers, three energy skills (hammer /
shake / rainbow fruit), double-watermelon blast and a cross-run evolution
codex. From jlake/merge-fruits (MIT); all art and SFX are procedural, ZCOOL
KuaiLe font (OFL).

<img src="https://raw.githubusercontent.com/cykgames/007-merge/main/docs/screenshots/gameplay.png" width="240">

### 008 · Space Shooter

Top-down arcade shmup: floating joystick, autofire with auto-aim, overheat /
reload energy cycle, enemies, meteors and pickups. Touch port of
acfaruk/gd-space-shooter (MIT); Kenney CC0 art, procedural BGM.

<img src="https://raw.githubusercontent.com/cykgames/008-shmup/main/docs/screenshots/gameplay.png" width="240">

### 009 · CykShooter

Top-down zombie arena shooter: twin-stick move + aim / auto-fire, dash, 5
weapons (gun / shotgun / rifle / bomb / sword), endless waves of zombies and
splitting slimes plus the Big Jelly boss. Touch port of
nezvers/Godot-GameTemplate (MIT); joystick adapted from 002-obby.

<img src="https://raw.githubusercontent.com/cykgames/009-shooter/main/addons/top_down/assets/images/project_preview.png" width="240">

## Conventions

- **Repo name**: `NNN-slug` — zero-padded serial + short slug (`001-parkour`, `002-...`)
- **Genre**: `runner` / `obby` / `arcade` / `puzzle` / `racing` / `roller` /
  `idle` / `sandbox` / `tower-defense` / `adventure` / `shooter` — parallel dev
  waves assign one distinct genre per agent
- **Status**: `prototype` → `mvp` → `playable` → `released` (or `abandoned`)
- **Author**: the building bot (`cykbotN`) is named in the catalog row, the
  repo README header, `CREDITS.md`, and the in-game title screen — ask that
  bot (or read its `docs/CHECKPOINT.md`) when taking over debugging
- **Layout**: source at root, dev checkpoints in `docs/`, builds attached to
  GitHub Releases (binaries never committed)
- **Public by default**; git history sanitized before first push (no LAN IPs,
  device serials, or user-profile paths)

## Adding a new game

```bash
# GH_TOKEN env with repo scope required
python scripts/new_game.py my-slug --local ~/projects/my-slug
```

Creates `NNN-my-slug` with the next free serial, and optionally scaffolds a
local starter repo (README + .gitignore) and pushes it. Then build the game,
update the catalog row above, and attach builds to Releases.
