"""Reprocess 17 sprite WebP'leri chroma key ile transparan yap.

Mevcut WebP'ler RGB — arka plan (15,99,173) gibi koyu mavi, transparan değil.
Source video'lardan (Input.mp4'ler ClipsForInference/<video>/Input.mp4'te duruyor)
tekrar yükle + chroma key uygula + RGBA olarak WebP'ye kaydet.

Yaklaşım 1 — Source video zaten mavi chroma'lı (kaynak alın orada pixelle
mavi zemin = alpha=0).
"""
import sys, os
from pathlib import Path

# avatar venv'i hermes-agent'tan izole et
_sys_clean = [p for p in sys.path if 'hermes-agent' not in p]
if _sys_clean != sys.path:
    sys.path[:] = _sys_clean

from PIL import Image

MASCOT = Path(r"E:\Projects\hermes-desktop-avatar\assets\sprites\mascot")
CLIPS  = Path(r"E:\corridor_key\EZ-CorridorKey\ClipsForInference")
FPS    = 24

# Chroma key: mavi → alpha=0.
# Pure blue zone (R<80, G<120, B>140, B>R+30, B>G+20) tamamen transparan.
# Spill suppression: kenar yumuşatma (opsiyonel — ilk geçişte yok).
def make_chroma_mask(img: Image.Image) -> Image.Image:
    """Return alpha channel (mode L): 0=transparent, 255=opaque."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    alpha = Image.new("L", rgb.size, 255)
    ap = alpha.load()
    for x in range(w):
        for y in range(h):
            r, g, b = px[x, y]
            # Mavi chroma testi: B baskın, R/G düşük
            if b > 140 and r < 80 and g < 120 and (b - r) > 30 and (b - g) > 20:
                ap[x, y] = 0
    return alpha


def reprocess_one(set_id: str, source_dir: Path):
    """Reprocess one clip: load Input.mp4 frames, chroma key, save as WebP."""
    input_mp4 = source_dir / "Input.mp4"
    if not input_mp4.exists():
        print(f"  [SKIP] {set_id}: Input.mp4 yok ({source_dir})")
        return False

    # Frame'leri çıkar (video_player gibi OpenCV alternatifi: Pillow + ffmpeg)
    import subprocess, tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="chroma_"))
    try:
        # ffmpeg ile PNG sequence çıkar (yavaş ama basit).  -f image2
        # Native FPS'i koru.  Trim 0-N.
        r = subprocess.run([
            "ffmpeg", "-y", "-i", str(input_mp4),
            "-vf", "fps=24",
            str(tmp_dir / "%05d.png"),
        ], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [FAIL] ffmpeg: {r.stderr[-200:]}")
            return False

        # Frame list
        png_files = sorted(tmp_dir.glob("*.png"))
        if not png_files:
            print(f"  [FAIL] hiç frame üretilmedi: {set_id}")
            return False

        # Her frame'i RGBA + chroma alpha işle, listeye ekle
        frames = []
        for fp in png_files:
            im = Image.open(fp).convert("RGB")
            alpha = make_chroma_mask(im)
            rgba = im.convert("RGBA")
            # Alpha kanalını putalpha ile ayarla
            rgba.putalpha(alpha)
            frames.append(rgba)

        n = len(frames)
        out = MASCOT / f"{set_id}.webp"
        # 100 ms frame duration (10 fps) — orijinal 24 fps çok hızlı
        duration_ms = 1000 // FPS  # 41 ms — 24 fps
        frames[0].save(
            str(out), format="WEBP",
            save_all=True, append_images=frames[1:],
            duration=duration_ms, loop=0,
            lossless=False, quality=92,  # alpha için daha yüksek kalite
        )
        print(f"  ✓ {set_id}: {n} frame → {out.name} ({out.stat().st_size/1024/1024:.1f} MB)")
        return True
    finally:
        for fp in tmp_dir.glob("*.png"):
            fp.unlink()
        tmp_dir.rmdir()


PRESETS = {
    # set_id → input dir name
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
    print(f"Reprocessing {len(PRESETS)} sprite set...")
    ok = 0
    for set_id, dir_name in PRESETS.items():
        source_dir = CLIPS / dir_name
        if reprocess_one(set_id, source_dir):
            ok += 1
    print(f"Done: {ok}/{len(PRESETS)} OK")


if __name__ == "__main__":
    main()
