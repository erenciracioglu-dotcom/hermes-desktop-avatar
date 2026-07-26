"""Batch-build sprite frames from a directory of chroma-keyed videos.

Pipeline (sprite build helpers for hermes-desktop-avatar; kept
self-contained here so the avatar project owns its sprite lifecycle):

    video.mp4
      └── ffmpeg (chroma key + downscale + AA) →
            <set_id>/chroma_keyed/talk_*.png
      └── apply_v3_to_png →
            <set_id>/chroma_keyed_aa_v3/talk_*.png
      └── merge →
            <mascot>/<state>_<idx>.png  (zero-padded 4-digit index)

The script defaults to dry-run mode (DRY_RUN env or --dry flag) — it
prints every step it WOULD take without writing anything.  Pass
--commit to actually run the pipeline.

Set-id convention: the script derives ``set_id`` from each filename via
``avatar.idle_animator.parse_rarity_from_filename`` so that a/b/c tiers
map consistently to the rarity tier.

This script never touches the live mascot
folder; everything writes under the avatar project's ``assets/sprites/``
tree.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Repo layout:
#   <avatar>/scripts/batch_build_sprites.py
#   <avatar>/assets/sprites/character_sets/<set_id>/chroma_keyed/
#   <avatar>/assets/sprites/character_sets/<set_id>/chroma_keyed_aa_v3/
#   <avatar>/assets/sprites/mascot/<state>_<idx>.png
HERE = Path(__file__).resolve().parent
AVATAR_ROOT = HERE.parent
ASSETS = AVATAR_ROOT / "assets" / "sprites"
SETS_BASE = ASSETS / "character_sets"
MASCOT_DIR = ASSETS / "mascot"

# Make the avatar package importable so we can reuse the rarity parser.
sys.path.insert(0, str(AVATAR_ROOT / "src"))
from avatar.idle_animator import parse_rarity_from_filename  # noqa: E402

# --------------------------------------------------------------------- ffmpeg
# Same chroma key pipeline as the original build_v3_from_video helper.
# 0x0f4477 = (15, 68, 115) blue screen target.
_FILTER_CHAIN = (
    "scale=320:568:flags=lanczos,unsharp=3:3:0.5:3:3:0.0,"
    "chromakey=0x0f4477:0.15:0.40,"
    "format=yuva420p,"
    "split[main][alpha];"
    "[alpha]boxblur=1:1[ablur];"
    "[ablur]format=gray,lut=c0='if(lt(val,40),0,if(gt(val,210),255,val))'[athr];"
    "[main][athr]alphamerge"
)

AA_BLUR_RADIUS = 1.0
AA_THRESHOLD_HI = 210
AA_THRESHOLD_LO = 40


def _set_id_from_video(path: Path) -> str:
    """Map a video filename to its character_sets/ set_id.

    Examples:
      nora-idle-a-sigh0.mp4  -> nora_idle_a_sigh0
      nora-thinking1.mp4     -> nora_thinking1
      Nora_Talking1.mp4      -> nora_talking1
    """
    norm = path.stem.lower().replace(" ", "-").replace("_", "-")
    return norm.replace("-", "_")


def _state_name_from_set(set_id: str) -> str:
    """Convert set_id back to a state prefix for the mascot/ folder.

    Convention is "<state>_<idx>.png" where state mirrors the tier
    letter and the variant description.
    """
    return set_id  # already in the right shape


def _detect_chroma_hex(video: Path, sample_seconds: float = 5.0) -> str:
    """Sample a single frame and return the dominant blue-screen color.

    The avatar's source videos use a green/blue screen background; this
    helper finds the most common near-blue pixel and returns its 6-hex
    color in ffmpeg's ``0xRRGGBB`` format.  Falls back to the legacy
    ``0x0f4477`` if sampling fails.
    """
    import numpy as np
    from collections import Counter
    from PIL import Image
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "sample.png"
        cmd = [
            "ffmpeg", "-y", "-ss", str(sample_seconds), "-i", str(video),
            "-frames:v", "1", str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            return "0x0f4477"
        img = Image.open(out).convert("RGB")
        arr = np.array(img)
        # pixels that are clearly blue (B > R+30 AND B > G+30)
        mask = (arr[:, :, 2].astype(int) - arr[:, :, 0].astype(int) > 30) & \
               (arr[:, :, 2].astype(int) - arr[:, :, 1].astype(int) > 30)
        if not mask.any():
            return "0x0f4477"
        blue = arr[mask]
        # mode of subsampled blue pixels (much faster than full mode)
        subsample = blue[::100]
        tuples = [tuple(int(c) for c in p) for p in subsample]
        top = Counter(tuples).most_common(1)[0][0]
        r_, g_, b_ = top
        return f"0x{r_:02x}{g_:02x}{b_:02x}"


def _make_filter_chain(chroma_hex: str) -> str:
    """Build the ffmpeg filter chain for a given chroma key color."""
    return (
        "scale=320:568:flags=lanczos,unsharp=3:3:0.5:3:3:0.0,"
        f"chromakey={chroma_hex}:0.15:0.40,"
        "format=yuva420p,"
        "split[main][alpha];"
        "[alpha]boxblur=1:1[ablur];"
        "[ablur]format=gray,lut=c0='if(lt(val,40),0,if(gt(val,210),255,val))'[athr];"
        "[main][athr]alphamerge"
    )


def _run_ffmpeg(video: Path, out_dir: Path, dry_run: bool, chroma_hex: str | None = None) -> Path | None:
    """Run ffmpeg to extract chroma-keyed frames as PNG sequence.

    Returns the output directory on success, None on ffmpeg failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    hex_value = chroma_hex or _detect_chroma_hex(video)
    filter_chain = _make_filter_chain(hex_value)
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vf", filter_chain,
        "-vsync", "0",
        "-frame_pts", "1",
        str(out_dir / "talk_%04d.png"),
    ]
    if dry_run:
        print(f"    [dry] ffmpeg → {out_dir}  (chroma={hex_value})")
        return out_dir
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [FAIL] ffmpeg exited {r.returncode}  chroma={hex_value}")
        print(f"    stderr tail: {r.stderr[-400:]}")
        return None
    print(f"    ffmpeg OK  chroma={hex_value}")
    return out_dir


