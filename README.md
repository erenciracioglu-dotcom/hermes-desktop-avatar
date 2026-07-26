# Hermes Desktop Avatar

PySide6 desktop avatar — a UI shell (skin) for a local **Hermes Agent** gateway.

## What this is

A small always-on-top sprite on the desktop. Double-click or right-click → chat.
Type a message; Hermes answers through its gateway. There is no automatic
screen-commentary loop and no built-in LLM provider stack — the avatar only
talks when the user pokes it.

## Architecture

```
                +----------------------+
   user click   |   avatar (PySide6)   |
   ───────────► |  - sprite overlay    |
                |  - chat panel        |
                |  - state machine     |
                +---------+------------+
                          │ HTTP
                          ▼
                +----------------------+
                |  Hermes gateway      |   :8642  (api_server)
                |  POST /v1/chat/…     |
                +---------+------------+
                          │
                          ▼
                      AIAgent / LLM
```

Session isolation uses `X-Hermes-Session-Id` (default `avatar-nora`).

### Local gateway auto-manage

For **loopback** URLs only (`127.0.0.1` / `localhost` / `::1`), the avatar
can bring up the Hermes API itself:

1. `GET {gateway_url}/health`
2. If down: ensure Hermes can start the **API server**
   - Hermes **refuses** to bind `:8642` without `API_SERVER_KEY` (≥16 chars)
   - Avatar sets `API_SERVER_ENABLED=true`, `API_SERVER_KEY`, host/port
   - By default also appends missing keys to `HERMES_HOME/.env` (durable)
3. Run `hermes gateway restart` (or detached `hermes gateway`)
4. Poll `/health` until ready (default 60s) or show a log-based diagnosis

Remote gateway URLs are never started or restarted. CLI discovery:
`hermes.hermes_command` → `HERMES_CLI` / `HERMES_COMMAND` / `HERMES_BIN`
→ `hermes` on `PATH` → common layouts under `HERMES_HOME` /
`HERMES_AGENT_DIR`.

A running Telegram/messaging gateway is **not** enough: the OpenAI-compatible
HTTP API (`api_server` platform) must be enabled and keyed.

## Desktop screenshots (client-side skill)

Hermes on the API channel may not have computer-use tools. The avatar exposes a
**client-side screenshot skill** the model can request:

1. System prompt teaches a **skill**: for a *live* screen request, Ava must
   emit ``[[DESKTOP_SCREENSHOT]]`` on its own line (not for mere mentions of
   screenshots in conversation).
2. The client captures immediately after the tag (no second user message),
   shows the PNG in chat, and sends a multimodal follow-up.
3. **Fallback:** if the user clearly asks for a live view (“what’s on my screen”, “take a screenshot”) and Ava omits the tag or claims she cannot see, the client still captures once — so the skill works even when the model forgets the protocol.
4. **Manual:** chat **📷** attaches a desktop PNG without waiting for Ava.

Mere chat about “screenshot files” does **not** auto-capture. Files go under
`%APPDATA%/hermes-desktop-avatar/screenshots/`.

## States

- **idle** — random idle animation, awaiting input  
- **thinking** — waiting for the gateway response  
- **talking** — text returned; optional TTS later  

## Run

```bat
run.bat
```

or:

```bat
set PYTHONPATH=src
.venv\Scripts\python.exe -m avatar
```

Requires the project venv (PySide6, requests) and Hermes Agent with the
`hermes` CLI available (or a gateway already healthy at `gateway_url`).

## Config

- Defaults: `config.default.json` (repo root)  
- User overrides: `%APPDATA%\hermes-desktop-avatar\config.json`  

Important keys under `hermes`:

