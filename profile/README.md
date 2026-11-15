# CYK Games

Small games built by AI agents under human direction — one numbered repo per
title. Every game ships full source + a downloadable build, and keeps its dev
log in `docs/`.

## Catalog

| # | Game | Engine | Platform | Status | Download |
|---|------|--------|----------|--------|----------|
| 001 | [Voxel Parkour](https://github.com/cykgames/001-parkour) | Godot 4.7.2 | Android (arm64) | playable | [APK v0.1.0](https://github.com/cykgames/001-parkour/releases/download/v0.1.0/godot-parkour-v0.1.0.apk) |

### 001 · Voxel Parkour

Minecraft-style auto-runner: floating analog joystick, jump the gaps, coins,
checkpoints, 3 themed levels (Grassland / Canyon / Frost). Zero external
assets — terrain, SFX and music are all procedural.

<img src="https://raw.githubusercontent.com/cykgames/001-parkour/main/docs/screenshot.png" width="240">

## Conventions

- **Repo name**: `NNN-slug` — zero-padded serial + short slug (`001-parkour`, `002-...`)
- **Status**: `prototype` → `mvp` → `playable` → `released` (or `abandoned`)
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
