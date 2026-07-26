"""AlphaHint gradient alpha kullanarak transparan WebP üret — V2 (test amaçlı).

Aynı iş mantığı reprocess_alphahint.py ile, fark:
  - Output mascot_v2/ klasörüne yazılır (mevcut mascot/ dokunulmaz)
  - Tek video testi için --video <dir_name> argümanı kabul eder
  - Tüm PRESETS yerine tek set çalıştırılabilir

Trim uygulanmaz — video sonu olduğu gibi WebP'ye yazılır. 17 sprite'ın
tamamı için trim'siz davranış.
"""
import sys, os, subprocess, argparse
from pathlib import Path

# avatar venv'i hermes-agent'tan izole et
_sys_clean = [p for p in sys.path if 'hermes-agent' not in p]
if _sys_clean != sys.path:
    sys.path[:] = _sys_clean

from PIL import Image

MASCOT_V2 = Path(r"E:\Projects\hermes-desktop-avatar\assets\sprites\mascot_v2")
CLIPS     = Path(r"E:\corridor_key\EZ-CorridorKey\ClipsForInference")
FPS       = 24


def reprocess_with_gradient(set_id: str, source_dir: Path, out_dir: Path) -> bool:
    """AlphaHint gradient + Input.mp4 RGB → gradient-alpha'li WebP."""
    input_mp4 = source_dir / "Input.mp4"
    alpha_dir = source_dir / "AlphaHint"
    if not input_mp4.exists() or not alpha_dir.exists():
        print(f"  [SKIP] {set_id}: missing Input.mp4 or AlphaHint/ in {source_dir}")
        return False
    alphas = sorted(alpha_dir.glob("*.png"))
    if not alphas:
        print(f"  [SKIP] {set_id}: AlphaHint empty in {source_dir}")
        return False

    # Input.mp4 frame'lerini PNG sequence olarak çıkar
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="alpha_h_"))
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-i", str(input_mp4),
            "-vf", "fps=24,scale=1024:1024",
            "-pix_fmt", "rgb24",
            str(tmp_dir / "frame_%05d.png"),
        ], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [FAIL ffmpeg] {set_id}: {r.stderr[-200:]}")
            return False

        rgb_files = sorted(tmp_dir.glob("*.png"))
        n = min(len(rgb_files), len(alphas))
        print(f"  frames: rgb={len(rgb_files)}  alpha={len(alphas)}  using={n}")

        frames = []
        for i in range(n):
            rgb = Image.open(rgb_files[i]).convert("RGBA")
            alpha = Image.open(alphas[i]).convert("L")
            if alpha.size != rgb.size:
                alpha = alpha.resize(rgb.size, Image.Resampling.LANCZOS)
            r_ch, g_ch, b_ch, _ = rgb.split()
            rgba = Image.merge("RGBA", (r_ch, g_ch, b_ch, alpha))
            frames.append(rgba)

        out = out_dir / f"{set_id}.webp"
        duration_ms = 1000 // FPS
        frames[0].save(
            str(out), format="WEBP", save_all=True, append_images=frames[1:],
            duration=duration_ms, loop=0, lossless=False, quality=92,
        )
        print(f"  ✓ {set_id}: {n} frame → {out.name} ({out.stat().st_size/1024/1024:.1f} MB)")
        return True
    finally:
        for fp in tmp_dir.glob("*.png"):
            fp.unlink()
        tmp_dir.rmdir()


PRESETS = {
    "nora_idle_a_sigh0":            "nora-idle-a-sigh0",
    "nora_idle_a_sigh1":            "Nora_Idle-a-sigh1",
    "nora_idle_a_sigh2":            "nora-idle-a-sigh2",
    "nora_idle_a_sigh_3":           "nora-idle-a-sigh-3",
    "nora_idle_b_dancing_serious":  "nora-idle-b-dancing-serious",
    "nora_idle_b_dancing_serious_2":"nora-idle-b-dancing-serious-2",
    "nora_idle_b_japanese_thank_you":"nora-idle-b-japanese-thank-you",
    "nora_idle_b_jumping":          "nora-idle-b-jumping",
    "nora_idle_b_turns_around":     "nora-idle-b-turns around",
    "nora_idle_c_dancing_sexy1":    "nora-idle-c-dancing-sexy1",
    "nora_idle_c_dancing_sexy2":    "nora idle-c-dancing sexy2",
    "nora_idle_c_impatient":        "nora-idle-c-impatient",
    "nora_thinking1":               "nora-thinking1",
    "nora_thinking2":               "nora-thinking2",
    "nora_thinking3":               "nora-thinking3",
    "nora_talking1":                "Nora_Talking1",
    "nora_talking2":                "Nora_Talking2",
}


def main():
    parser = argparse.ArgumentParser(description="Reprocess Nora clips with gradient alpha into mascot_v2/")
    parser.add_argument("--video", type=str, default=None,
                        help="Tek video testi: dir_name (örn. nora-idle-c-impatient)")
    parser.add_argument("--out", type=str, default=str(MASCOT_V2),
                        help="Output directory (default: mascot_v2)")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.video:
        # Tek set_id'yi bul (ters map)
        match = None
        for set_id, dir_name in PRESETS.items():
            if dir_name == args.video:
                match = (set_id, dir_name)
                break
        if not match:
            print(f"[FAIL] --video '{args.video}' not found in PRESETS")
            print("       Available:", list(PRESETS.values()))
            return 2
        set_id, dir_name = match
        source_dir = CLIPS / dir_name
        print(f"[SINGLE VIDEO TEST] {set_id} ← {source_dir}")
        print(f"[OUT] {out_dir}")
        ok = reprocess_with_gradient(set_id, source_dir, out_dir)
        return 0 if ok else 1

    # Tam batch
    print(f"Reprocessing {len(PRESETS)} with gradient alpha (AlphaHint) → {out_dir}...", flush=True)
    ok = 0
    for set_id, dir_name in PRESETS.items():
        source_dir = CLIPS / dir_name
        if reprocess_with_gradient(set_id, source_dir, out_dir):
            ok += 1
    print(f"Done: {ok}/{len(PRESETS)} OK", flush=True)
    return 0 if ok == len(PRESETS) else 1


if __name__ == "__main__":
    sys.exit(main())