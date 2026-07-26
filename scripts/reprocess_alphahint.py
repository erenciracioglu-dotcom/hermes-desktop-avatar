"""AlphaHint gradient alpha kullanarak transparan WebP üret.

CorridorKey'in output'u 2 katmanlı:
  AlphaHint/<frame>.png → gradient alpha (0-255, saç/peri kenarlari dahil)
  Input.mp4             → 1024x1024 raw frame

Bu script AlphaHint gradient'ini Input.mp4 frame'i üzerine apply eder →
gradient alpha'li RGBA WebP.  batch_corridor_key.py'in RGB→RGBA bug'i
yüzünden kaybedilen yumuşak kenarlar bu script'le geri geliyor.
"""
import sys, os, subprocess
from pathlib import Path

# avatar venv'i hermes-agent'tan izole et
_sys_clean = [p for p in sys.path if 'hermes-agent' not in p]
if _sys_clean != sys.path:
    sys.path[:] = _sys_clean

from PIL import Image

MASCOT = Path(r"E:\Projects\hermes-desktop-avatar\assets\sprites\mascot")
CLIPS  = Path(r"E:\corridor_key\EZ-CorridorKey\ClipsForInference")
FPS    = 24


def reprocess_with_gradient(set_id: str, source_dir: Path) -> bool:
    """AlphaHint gradient + Input.mp4 RGB → gradient-alpha'li WebP."""
    input_mp4 = source_dir / "Input.mp4"
    alpha_dir = source_dir / "AlphaHint"
    if not input_mp4.exists() or not alpha_dir.exists():
        return False
    alphas = sorted(alpha_dir.glob("*.png"))
    if not alphas:
        return False

    # Input.mp4 frame'lerini PNG sequence olarak çıkar
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="alpha_h_"))
    try:
        # ffmpeg ile yuv420p→rgb PNG çıkar.  -pix_fmt rgb24 kalite kaybını önler.
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

        frames = []
        for i in range(n):
            rgb = Image.open(rgb_files[i]).convert("RGBA")  # RGB+gri A
            alpha = Image.open(alphas[i]).convert("L")
            # AlphaHint bazen 1024x1024, Input 1024x1024 (veya 960x960) olur
            if alpha.size != rgb.size:
                alpha = alpha.resize(rgb.size, Image.Resampling.LANCZOS)
            # RGBA → split R, G, B, A → yeni alpha ile birleştir
            r_ch, g_ch, b_ch, _ = rgb.split()
            rgba = Image.merge("RGBA", (r_ch, g_ch, b_ch, alpha))
            frames.append(rgba)

        out = MASCOT / f"{set_id}.webp"
        duration_ms = 1000 // FPS
        frames[0].save(
            str(out), format="WEBP", save_all=True, append_images=frames[1:],
            duration=duration_ms, loop=0, lossless=False, quality=92,
        )
        print(f"  ✓ {set_id}: {n} frame gradient-alpha → {out.name} ({out.stat().st_size/1024/1024:.1f} MB)")
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
    print(f"Reprocessing {len(PRESETS)} with gradient alpha (AlphaHint)...", flush=True)
    ok = 0
    for set_id, dir_name in PRESETS.items():
        source_dir = CLIPS / dir_name
        if reprocess_with_gradient(set_id, source_dir):
            ok += 1
    print(f"Done: {ok}/{len(PRESETS)} OK", flush=True)


if __name__ == "__main__":
    main()
