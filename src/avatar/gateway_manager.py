"""Ensure a local Hermes gateway API is reachable for the desktop avatar.

The avatar talks only to the OpenAI-compatible HTTP API (default
``http://127.0.0.1:8642``). A Hermes *process* can exist without that
port accepting connections (stale lock, API server disabled / missing
``API_SERVER_KEY``, crashed worker). This module:

1. Health-checks the configured gateway URL.
2. For **loopback** URLs only, ensures Hermes has API-server env
   (``API_SERVER_ENABLED`` + ``API_SERVER_KEY``), discovers the ``hermes``
   CLI, and runs ``gateway restart`` / detached ``gateway``.
3. Polls ``/health`` until ready or timeout, with log-based diagnosis.

Remote gateway URLs are never started or restarted by the avatar.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Env overrides (portable; no machine-specific defaults required)
_ENV_CLI = ("HERMES_CLI", "HERMES_COMMAND", "HERMES_BIN")
_ENV_AGENT_DIR = ("HERMES_AGENT_DIR", "HERMES_AGENT_ROOT")
_ENV_HOME = ("HERMES_HOME", "HERMES_DIR")

# Min length Hermes accepts for API_SERVER_KEY (see has_usable_secret min_length=16)
_MIN_API_KEY_LEN = 16


@dataclass
class GatewayEnsureResult:
    ok: bool
    url: str
    action: str  # "already_up" | "started" | "restarted" | "failed" | "skipped_remote"
    detail: str = ""
    hermes_cli: str | None = None
    api_key: str | None = None  # key used / ensured (client should use same)


@dataclass
class GatewayManageOptions:
    """Runtime options for ensure_gateway()."""

    auto_start: bool = True
    auto_restart: bool = True
    startup_timeout_seconds: float = 60.0
    health_timeout_seconds: float = 2.0
    # Optional absolute path or command name; empty = auto-discover
    hermes_command: str | None = None
    # API key for Hermes API server + avatar Authorization header
    api_key: str | None = None
    # When True (default), inject/persist API_SERVER_* so Hermes binds the port
    ensure_api_server: bool = True
    # When True (default), write missing keys into HERMES_HOME/.env (durable)
    write_hermes_env: bool = True
    # Called with human-readable status lines (UI / logs)
    progress: Optional[Callable[[str], None]] = None
    # Extra env for hermes subprocess
    extra_env: dict[str, str] = field(default_factory=dict)


def _progress(opts: GatewayManageOptions, msg: str) -> None:
    logger.info("%s", msg)
    if opts.progress:
        try:
            opts.progress(msg)
        except Exception:
            pass


def is_loopback_url(url: str) -> bool:
    """True if host is localhost / 127.0.0.1 / ::1 (avatar may manage it)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def parse_gateway_url(url: str) -> tuple[str, int]:
    """Return (host, port) with defaults for missing pieces."""
    raw = (url or "").strip() or "http://127.0.0.1:8642"
    p = urlparse(raw)
    host = (p.hostname or "127.0.0.1").strip()
    if p.port:
        port = int(p.port)
    else:
        port = 443 if (p.scheme or "http").lower() == "https" else 8642
    return host, port


def health_check(
    base_url: str,
    *,
    timeout: float = 2.0,
) -> tuple[bool, str]:
    """GET {base}/health. Returns (ok, detail)."""
    url = (base_url or "").rstrip("/")
    if not url:
        return False, "empty gateway URL"
    try:
        # Bypass proxy env for local agent — corporate proxies often break loopback.
        session = requests.Session()
        session.trust_env = False
        r = session.get(f"{url}/health", timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:160]}"
        try:
            body = r.json()
            status = body.get("status") if isinstance(body, dict) else None
            if status and str(status).lower() not in {"ok", "healthy", "up"}:
                return False, f"unexpected health payload: {body!r}"
            return True, str(body) if body is not None else "ok"
        except Exception:
            return True, (r.text or "ok")[:200]
    except requests.exceptions.ConnectionError as exc:
        return False, f"connection refused/unreachable: {exc}"
    except requests.exceptions.Timeout:
        return False, f"health timed out after {timeout}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def wait_until_healthy(
    base_url: str,
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.5,
    health_timeout: float = 2.0,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(1.0, float(timeout))
    last = "not checked"
    while time.monotonic() < deadline:
        ok, last = health_check(base_url, timeout=health_timeout)
        if ok:
            return True, last
        remaining = deadline - time.monotonic()
        if progress:
            try:
                progress(f"Waiting for gateway… ({max(0, int(remaining))}s left)")
            except Exception:
                pass
        time.sleep(poll_interval)
    return False, last