def _encode_webp(in_dir: Path, out_dir: Path, quality: int = 95, dry_run: bool = False) -> Path | None:
    """Encode a PNG sequence into a single animated WebP.

    The output filename is derived from the parent directory name
    (``<set_id>/chroma_keyed_aa_v3/talk_*.png`` -> ``<set_id>.webp``).
    Returns the output WebP path on success, None on failure.
    """
    from PIL import Image
    set_id = in_dir.parent.name
    out_path = out_dir / f"{set_id}.webp"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = sorted(in_dir.glob("talk_*.png"))
    if not frames:
        print(f"    [SKIP] no talk_*.png in {in_dir}")
        return None
    if dry_run:
        print(f"    [dry] webp encode {len(frames)} frames → {out_path} (q={quality})")
        return out_path
    try:
        images = [Image.open(p).convert("RGBA") for p in frames]
        # animated WebP: duration is per-frame ms.  Avatar uses 24fps so
        # ~41ms/frame.  lossless=False, quality=user-configurable.
        images[0].save(
            str(out_path),
            format="WEBP",
            save_all=True,
            append_images=images[1:],
            duration=42,
            loop=0,
            lossless=False,
            quality=quality,
        )
        print(f"    webp OK  → {out_path}  ({out_path.stat().st_size // 1024} KB, q={quality})")
        return out_path
    except Exception as e:
        print(f"    [FAIL] webp encode {out_path}: {e}")
        return None


