"""Convert the nine received Kling clips; source MP4 files remain unchanged."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data/tools/media-python"))
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# Shared framing includes the raised hair tuft in sneeze/nap, without tracking crops.
FILTER = "crop=554:554:356:94,format=rgba,colorkey=0x00F200:0.22:0.12,despill=green,scale=512:512,setsar=1"
output = ROOT / "frontend/assets/pet/animations"
output.mkdir(parents=True, exist_ok=True)
records = []
for name in ("idle", "search", "pat", "idle_guard", "idle_drum", "idle_listen", "idle_sneeze", "peek", "nap"):
    source = ROOT / f"data/pet-animation/incoming/{name}.mp4"
    target = output / f"{name}-v2.webm"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                    "-an", "-vf", FILTER, "-r", "24", "-c:v", "libvpx-vp9",
                    "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-crf", "30",
                    "-b:v", "0", "-deadline", "good", "-cpu-used", "4", str(target)], check=True)
    records.append({"id": name, "source": source.name,
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "output": target.name, "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "bytes": target.stat().st_size})
    print(f"{target.name}: {target.stat().st_size:,} bytes", flush=True)
report = {"date": "2026-10-29", "ffmpeg": imageio_ffmpeg.get_ffmpeg_version(),
          "filter": FILTER, "fps": 24, "audio": False, "clips": records}
(output / "provenance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
