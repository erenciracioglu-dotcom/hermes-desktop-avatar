# Avatar character packing

This document describes how **Hermes Desktop Avatar** loads mascot characters
as portable packs, how Nora is packaged today, and how to add a new character
later without changing application code.

---

## Goals

| Goal | Approach |
|------|----------|
| One downloadable file per character | `.hchar` = ZIP + `character.json` + `clips/` |
| Easy local development | Directory pack with `character.json` (optional external clip roots) |
| App does not hard-code clip lists for Nora | Registry scans pack folders; Settings picks `character_id` |
| Jenny / other built-ins | Temporarily removed; re-add via pack or `BUILTIN_PRESETS` later |

---

## Concepts

### Character vs app

- **App (motor):** overlay, state machine (`idle` / `thinking` / `talking`),
  rarity-weighted ambient picker, chat, TTS, gateway.
- **Character (data):** which animated WebP clips map to which states, ambient
  pool, rarity weights, label/version.

Identity / SOUL / LLM tools stay on the **Hermes gateway**. Packs do **not**
replace Hermes; they only change the desktop skin.

### Two pack shapes

1. **Directory pack** (repo / development)

   ```text
   assets/characters/nora/
     character.json
     clips/                 # optional local WebPs
   ```

   Nora’s repo pack uses `external_clip_roots` so large WebPs stay under
   `assets/sprites/mascot_v2/` without being duplicated.

2. **Portable pack** (distribution / download)

   ```text
   nora.hchar               # zip file
     character.json
     clips/
       nora_idle_a_sigh0.webp
       nora_talking1.webp
       …
   ```

   Built by `scripts/pack_character.py`. On first load the app extracts to:

   `%APPDATA%/hermes-desktop-avatar/character_cache/<id>_v<version>/`

User-installed packs can also live under:

`%APPDATA%/hermes-desktop-avatar/characters/`

---

## Discovery order

Implemented in `src/avatar/character_registry.py`:

1. `BUILTIN_PRESETS` in `characters.py` (currently **empty**)
2. Bundled: `assets/characters/`
   - subfolders with `character.json`
   - then `*.hchar` files (same `id` → **file wins** over directory)
3. User: `%APPDATA%/hermes-desktop-avatar/characters/` (same rules)

Active character: config key `character_id` (default `"nora"`), chosen in
**Settings → Character**.

---

## Manifest schema (`character.json`) v1

Required:

| Field | Type | Meaning |
|-------|------|---------|
| `schema` | int | Must be `1` (app supports ≤ current) |
| `id` | string | Stable id (`nora`); used as `character_id` |
| `label` | string | UI name |
| `states` | object | Map state → list of clip paths or filenames |

Important states used by the overlay:

| State | When |
|-------|------|
| `idle` | Ambient pool base (or use `ambient.pool`) |
| `talk` | Controller `TALKING` (TTS / reply) |
| `think` | Controller `THINKING` (waiting for gateway) |

Optional:

```json
{
  "schema": 1,
  "id": "nora",
  "label": "Nora",
  "version": "1.0.0",
  "author": "…",
  "license": "user-content",
  "default_fps": 24,
  "external_clip_roots": [
    "../../sprites/mascot_v2",
    "../../sprites/mascot"
  ],
  "states": {
    "idle": ["nora_idle_a_sigh0.webp", "…"],
    "talk": ["nora_talking1.webp", "nora_talking2.webp"],
    "think": ["nora_thinking1.webp", "nora_thinking2.webp", "nora_thinking3.webp"]
  },
  "ambient": {
    "pool": ["nora_idle_a_sigh0.webp", "nora_idle_b_jumping.webp", "…"],
    "rarity": {
      "nora_idle_a_sigh0.webp": 1.0,
      "nora_idle_b_jumping.webp": 4.0,
      "nora_idle_c_impatient.webp": 20.0
    }
  },
  "fallbacks": {
    "dance": "idle",
    "sleep": "idle"
  },
  "preview": "preview.png"
}
```

### Rarity

- **Lower number = more frequent** (weight ≈ `1 / rarity`).
- Nora tiers: soft idle ≈ `1`, playful ≈ `4`, rare ≈ `20`.
- Keys may be filename, stem, or `clips/….webp`; loader normalizes to stem.

### Clip formats

- Prefer **animated WebP** (one file per clip).
- Loader: Pillow decode, optional lazy frames (`AVATAR_LAZY_SPRITE`).
- PNG sequences still work for legacy paths; new packs should ship WebP.

---

## Nora packaging (what we did)

### Source recipe (code)

`src/avatar/characters.py` defines the Nora pack **recipe** (not a live
built-in preset):

