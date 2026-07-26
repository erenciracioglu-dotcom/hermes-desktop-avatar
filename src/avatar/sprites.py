"""Load mascot frames (pixel sheets sliced into state sequences).

Supports both PNG sequence (legacy) and animated WebP (preferred for
new pipeline — CorridorKey output).  Loader auto-detects file format by
extension; PNG fallback retained so existing sprite sets keep working.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any  # noqa: F401 — used in load_frames_for_preset

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from .paths import sprites_dir

logger = logging.getLogger(__name__)

# Primary states used by the app
STATES = ("idle", "think", "talk", "sleep", "dance")
# Extra ambient clips for idle waiting (everything except talk/dialogue)
# sleep is not in the ambient pool (no dedicated sleep sheet; idle fallback
# made long idle loops). sleep is only a forced controller state.
AMBIENT_STATES = ("idle", "walk", "think", "attack", "dance")
# v3 AA sets are also loaded
V3_STATES = (
    "bored1_v3", "bored2_v3",
    "dancing1_v3", "dancing2_v3",
    "double_chat_blue_v3",
    "thinking1_v3", "thinking2_v3",
)
ALL_LOAD_STATES = ("idle", "walk", "talk", "think", "sleep", "attack", "dance") + V3_STATES

MAX_DISPLAY_HEIGHT = 320


def ensure_placeholder_sprites() -> Path:
    d = sprites_dir()
    d.mkdir(parents=True, exist_ok=True)
    # PNG placeholder remains (placeholder only — real sprites are PNG or WebP)
    if any(d.glob("idle_*.png")):
        return d
    for state in STATES:
        for i in range(2):
            path = d / f"{state}_{i}.png"
            if not path.is_file():
                _write_placeholder(path, state, i)
    return d


# Image extensions accepted by the sprite loader.  Listed in priority
# order for the fallback scanner; animated WebP is detected first.
SPRITE_EXTS = ("webp", "png")


def _asset_search_dirs(preset=None) -> list[Path]:
    """Directories to search for sprite files for a preset.

    Pack characters use extracted ``asset_root`` (and ``clips/`` subdir).
    Built-ins use the shared mascot folder.
    """
    dirs: list[Path] = []
    if preset is not None:
        root = getattr(preset, "asset_root", None)
        if root is not None:
            root = Path(root)
            clips = root / "clips"
            if clips.is_dir():
                dirs.append(clips)
            dirs.append(root)
        extra = getattr(preset, "extra_asset_roots", None) or []
        for er in extra:
            er = Path(er)
            clips = er / "clips"
            if clips.is_dir():
                dirs.append(clips)
            dirs.append(er)
        # Pack folder itself (directory packs with local clips/)
        pack = getattr(preset, "pack_path", None)
        if pack is not None:
            pack = Path(pack)
            if pack.is_dir():
                pclips = pack / "clips"
                if pclips.is_dir():
                    dirs.append(pclips)
                dirs.append(pack)
    dirs.append(sprites_dir())
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _find_sprite_frames(d: Path, prefix: str) -> list[Path]:
    """Find all sprite frames for ``prefix`` under ``d``.

    Prefers animated WebP over PNG (new pipeline outputs WebP).  When
    a WebP exists for the prefix, PNG sequence is skipped entirely
    (WebP is the single animated source).  Returns frame paths in
    lexical order so animation timing is preserved.
    """
    out: list[Path] = []
    # WebP first — single animated file means we use it directly.
    webp = d / f"{prefix}.webp"
    if webp.is_file():
        return [webp]
    # Also allow nested clips/ when d is pack root
    webp2 = d / "clips" / f"{prefix}.webp"
    if webp2.is_file():
        return [webp2]
    # PNG sequence (zero-padded, lexical order) — fallback only
    out.extend(sorted(d.glob(f"{prefix}_*.png")))
    if not out:
        clips = d / "clips"
        if clips.is_dir():
            out.extend(sorted(clips.glob(f"{prefix}_*.png")))
    return out


def _find_sprite_frames_for_preset(preset, prefix: str) -> list[Path]:
    for d in _asset_search_dirs(preset):
        found = _find_sprite_frames(d, prefix)
        if found:
            return found
    return []


def _load_webp_frames(path: Path, lazy: bool = False) -> list[QPixmap]:
    """Decode an animated WebP into QPixmap frames using Pillow.

    Pillow's Image.open() + .seek() handles animated WebP natively.
    We materialise every frame eagerly because the sprite player
    indexes by frame number and seeks back/forward frequently.

    If ``lazy`` is True, only the first frame is materialised; later
    frames are decoded on demand via ``LazySpriteSequence``.  This
    reduces startup time for large sprite sets (3000+ frames).
    """
    from PIL import Image
    if lazy:
        # Lazy path: caller gets a lazy sequence that decodes on seek
        try:
            img = Image.open(str(path))
            n_frames = getattr(img, "n_frames", 1)
            img.seek(0)
            first_frame = img.convert("RGBA")
            try:
                from PIL.ImageQt import ImageQt
                qimg = ImageQt(first_frame)
            except ImportError:
                data = first_frame.tobytes("raw", "RGBA")
                w, h = first_frame.size
                qimg = QImage(data, w, h, QImage.Format.Format_RGBA8888).copy()
            first_pix = QPixmap.fromImage(qimg)
            logger.info("WebP %s: lazy %d frames (first decoded)", path.name, n_frames)
            return _LazySpriteSequence(path, n_frames, first_pix)
        except Exception as e:
            logger.error("WebP lazy init failed for %s: %s", path, e)
            return []

    pixmaps: list[QPixmap] = []
    try:
        img = Image.open(str(path))
        n_frames = getattr(img, "n_frames", 1)
        for i in range(n_frames):
            img.seek(i)
            # Convert to RGBA so alpha channel survives
            frame = img.convert("RGBA")
            # Use ImageQt for safe PIL→QImage conversion.  ImageQt owns
            # its own buffer copy, so the QPixmap survives garbage
            # collection of the underlying PIL Image.
            try:
                from PIL.ImageQt import ImageQt
                qimg = ImageQt(frame)
            except ImportError:
                # Fallback: manual QImage construction.  We MUST hold a
                # reference to the bytes buffer for the lifetime of the
                # QImage, otherwise QPainter will paint garbage.
                data = frame.tobytes("raw", "RGBA")
                w, h = frame.size
                qimg = QImage(data, w, h, QImage.Format.Format_RGBA8888)
                # .copy() forces QImage to allocate its own buffer so
                # the source bytes can be freed.
                qimg = qimg.copy()
            pixmaps.append(QPixmap.fromImage(qimg))
        img.close()
        logger.info("WebP %s: %d frames decoded", path.name, len(pixmaps))
    except Exception as e:
        logger.error("WebP decode failed for %s: %s", path, e)
    return pixmaps


class _LazySpriteSequence:
    """List-compatible sequence that decodes WebP frames on access.

    Behaves like a list of QPixmap for sprite_player/index access
    patterns.  First frame is held eagerly (already decoded); later
    frames are decoded when indexed for the first time and cached.
    """

    __slots__ = ("_path", "_n_frames", "_first", "_cache", "_img_ref")

    def __init__(self, path: Path, n_frames: int, first_pix: QPixmap):
        self._path = path
        self._n_frames = n_frames
        self._first = first_pix
        self._cache: dict[int, QPixmap] = {0: first_pix}
        # Keep Image open so .seek() works without reopening the file
        from PIL import Image
        self._img_ref = Image.open(str(path))

    def __len__(self) -> int:
        return self._n_frames

    def __getitem__(self, idx: int) -> QPixmap:
        if idx < 0:
            idx += self._n_frames
        if not 0 <= idx < self._n_frames:
            raise IndexError(f"frame {idx} out of range [0, {self._n_frames})")
        if idx in self._cache:
            return self._cache[idx]
        # Decode frame on demand
        try:
            self._img_ref.seek(idx)
            frame = self._img_ref.convert("RGBA")
            try:
                from PIL.ImageQt import ImageQt
                qimg = ImageQt(frame)
            except ImportError:
                data = frame.tobytes("raw", "RGBA")
                w, h = frame.size
                qimg = QImage(data, w, h, QImage.Format.Format_RGBA8888).copy()
            pix = QPixmap.fromImage(qimg)
        except Exception as e:
            logger.error("WebP lazy frame %d failed for %s: %s", idx, self._path, e)
            pix = self._first  # fallback to first frame
        self._cache[idx] = pix
        return pix

    def __iter__(self):
        for i in range(self._n_frames):
            yield self[i]


def _load_path(path: Path, lazy: bool = False) -> list[QPixmap]:
    """Load a sprite asset: animated WebP (preferred) or single PNG."""
    if path.suffix.lower() == ".webp":
        return _load_webp_frames(path, lazy=lazy)
    # Single PNG (or first PNG of a sequence — caller iterates indices)
    pix = _load_scaled(path)
    return [pix] if pix is not None else []


def _write_placeholder(path: Path, state: str, frame: int) -> None:
    size = 96
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    body = QColor(80, 180, 255)
    p.setBrush(body)
    p.setPen(QColor(20, 40, 60))
    p.drawEllipse(18, 22, 60, 58)
    p.end()
    img.save(str(path))


def _load_scaled(path: Path) -> QPixmap | None:
    pix = QPixmap(str(path))
    if pix.isNull():
        logger.warning("QPixmap null: %s (file size=%d)", path.name, path.stat().st_size)
        return None
    # Load sprites at original size; scale once in widget paint.
    # Double-scaling (shrink here + enlarge in paint) loses quality each step.
    # kalite kaybettirir. Tek seferlik widget-paint scale en temizidir.
    return pix


def load_frames() -> dict[str, list[QPixmap]]:
    """Legacy loader: load every state in ALL_LOAD_STATES (env laziness)."""
    import os
    lazy = os.environ.get("AVATAR_LAZY_SPRITE", "1") != "0"
    ensure_placeholder_sprites()
    d = sprites_dir()
    frames: dict[str, Any] = {}
    for state in ALL_LOAD_STATES:
        paths = _find_sprite_frames(d, state)
        if not paths:
            frames[state] = []
            continue
        # Only use WebP for the new animated pipeline (PNG sequence
        # from old sprite-loader is intentionally ignored here too).
        if paths[0].suffix.lower() != ".webp":
            frames[state] = []
            continue
        if lazy:
            seq = _load_webp_frames(paths[0], lazy=True)
            if isinstance(seq, _LazySpriteSequence):
                frames[state] = seq
            else:
                frames[state] = []
        else:
            pixs = _load_path(paths[0], lazy=False)
            frames[state] = pixs
        if frames[state]:
            logger.info(
                "Loaded %s × %s frames (from %s, lazy=%s)",
                state, len(frames[state]), paths[0].name, lazy,
            )
    # Fallbacks so UI never breaks
    if not frames.get("idle"):
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor(80, 180, 255))
        frames["idle"] = [QPixmap.fromImage(img)]
    for state in STATES:
        if not frames.get(state):
            idle = frames.get("idle")
            if idle is not None:
                frames[state] = list(idle) if isinstance(idle, list) else [idle]
    return frames


def load_frames_for_preset(preset, lazy: bool | None = None) -> dict[str, list[QPixmap]]:
    """Load sprites for a character preset.

    For each state, load files matching prefixes in ``sprite_map``.
    Multiple prefixes become multiple clips (overlay picks / round-robins).

    Animated WebP is preferred (CorridorKey pipeline).

    If ``lazy`` is True, WebP frames decode on demand. Default
    (lazy=None) uses AVATAR_LAZY_SPRITE: "0" = eager, else lazy=True.
    """
    import os
    if lazy is None:
        lazy = os.environ.get("AVATAR_LAZY_SPRITE", "1") != "0"
    frames: dict[str, list[QPixmap] | _LazySpriteSequence] = {}
    search = _asset_search_dirs(preset)
    logger.info(
        "Preset %s: asset search %s",
        getattr(preset, "id", "?"),
        [str(p) for p in search],
    )

    # Load idle ambient prefixes first. In lazy mode each prefix stays a
    # separate LazySpriteSequence; the player walks them as ambient states.
    if preset.idle_ambient:
        if lazy:
            lazy_seqs: list[_LazySpriteSequence] = []
            for prefix in preset.idle_ambient:
                paths = _find_sprite_frames_for_preset(preset, prefix)
                # Skip missing WebP (legacy PNG sequences are not used here)
                if not paths:
                    logger.debug("skip %s (no sprite file)", prefix)
                    continue
                if paths[0].suffix.lower() != ".webp":
                    logger.debug("skip %s (no WebP, only PNG sequence)", prefix)
                    continue
                seq = _load_webp_frames(paths[0], lazy=True)
                if isinstance(seq, _LazySpriteSequence):
                    lazy_seqs.append(seq)
            frames["idle"] = lazy_seqs  # type: ignore[assignment]
            logger.info("Preset %s: idle_ambient -> %d lazy sequences (round-robin)",
                        preset.id, len(lazy_seqs))
        else:
            idle_pixs: list[QPixmap] = []
            for prefix in preset.idle_ambient:
                paths = _find_sprite_frames_for_preset(preset, prefix)
                if not paths:
                    continue
                if paths[0].suffix.lower() != ".webp":
                    continue
                pixs = _load_path(paths[0], lazy=False)
                idle_pixs.extend(pixs)
            frames["idle"] = idle_pixs
            logger.info("Preset %s: idle_ambient -> %d frame (round-robin, eager)",
                        preset.id, len(idle_pixs))

    # Load every sprite_map prefix per state; multiple clips → list for
    # random pick in forced mode (talk1/talk2, thinking1/2/3).
    for state, prefixes in preset.sprite_map.items():
        if state in ("idle", "playful", "rare"):
            continue  # idle_ambient / ambient tiers — handled above
        if not prefixes:
            continue
        loaded_seqs: list[Any] = []
        for prefix in prefixes:
            paths = _find_sprite_frames_for_preset(preset, prefix)
            if not paths or paths[0].suffix.lower() != ".webp":
                # Built-in Jenny still uses PNG sequences for some states
                if paths:
                    if lazy:
                        # Eager single-path lists for non-WebP
                        pixs = _load_path(paths[0], lazy=False)
                        if pixs:
                            loaded_seqs.append(pixs)
                    else:
                        pixs = _load_path(paths[0], lazy=False)
                        if pixs:
                            loaded_seqs.append(pixs)
                else:
                    logger.debug("skip %s/%s (no sprite file)", state, prefix)
                continue
            if lazy:
                seq = _load_webp_frames(paths[0], lazy=True)
                if isinstance(seq, _LazySpriteSequence):
                    loaded_seqs.append(seq)
            else:
                pixs = _load_path(paths[0], lazy=False)
                if pixs:
                    loaded_seqs.append(pixs)
        if not loaded_seqs:
            logger.debug("skip state %s (no sequences)", state)
            continue
        if len(loaded_seqs) == 1:
            frames[state] = loaded_seqs[0]
        else:
            frames[state] = loaded_seqs  # type: ignore[assignment]
        logger.info(
            "Preset %s: %s -> %d clip(s) (lazy=%s)",
            preset.id, state, len(loaded_seqs), lazy,
        )

    # Fallback: ensure idle exists
    if not frames.get("idle"):
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor(80, 180, 255))
        frames["idle"] = [QPixmap.fromImage(img)]
    idle_fallback = frames["idle"]
    if isinstance(idle_fallback, list) and idle_fallback and not isinstance(
        idle_fallback[0], QPixmap
    ):
        # list of sequences → first sequence as fallback frames
        idle_one = idle_fallback[0]
    else:
        idle_one = idle_fallback
    for state in STATES:
        if not frames.get(state):
            frames[state] = idle_one
    return frames


def ambient_states_available(frames: dict[str, list[QPixmap]]) -> list[str]:
    return [s for s in AMBIENT_STATES if frames.get(s)]