# ---------------------------------------------------------------------------
# Hermes home / .env / API key
# ---------------------------------------------------------------------------

def find_hermes_home() -> Path | None:
    """Locate HERMES_HOME (config.yaml / .env parent)."""
    for env in _ENV_HOME:
        v = (os.environ.get(env) or "").strip()
        if v:
            p = Path(v).expanduser()
            if p.is_dir():
                return p
    local = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    if local:
        candidates.append(Path(local) / "hermes")
    candidates.append(Path.home() / ".hermes")
    candidates.append(Path.home() / "AppData" / "Local" / "hermes")
    for p in candidates:
        if p.is_dir() and (
            (p / "config.yaml").is_file()
            or (p / ".env").is_file()
            or (p / "gateway.pid").is_file()
        ):
            return p
    for p in candidates:
        if p.is_dir():
            return p
    return None


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("export "):
            s = s[7:].strip()
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def read_hermes_api_key(home: Path | None = None) -> str | None:
    """API_SERVER_KEY from process env or HERMES_HOME/.env."""
    for env in ("API_SERVER_KEY", "HERMES_GATEWAY_KEY"):
        v = (os.environ.get(env) or "").strip()
        if len(v) >= _MIN_API_KEY_LEN:
            return v
    h = home or find_hermes_home()
    if h is None:
        return None
    parsed = _parse_dotenv(h / ".env")
    v = (parsed.get("API_SERVER_KEY") or "").strip()
    if len(v) >= _MIN_API_KEY_LEN:
        return v
    return None


def generate_api_key() -> str:
    """Cryptographically strong key acceptable to Hermes startup guard."""
    return secrets.token_hex(32)  # 64 hex chars


def resolve_api_key(preferred: str | None = None) -> str:
    """Pick a usable API key: preferred → Hermes env/.env → generate."""
    for candidate in (
        (preferred or "").strip(),
        (os.environ.get("HERMES_GATEWAY_KEY") or "").strip(),
        (os.environ.get("API_SERVER_KEY") or "").strip(),
        read_hermes_api_key() or "",
    ):
        if len(candidate) >= _MIN_API_KEY_LEN:
            return candidate
    return generate_api_key()


def ensure_hermes_api_server_env(
    *,
    api_key: str,
    host: str = "127.0.0.1",
    port: int = 8642,
    write_dotenv: bool = True,
    home: Path | None = None,
) -> tuple[dict[str, str], str]:
    """Ensure API server env vars; optionally persist into HERMES_HOME/.env.

    Returns (env_updates, status_message).
    """
    key = (api_key or "").strip()
    if len(key) < _MIN_API_KEY_LEN:
        key = generate_api_key()

    # Prefer loopback for desktop avatar
    bind_host = host if host not in {"0.0.0.0", "::", "localhost"} else "127.0.0.1"
    if host == "localhost":
        bind_host = "127.0.0.1"

    updates = {
        "API_SERVER_ENABLED": "true",
        "API_SERVER_KEY": key,
        "API_SERVER_HOST": bind_host,
        "API_SERVER_PORT": str(int(port)),
    }

    # Always set into current process so children inherit even without write.
    for k, v in updates.items():
        os.environ[k] = v

    h = home or find_hermes_home()
    msg_parts = [f"API_SERVER_ENABLED=true port={port} host={bind_host}"]

    if write_dotenv and h is not None:
        env_path = h / ".env"
        existing = _parse_dotenv(env_path)
        need_write: dict[str, str] = {}
        for k, v in updates.items():
            cur = (existing.get(k) or "").strip()
            if k == "API_SERVER_KEY":
                if len(cur) < _MIN_API_KEY_LEN:
                    need_write[k] = v
                else:
                    # Keep Hermes' existing key; avatar must use the same one.
                    updates[k] = cur
                    os.environ[k] = cur
                    key = cur
            elif not cur or cur.lower() in {"false", "0", "no", "off", ""}:
                need_write[k] = v
            elif k == "API_SERVER_PORT" and cur != str(port):
                # Don't silently change a deliberate custom port; prefer existing.
                try:
                    updates[k] = str(int(cur))
                    os.environ[k] = updates[k]
                except ValueError:
                    need_write[k] = v
            elif k == "API_SERVER_ENABLED" and cur.lower() not in {
                "1", "true", "yes", "on",
            }:
                need_write[k] = "true"

        if need_write:
            _append_dotenv(env_path, need_write)
            msg_parts.append(f"wrote {', '.join(need_write)} → {env_path}")
        else:
            msg_parts.append(f"Hermes .env already has API server settings ({env_path})")
    elif write_dotenv and h is None:
        msg_parts.append("HERMES_HOME not found — env applied only for this process")

    return updates, "; ".join(msg_parts)


