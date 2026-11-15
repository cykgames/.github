# CYK Games

Small games built by AI agents under human direction — one numbered repo per
title. Every game ships full source + a downloadable build, and keeps its dev
log in `docs/`.

## Catalog

| # | Game | Author | Genre | Engine | Platform | Status | Download |
|---|------|--------|-------|--------|----------|--------|----------|
| 001 | [Voxel Parkour](https://github.com/cykgames/001-parkour) | cykbot9 | runner | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.0](https://github.com/cykgames/001-parkour/releases/download/v0.1.0/godot-parkour-v0.1.0.apk) |
| 002 | [Obby World](https://github.com/cykgames/002-obby) | cykbot9 | obby | Godot 4.7.2 | Android (arm64) | playable | [APK v0.2.0](https://github.com/cykgames/002-obby/releases/download/v0.2.0/002-obby-v0.2.0.apk) |
| 004 | [Match3](https://github.com/cykgames/004-match3) | cykbot4 | puzzle | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.0](https://github.com/cykgames/004-match3/releases/download/v0.1.0/004-match3-v0.1.0.apk) |
| 005 | [Marble Runner](https://github.com/cykgames/005-marble) | cykbot4 | roller | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.0](https://github.com/cykgames/005-marble/releases/download/v0.1.0/005-marble-v0.1.0.apk) |
| 008 | [Space Shooter](https://github.com/cykgames/008-shmup) | cykbot4 | arcade | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.0](https://github.com/cykgames/008-shmup/releases/download/v0.1.0/008-shmup-v0.1.0.apk) |

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

### 008 · Space Shooter

Top-down arcade shmup: floating joystick, autofire with auto-aim, overheat /
reload energy cycle, enemies, meteors and pickups. Touch port of
acfaruk/gd-space-shooter (MIT); Kenney CC0 art, procedural BGM.

<img src="https://raw.githubusercontent.com/cykgames/008-shmup/main/docs/screenshots/gameplay.png" width="240">

## Conventions

- **Repo name**: `NNN-slug` — zero-padded serial + short slug (`001-parkour`, `002-...`)
- **Genre**: `runner` / `obby` / `arcade` / `puzzle` / `racing` / `roller` /
  `idle` / `sandbox` — parallel dev waves assign one distinct genre per agent
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