| Key | Default | Meaning |
|-----|---------|---------|
| `gateway_url` | `http://127.0.0.1:8642` | Hermes gateway base URL |
| `session_id` | `avatar-nora` | Gateway session isolation |
| `auto_start` | `true` | If local `/health` fails, try to start `hermes gateway` |
| `auto_restart` | `true` | Prefer `hermes gateway restart` when the API is down (stale PID lock) |
| `startup_timeout_seconds` | `60` | How long to wait for `/health` after start/restart |
| `hermes_command` | `null` | Optional path/name of the Hermes CLI |
| `api_key` | `null` | Bearer key (= Hermes `API_SERVER_KEY`); auto-filled if missing |
| `ensure_api_server` | `true` | Inject `API_SERVER_*` env when managing a local gateway |
| `write_hermes_env` | `true` | Persist missing `API_SERVER_*` into `HERMES_HOME/.env` |
| `persona` | environment text | **Channel/context overlay** sent as `role: system` each turn (ephemeral layer on top of Hermes SOUL — not a character rewrite) |

Env overrides: `HERMES_GATEWAY_URL`, `HERMES_GATEWAY_KEY` / `API_SERVER_KEY`,
`HERMES_AVATAR_SESSION`, `HERMES_CLI` (or `HERMES_COMMAND` / `HERMES_BIN`),
`HERMES_HOME`.

### Setup agent — per-user configuration

The avatar ships with sensible defaults but a setup/installation agent running
on behalf of a fresh user must configure the following **machine-specific
values** before first launch. None of these are secrets; all are paths and
endpoints that differ per machine.

| Item | Where it lives | What the agent should do |
|------|----------------|--------------------------|
| **Hermes Agent source** | `HERMES_HOME` (env) or `%LOCALAPPDATA%/hermes` | Discover the user's existing Hermes Agent checkout. Avatar reads voice tools (TTS) from there. Common layouts: `C:/hermes-agent`, `%LOCALAPPDATA%/hermes/hermes-agent`, `~/.hermes/...`. |
| **Hermes CLI** | `hermes.hermes_command` in config | Find the user's `hermes` binary. Order: `HERMES_CLI` env → `HERMES_COMMAND` → `HERMES_BIN` → `hermes` on `PATH` → `HERMES_HOME/hermes` or `HERMES_AGENT_DIR/hermes`. |
| **Gateway URL** | `hermes.gateway_url` (default `http://127.0.0.1:8642`) | Point at the user's existing gateway. Only loopback (`127.0.0.1` / `localhost`) is auto-managed; remote URLs must already be healthy. |
| **Gateway API key** | `hermes.api_key` | If Hermes is being managed locally and requires a key, set it once and let `write_hermes_env=true` persist it into the gateway's `.env`. Otherwise leave `null` if the gateway is open. |
| **Gateway session id** | `hermes.session_id` | Distinct value per avatar deployment (default `avatar-nora`). Set this if the user already uses other Hermes sessions, to avoid conversation mixing. |
| **Gateway persona** | `hermes.persona` | The user-editable system overlay the avatar sends each turn. Default is generic English; replace if the user has a different voice / tone requirement. |
| **Voice / TTS** | `voice.*` keys + Hermes provider config | Avatar calls Hermes's `text_to_speech` tool in-process. The configured provider in the user's `HERMES_HOME/config.yaml` (xAI / MiniMax / Edge / etc.) decides the voice. `voice.edge_fallback` keeps things working if Hermes is offline. |
| **Character pack location** | `assets/characters/` (bundled), `%APPDATA%/hermes-desktop-avatar/characters/` (user) | Place extra `.hchar` files or unpacked `<id>/character.json` here if the user has a pack to install. Default bundled character is `nora` (the only file shipped in this repo is `nora.hchar`; on first launch it is unpacked to `%APPDATA%/hermes-desktop-avatar/character_cache/nora_v1.0.0/`). |
| **Sprite assets** | `external_clip_roots` in `assets/characters/nora/character.json` | Only required if a sprite fails to load and you need to add an extra search root (e.g. a per-user sprite folder). The release manifest leaves this empty on purpose: the bundled `nora.hchar` is the only source. |

A setup agent should:

1. Detect whether `hermes` CLI is already on `PATH`; if not, ask the user
   before adding it.