def _apply_v3_aa(in_dir: Path, out_dir: Path, dry_run: bool) -> int:
    """Apply Gaussian blur + threshold to chroma_keyed/talk_*.png.

    Uses PIL since we need a numpy-free path that mirrors
    scripts/apply_v3_aa.py.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageFilter
    import numpy as np
    frames = sorted(in_dir.glob("talk_*.png"))
    if not frames:
        print(f"    [SKIP] no talk_*.png in {in_dir}")
        return 0
    if dry_run:
        print(f"    [dry] apply AA to {len(frames)} frames → {out_dir}")
        return len(frames)
    for src in frames:
        img = Image.open(src).convert("RGBA")
        a = img.split()[3].filter(ImageFilter.GaussianBlur(radius=AA_BLUR_RADIUS))
        arr = np.array(a)
        # threshold
        lut = np.where(arr < AA_THRESHOLD_LO, 0,
               np.where(arr > AA_THRESHOLD_HI, 255, arr)).astype("uint8")
        img.putalpha(Image.fromarray(lut, mode="L"))
        img.save(out_dir / src.name)
    return len(frames)


def _merge_to_mascot(set_id: str, state_name: str, dry_run: bool) -> int:
    """Copy chroma_keyed_aa_v3/talk_*.png into mascot/<state>_<idx>.png.

    NOTE: keeps the avatar's existing mascot/ files untouched for other
    sets — only deletes the prefix `<state>_*` before merging.
    """
    src_dir = SETS_BASE / set_id / "chroma_keyed_aa_v3"
    if not src_dir.is_dir():
        print(f"    [SKIP] {src_dir} not found")
        return 0
    if dry_run:
        # count without touching
        n = len(list(src_dir.glob("talk_*.png")))
        print(f"    [dry] merge {n} frames → mascot/{state_name}_<idx>.png")
        return n
    # Remove existing files for this state only
    removed = 0
    for old in MASCOT_DIR.glob(f"{state_name}_*.png"):
        old.unlink()
        removed += 1
    if removed:
        print(f"    cleared {removed} old {state_name}_*.png")
    src_files = sorted(src_dir.glob("talk_*.png"))
    for i, src in enumerate(src_files):
        dst = MASCOT_DIR / f"{state_name}_{i}.png"
        shutil.copy2(src, dst)
    return len(src_files)


def process_video(video: Path, dry_run: bool, output_format: str = "png") -> dict[str, int]:
    """Run the full pipeline for a single video.  Returns counts per stage.

    output_format: "png" keeps the existing PNG sequence output.  "webp"
    adds a final step that encodes chroma_keyed_aa_v3/talk_*.png into a
    single animated WebP at the sprite's mascot/ location.
    """
    set_id = _set_id_from_video(video)
    state_name = _state_name_from_set(set_id)
    rarity = parse_rarity_from_filename(video.name)
    print(f"\n[{video.name}]")
    print(f"  set_id={set_id}  state={state_name}  rarity={rarity}")

    ck_dir = SETS_BASE / set_id / "chroma_keyed"
    aa_dir = SETS_BASE / set_id / "chroma_keyed_aa_v3"

    result = {"ffmpeg": 0, "aa": 0, "merge": 0, "webp": 0}

    ffmpeg_out = _run_ffmpeg(video, ck_dir, dry_run)
    if ffmpeg_out is None:
        return result
    n_ck = len(list(ffmpeg_out.glob("talk_*.png"))) if not dry_run else 0
    result["ffmpeg"] = n_ck

    n_aa = _apply_v3_aa(ck_dir, aa_dir, dry_run)
    result["aa"] = n_aa

    n_merge = _merge_to_mascot(set_id, state_name, dry_run)
    result["merge"] = n_merge

    if output_format == "webp":
        # Animated WebP goes directly into mascot/ (alongside any PNG
        # sequence from the merge step).  This lets the sprite loader
        # pick it up via _find_sprite_frames (WebP preferred).
        webp_out = _encode_webp(aa_dir, MASCOT_DIR, dry_run=dry_run)
        if webp_out is not None:
            result["webp"] = 1

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "videos_dir",
        type=Path,
        help="Directory of .mp4 videos to process",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually run the pipeline (default is dry-run).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N videos (0 = all).  Useful for testing.",
    )
    parser.add_argument(
        "--format",
        choices=("png", "webp"),
        default="png",
        help=(
            "Output format for sprite assets. 'png' produces a PNG sequence "
            "(legacy). 'webp' additionally encodes the chroma_keyed_aa_v3 "
            "frames into a single animated WebP per sprite and writes it to "
            "mascot/<set_id>.webp.  The sprite loader prefers WebP when "
            "both formats exist."
        ),
    )
    args = parser.parse_args()

    if not args.videos_dir.is_dir():
        print(f"ERROR: {args.videos_dir} is not a directory")
        return 2

    videos = sorted(args.videos_dir.glob("*.mp4"))
    if args.limit > 0:
        videos = videos[: args.limit]

    dry_run = not args.commit
    print(f"Videos to process: {len(videos)}")
    print(f"Mode: {'DRY RUN' if dry_run else 'COMMIT (real writes)'}")
    print(f"Format: {args.format}")
    print(f"SETS_BASE = {SETS_BASE}")
    print(f"MASCOT_DIR = {MASCOT_DIR}")
    print()

    totals = {"ffmpeg": 0, "aa": 0, "merge": 0, "webp": 0}
    for v in videos:
        r = process_video(v, dry_run=dry_run, output_format=args.format)
        for k in totals:
            totals[k] += r[k]

    print()
    print("=" * 50)
    print(f"Total frames: ffmpeg={totals['ffmpeg']}  aa={totals['aa']}  merge={totals['merge']}  webp={totals['webp']}")
    if dry_run:
        print("(dry run — re-run with --commit to actually write files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