- `NORA_PACK_CLIPS` — idle / talk / think stems  
- `NORA_PACK_AMBIENT` — ambient pool stems  
- `NORA_PACK_RARITY` — rarity weights  

Used only by the packer script.

### Repo directory pack

`assets/characters/nora/character.json`

- Points `external_clip_roots` at `mascot_v2` then `mascot`.
- App resolves stems like `nora_talking1` →
  `assets/sprites/mascot_v2/nora_talking1.webp`.

### Portable `.hchar`

```bat
set PYTHONPATH=src
python scripts\pack_character.py nora
```

Default output: `assets/characters/nora.hchar`

What the script does:

1. Resolve each stem under `mascot_v2/`, then `mascot/`.
2. Build `character.json` (schema v1).
3. ZIP with `ZIP_STORED` (WebP already compressed) as:
   - `character.json`
   - `clips/<stem>.webp` for every clip.

Large files: `assets/characters/.gitignore` ignores `*.hchar` so git stays
light; directory pack remains the source of truth in the repo.

### Runtime load path

```text
registry finds nora.hchar or nora/character.json
        │
        ▼
  load_preset_from_pack  → extract to character_cache/…
  or load_preset_from_directory → external roots / local clips
        │
        ▼
  CharacterPreset (sprite_map, idle_ambient, idle_rarity, asset_root)
        │
        ▼
  sprites.load_frames_for_preset → overlay.set_character
```

Key modules:

| Module | Role |
|--------|------|
| `character_pack.py` | Validate manifest, zip I/O, extract, directory load |
| `character_registry.py` | Scan dirs, list/resolve characters |
| `characters.py` | `CharacterPreset` + Nora recipe constants |
| `sprites.py` | Resolve prefixes under `asset_root` / extra roots |
| `settings_dialog.py` | Character combo + preview |
| `scripts/pack_character.py` | Build `nora.hchar` |

---

## Adding a new character (checklist)

1. **Clips** — animated WebPs for at least idle ambient + talk (+ think).
2. **Manifest** — copy `assets/characters/nora/character.json`, change `id` /
   `label` / paths / rarity.
3. **Install for dev**
   - Folder: `assets/characters/<id>/character.json`  
     (and either `clips/` or `external_clip_roots`), **or**
   - File: place `<id>.hchar` under `assets/characters/` or user `characters/`.
4. **Pack for distribution** (optional helper; today only `nora` recipe exists):

   ```bat
   python scripts\pack_character.py nora
   ```

   For a new id, either extend `scripts/pack_character.py` with a recipe or
   call `avatar.character_pack.write_pack()` from a small script.
5. **Select** — Settings → Character, or set `"character_id": "<id>"` in config.
6. **No app release required** if the pack is dropped into a scanned folder.

---

## API surface (Python)

```python
from avatar.character_registry import list_characters, resolve_character
from avatar.character_pack import write_pack, load_preset_from_pack

# All known characters
for c in list_characters(force=True):
    print(c.id, c.label, c.source, c.version)

# Active preset for overlay
preset = resolve_character("nora")

# Create a zip pack programmatically
write_pack(
    out_path,
    manifest_dict,           # must pass validate_manifest rules
    {"clips/foo.webp": Path("…/foo.webp"), …},
)
```

---

## Settings / config

| Key | Default | Meaning |
|-----|---------|---------|
| `character_id` | `"nora"` | Selected pack id |

User config: `%APPDATA%/hermes-desktop-avatar/config.json`  
Defaults: `config.default.json`

---

## Design rules (keep these)

1. **Data not code** — new skins should not require editing `overlay.py`.
2. **Ship WebP, not raw PNG dumps** — keep `character_sets/` as pipeline only.
3. **Client owns TTS** — packs do not embed Hermes voice identity.
4. **Schema version** — bump `schema` when breaking manifest fields; refuse
   packs newer than the app supports.
5. **Same `id` override** — user/portable pack can replace a bundled pack.

---

## Related paths

```text
assets/characters/           # bundled packs
assets/characters/nora/      # Nora directory pack (git)
assets/characters/nora.hchar # optional portable build (gitignored)
assets/sprites/mascot_v2/    # Nora WebP sources for packing
scripts/pack_character.py    # build .hchar
src/avatar/character_pack.py
src/avatar/character_registry.py
src/avatar/characters.py
src/avatar/sprites.py
src/avatar/settings_dialog.py
```

---

## Future ideas (not implemented)

- Settings **Import .hchar…** button  
- Download URL + checksum for marketplace  
- Signed packs  
- Pack recipe CLI for arbitrary folders without hard-coded Nora lists  
- Bring Jenny back as a `.hchar` rather than code built-in  