2. Confirm the user's preferred gateway URL — do **not** assume `localhost`.
3. Generate an API key (≥16 chars) only if Hermes requires one and the user
   has none configured.
4. Set `hermes.session_id` to something unique (`avatar-<userhandle>` is fine).
5. Decide whether the user wants voice replies and pick the corresponding
   provider key in Hermes's config.
6. Leave `mascot_v2/`, `character_sets/` and other pipeline-only folders alone
   — they are gitignored and not part of this release.

### Gateway lifecycle (loopback only)

The avatar talks to Hermes Agent via the **OpenAI-compatible HTTP API**
(default `http://127.0.0.1:8642`). The Hermes CLI is shipped separately from
this avatar, so on first launch the avatar may need to bring the gateway up.
This logic is in `src/avatar/gateway_manager.py::ensure_gateway()` and runs
exactly once at startup.

Sequence:

1. `GET {gateway_url}/health` with `trust_env=False` (corporate proxies
   otherwise break loopback). If 200 → done, action `already_up`.
2. If the URL is **not** loopback (`127.0.0.1`, `localhost`, `::1`,
   `0.0.0.0`) the avatar refuses to manage it — `skipped_remote`. Remote
   gateways must already be healthy.
3. Otherwise the avatar prepares the API server env and **only then** starts
   the gateway. The **avatar** writes the API server settings — not the
   Hermes CLI. This is handled by
   `src/avatar/gateway_manager.py::ensure_hermes_api_server_env()`:

   - `API_SERVER_ENABLED=true` and `API_SERVER_KEY=<≥16 chars>` (the avatar
     generates one via `secrets.token_urlsafe()` if missing). Without these,
     Hermes binds its messaging platforms (Telegram, …) but not `:8642`,
     even if a `hermes` process is running — a frequent cause of "wait 45 s
     then fail".
   - `API_SERVER_HOST` and `API_SERVER_PORT` default to `127.0.0.1:8642`.
     The avatar forces loopback (`127.0.0.1`) when the user-configured host
     is `0.0.0.0`, `::`, or `localhost` because desktop users expect a
     loopback-only API server.
   - The avatar reads `HERMES_HOME/.env` first with `_parse_dotenv()` and
     **respects existing keys** — it will not overwrite:
     - A valid `API_SERVER_KEY` (≥16 chars) → the avatar reuses it as
       `Authorization: Bearer …` so a manually-configured Hermes stays in
       sync with the avatar.
     - A `false`/`0`/empty `API_SERVER_ENABLED` → flipped to `true` only
       if it is missing or was previously disabled.
     - A custom `API_SERVER_PORT` → preserved; the avatar will not silently
       rebind a port the user picked.
   - When `hermes.write_hermes_env=true` (default), missing values are
     appended to `HERMES_HOME/.env` inside a clearly marked block:

     ```text
     # --- hermes-desktop-avatar: OpenAI-compatible API server (loopback) ---
     API_SERVER_ENABLED=true
     API_SERVER_KEY=<generated>
     API_SERVER_HOST=127.0.0.1
     API_SERVER_PORT=8642
     # --- end hermes-desktop-avatar ---
     ```

     On every run, `_append_dotenv()` first **strips the previous
     avatar-managed block** via regex, then re-appends — so re-running the
     avatar never produces duplicate `API_SERVER_*` entries. The avatar
     never touches keys it did not manage (e.g. `HERMES_HOME`, OAuth
     tokens, Telegram bot tokens, model provider keys).
4. The avatar prefers **`hermes gateway restart`** when `auto_restart=true`
   (default) — this clears stale PID locks left by a crashed previous
   gateway. If `gateway restart` fails, it falls back to
   `hermes gateway run --replace`.