def _append_dotenv(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block_lines = [
        "",
        "# --- hermes-desktop-avatar: OpenAI-compatible API server (loopback) ---",
    ]
    for k, v in values.items():
        block_lines.append(f"{k}={v}")
    block_lines.append("# --- end hermes-desktop-avatar ---")
    block = "\n".join(block_lines) + "\n"
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        existing = ""
    # Strip a previous avatar-managed block so re-runs don't stack duplicates
    existing = re.sub(
        r"\n?# --- hermes-desktop-avatar:.*?--- end hermes-desktop-avatar ---\n?",
        "\n",
        existing,
        flags=re.DOTALL,
    )
    try:
        path.write_text(existing.rstrip() + "\n" + block, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def diagnose_local_failure(home: Path | None = None) -> str:
    """Best-effort human diagnosis from Hermes logs / state (portable)."""
    h = home or find_hermes_home()
    hints: list[str] = []
    if h is None:
        return (
            "Could not locate HERMES_HOME. Set HERMES_HOME or install Hermes Agent. "
            "API server needs API_SERVER_ENABLED=true and API_SERVER_KEY (≥16 chars)."
        )

    # gateway_state.json
    state_path = h / "gateway_state.json"
    if state_path.is_file():
        try:
            import json

            state = json.loads(state_path.read_text(encoding="utf-8"))
            platforms = state.get("platforms") or {}
            api = platforms.get("api_server") or {}
            if api:
                hints.append(
                    f"gateway_state api_server.state={api.get('state')!r} "
                    f"error={api.get('error_message')!r}"
                )
        except Exception:
            pass

    # Scan recent gateway.log for known API-server errors
    log_path = h / "logs" / "gateway.log"
    if log_path.is_file():
        try:
            # Read last ~64KB only
            data = log_path.read_bytes()
            if len(data) > 65536:
                data = data[-65536:]
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            keywords = (
                "API_SERVER_KEY",
                "api_server",
                "Api_Server",
                "8642",
                "Refusing to start",
                "Address already in use",
            )
            hits = [ln for ln in lines if any(k in ln for k in keywords)]
            for ln in hits[-8:]:
                hints.append(ln.strip()[:220])
        except OSError:
            pass

    env = _parse_dotenv(h / ".env")
    key = (env.get("API_SERVER_KEY") or os.environ.get("API_SERVER_KEY") or "").strip()
    enabled = (
        env.get("API_SERVER_ENABLED")
        or os.environ.get("API_SERVER_ENABLED")
        or ""
    ).strip().lower()
    if len(key) < _MIN_API_KEY_LEN:
        hints.append(
            "API_SERVER_KEY missing/short — Hermes refuses to bind :8642 without it."
        )
    if enabled in {"", "false", "0", "no", "off"} and len(key) < _MIN_API_KEY_LEN:
        hints.append(
            "API_SERVER_ENABLED is not true — gateway may only run messaging (Telegram) "
            "without the OpenAI-compatible HTTP API."
        )

    if not hints:
        return (
            f"Hermes home={h}. No API-server error lines found in recent logs. "
            "Confirm API_SERVER_ENABLED=true and API_SERVER_KEY are set, then "
            "`hermes gateway restart`."
        )
    return " | ".join(hints[:10])


# ---------------------------------------------------------------------------
# CLI discovery / process
# ---------------------------------------------------------------------------

def find_hermes_cli(explicit: str | None = None) -> str | None:
    """Locate a runnable Hermes CLI on any platform."""
    candidates: list[str] = []

    def _add(raw: str | None) -> None:
        if not raw:
            return
        s = str(raw).strip().strip('"')
        if s and s not in candidates:
            candidates.append(s)

    _add(explicit)
    for env in _ENV_CLI:
        _add(os.environ.get(env))

    which = shutil.which("hermes")
    if which:
        _add(which)
    if sys.platform == "win32":
        for name in ("hermes.cmd", "hermes.exe", "hermes.bat"):
            w = shutil.which(name)
            if w:
                _add(w)

    roots: list[Path] = []
    for env in _ENV_AGENT_DIR:
        v = (os.environ.get(env) or "").strip()
        if v:
            roots.append(Path(v))
    home = find_hermes_home()
    if home is not None:
        roots.append(home)
        roots.append(home / "hermes-agent")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "hermes" / "hermes-agent")
    roots.append(Path.home() / ".hermes")
    roots.append(Path.home() / "hermes-agent")

    for root in roots:
        try:
            r = root.expanduser()
        except Exception:
            continue
        for rel in (
            Path("bin") / "hermes",
            Path("Scripts") / "hermes.exe",
            Path("Scripts") / "hermes.cmd",
            Path(".venv") / "Scripts" / "hermes.exe",
            Path("venv") / "Scripts" / "hermes.exe",
            Path(".venv") / "bin" / "hermes",
            Path("venv") / "bin" / "hermes",
            Path("hermes"),
        ):
            p = r / rel
            if p.is_file():
                _add(str(p))

    for c in candidates:
        path = Path(c)
        try:
            if path.is_file():
                return str(path.resolve())
        except Exception:
            if path.is_file():
                return str(path)
        resolved = shutil.which(c)
        if resolved:
            return resolved
    return None


def _hermes_argv(hermes_cli: str, args: list[str]) -> list[str]:
    if sys.platform == "win32":
        suffix = Path(hermes_cli).suffix.lower()
        if suffix in {".cmd", ".bat"}:
            return ["cmd.exe", "/c", hermes_cli, *args]
    return [hermes_cli, *args]


def _run_hermes(
    hermes_cli: str,
    args: list[str],
    *,
    timeout: float | None = 120.0,
    detached: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run ``hermes …``; return (returncode, combined output snippet)."""
    cmd = _hermes_argv(hermes_cli, args)
    logger.info("running: %s", " ".join(cmd))

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    creationflags = 0
    kwargs: dict = {
        "args": cmd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": run_env,
    }

    if sys.platform == "win32":
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            kwargs["close_fds"] = True
        kwargs["creationflags"] = creationflags
    else:
        if detached:
            kwargs["start_new_session"] = True
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL

    try:
        if detached:
            subprocess.Popen(**kwargs)  # noqa: S603
            return 0, "detached spawn"
        completed = subprocess.run(  # noqa: S603
            **kwargs,
            timeout=timeout,
        )
        out = (completed.stdout or "").strip()
        return int(completed.returncode), out[-2000:] if out else ""
    except subprocess.TimeoutExpired as exc:
        out = ""
        if exc.stdout:
            out = str(exc.stdout)[-2000:]
        return 124, out or "command timed out"
    except FileNotFoundError:
        return 127, f"executable not found: {hermes_cli}"
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def _try_restart(
    hermes_cli: str,
    opts: GatewayManageOptions,
    env: dict[str, str],
) -> tuple[bool, str]:
    _progress(opts, "Restarting local Hermes gateway…")
    code, out = _run_hermes(
        hermes_cli,
        ["gateway", "restart"],
        timeout=max(30.0, opts.startup_timeout_seconds),
        detached=False,
        env=env,
    )
    if code == 0:
        return True, out or "gateway restart ok"
    _progress(opts, "restart failed; trying gateway run --replace…")
    code2, out2 = _run_hermes(
        hermes_cli,
        ["gateway", "run", "--replace"],
        timeout=max(30.0, opts.startup_timeout_seconds),
        detached=False,
        env=env,
    )
    if code2 == 0:
        return True, out2 or "gateway run --replace ok"
    return (
        False,
        f"restart rc={code}: {out or '(no output)'}; "
        f"replace rc={code2}: {out2 or '(no output)'}",
    )


def _try_start_detached(
    hermes_cli: str,
    opts: GatewayManageOptions,
    env: dict[str, str],
) -> tuple[bool, str]:
    _progress(opts, "Starting Hermes gateway in background…")
    code, out = _run_hermes(
        hermes_cli,
        ["gateway"],
        timeout=None,
        detached=True,
        env=env,
    )
    if code == 0:
        return True, out or "gateway spawned"
    return False, out or f"spawn failed rc={code}"


def ensure_gateway(
    base_url: str,
    options: GatewayManageOptions | None = None,
) -> GatewayEnsureResult:
    """Make the gateway API healthy if possible.

    Returns a result; does not raise. Callers map ``ok=False`` to UI errors.
    """
    opts = options or GatewayManageOptions()
    url = (base_url or "").rstrip("/") or "http://127.0.0.1:8642"
    host, port = parse_gateway_url(url)

    ok, detail = health_check(url, timeout=opts.health_timeout_seconds)
    if ok:
        _progress(opts, f"Gateway already healthy at {url}")
        key = resolve_api_key(opts.api_key)
        return GatewayEnsureResult(True, url, "already_up", detail, api_key=key)

    if not is_loopback_url(url):
        return GatewayEnsureResult(
            False,
            url,
            "skipped_remote",
            f"Gateway at {url} is not local; avatar cannot start it. "
            f"Last health error: {detail}",
        )

    if not opts.auto_start and not opts.auto_restart:
        return GatewayEnsureResult(
            False,
            url,
            "failed",
            f"Gateway not reachable ({detail}). "
            "Enable hermes.auto_start / auto_restart in settings, "
            "or start the Hermes gateway yourself.",
        )

    # --- Ensure API server can bind (root cause of "wait 45s then fail") ---
    api_key = resolve_api_key(opts.api_key)
    inject_env: dict[str, str] = dict(opts.extra_env or {})
    if opts.ensure_api_server:
        updates, ensure_msg = ensure_hermes_api_server_env(
            api_key=api_key,
            host=host,
            port=port,
            write_dotenv=opts.write_hermes_env,
        )
        api_key = updates.get("API_SERVER_KEY", api_key)
        inject_env.update(updates)
        _progress(opts, f"API server config: {ensure_msg}")

    hermes_cli = find_hermes_cli(opts.hermes_command)
    if not hermes_cli:
        diag = diagnose_local_failure()
        return GatewayEnsureResult(
            False,
            url,
            "failed",
            f"Gateway not reachable ({detail}) and the `hermes` CLI was not found. "
            "Install Hermes Agent, ensure `hermes` is on PATH, or set "
            "HERMES_CLI / hermes.hermes_command. "
            f"Diagnosis: {diag}",
            hermes_cli=None,
            api_key=api_key,
        )

    _progress(opts, f"Using Hermes CLI: {hermes_cli}")

    action = "failed"
    manage_detail = detail

    if opts.auto_restart:
        restarted, msg = _try_restart(hermes_cli, opts, inject_env)
        manage_detail = msg
        if restarted:
            action = "restarted"
        elif opts.auto_start:
            started, msg2 = _try_start_detached(hermes_cli, opts, inject_env)
            manage_detail = f"{msg}; start: {msg2}"
            if started:
                action = "started"
    elif opts.auto_start:
        started, msg = _try_start_detached(hermes_cli, opts, inject_env)
        manage_detail = msg
        if started:
            action = "started"

    if action == "failed":
        diag = diagnose_local_failure()
        return GatewayEnsureResult(
            False,
            url,
            "failed",
            f"Could not start/restart Hermes gateway via `{hermes_cli}`. "
            f"Health: {detail}. Manage: {manage_detail}. Diagnosis: {diag}",
            hermes_cli=hermes_cli,
            api_key=api_key,
        )

    wait_s = float(opts.startup_timeout_seconds or 60.0)
    _progress(opts, f"Waiting up to {int(wait_s)}s for {url}/health …")
    healthy, health_detail = wait_until_healthy(
        url,
        timeout=wait_s,
        health_timeout=opts.health_timeout_seconds,
        progress=opts.progress,
    )
    if healthy:
        _progress(opts, f"Gateway ready ({action}): {health_detail}")
        return GatewayEnsureResult(
            True, url, action, health_detail, hermes_cli=hermes_cli, api_key=api_key
        )

    diag = diagnose_local_failure()
    return GatewayEnsureResult(
        False,
        url,
        "failed",
        f"Hermes CLI ran ({action}) but {url}/health never became ready. "
        f"Last error: {health_detail}. "
        f"Diagnosis: {diag}. "
        f"Manage output: {manage_detail}",
        hermes_cli=hermes_cli,
        api_key=api_key,
    )