5. If both fail, the avatar starts a detached `hermes gateway` (Windows:
   `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, stdio to DEVNULL) and
   polls `/health` until `startup_timeout_seconds` (default 60 s).
6. The first key the avatar generated/picked is cached as
   `config.json:hermes.api_key` and reused as `Authorization: Bearer …`
   on every chat call.

Conflict handling rules (read these before changing default behaviour):

- **Other instances of `hermes gateway` already running for the same
  `:8642`:** the avatar detects via `/health` and uses `gateway restart`,
  which atomically replaces the previous worker. Manual instances still
  using that port get their messaging loop (Telegram, etc.) preserved.
- **Multiple avatar instances:** `single_instance.py` blocks a second
  avatar from starting. This is intentional — two avatars on the same
  `:8642` compete for chat interrupts.
- **A non-Hermes service already on `:8642`:** `/health` will not return
  200 and the avatar surfaces the error. Users should either
  `hermes.gateway_url` to a free port or stop the conflicting service.
- **`hermes` CLI not on `PATH` and not in `HERMES_HOME`:** the avatar
  cannot manage the gateway at all. UI shows the discovery steps; the
  user must install the Hermes CLI (e.g. `pipx install hermes-agent`)
  and retry.
- **`API_SERVER_KEY` regenerated by the avatar:** the prior Bearer key on
  saved avatar clients invalidates. The avatar overwrites
  `HERMES_HOME/.env`, so the next manual `hermes gateway` keeps the new
  key in sync.

For setup agents: after the avatar's first successful `ensure_gateway()`,
`%APPDATA%/hermes-desktop-avatar/config.json` contains the resolved
`hermes.api_key` and `hermes.gateway_url`. Treat these as the source of
truth going forward — env vars still override.

### Context overlay

Each chat request includes a short fixed preamble (“you are still Hermes”) plus
the user-editable `hermes.persona` text. Gateway maps that to
`ephemeral_system_prompt` above core identity. Edit under **Settings → Environment context**.

### Voice replies (avatar-owned TTS)

Toggle **Voice replies** (`voice.enabled`). Architecture:

1. Hermes gateway returns **text only** (model is told not to call TTS / MEDIA).
2. Avatar calls Hermes Agent’s **`text_to_speech` tool in-process** with that
   exact reply string — uses the user’s Hermes TTS provider (xAI, MiniMax,
   Edge, … from `HERMES_HOME/config.yaml`).
3. If the tool import/call fails and `voice.edge_fallback` is true (default),
   Edge TTS speaks the same text.
4. Qt Multimedia auto-plays the file; talking animation lasts until playback ends.

This avoids relying on the LLM to “remember” to generate audio every turn
(which caused silent replies, stale `nora_voice.ogg`, and mismatched speech).

Requires a local Hermes agent checkout with `tools/tts_tool.py` (e.g.
`C:\hermes-agent` or `%LOCALAPPDATA%\hermes\hermes-agent`) and `HERMES_HOME`
pointing at the config that already works for Telegram TTS. No Hermes source patch.

## Layout

```
src/avatar/
  __main__.py         entry: python -m avatar
  app.py              Qt app, tray, wiring
  overlay.py          transparent sprite window
  sprites.py          frame loading
  characters.py       Jenny / Nora presets
  state_machine.py    idle / thinking / talking
  idle_animator.py    ambient pick
  chat_history.py     JSON chat log
  chat_panel.py       chat UI
  console_widget.py   avatar.log tail
  runtime_agent.py    gateway HTTP client
  gateway_manager.py  local health-check / auto start-restart
  controller.py       state + chat orchestration
  settings.py         config load/save
  settings_dialog.py  settings UI
  paths.py            paths
  single_instance.py  one instance only
```

Sprite assets live under `assets/sprites/`. Processing helpers are in `scripts/`.

## Character packs

Characters are data packs (not hard-coded). Currently only **Nora** ships.

| Kind | Location |
|------|----------|
| Directory pack | `assets/characters/<id>/character.json` |
| Portable pack | `assets/characters/<id>.hchar` (zip: manifest + `clips/*.webp`) |
| User packs | `%APPDATA%/hermes-desktop-avatar/characters/` |

Full packaging guide: [`avatar_packing.md`](avatar_packing.md).

```bat
python scripts\pack_character.py nora
```

Pick the active character under **Settings → Character** (`character_id` in config).
