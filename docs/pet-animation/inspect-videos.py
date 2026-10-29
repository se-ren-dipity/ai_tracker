"""Read source videos and generate review evidence without changing the originals."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data/tools/media-python"))
import imageio_ffmpeg


def inspect(source, output):
    reader = imageio_ffmpeg.read_frames(str(source), pix_fmt="rgb24", output_params=["-vf", "scale=640:360"])
    metadata = next(reader)
    fps = metadata["fps"]
    samples, boxes, changes = [], [], []
    first = previous = last = None
    # Alpha is not created here. This mask only measures the non-green content bounds.
    for index, raw in enumerate(reader):
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(360, 640, 3)
        rgb = frame.astype(np.int16)
        foreground = ~((rgb[:, :, 1] > np.maximum(rgb[:, :, 0], rgb[:, :, 2]) + 45) & (rgb[:, :, 1] > 100))
        # Central character region excludes unrelated decorations/watermarks from geometry metrics.
        central = foreground.copy()
        central[:, :160] = False
        central[:, 480:] = False
        ys, xs = np.where(central)
        if len(xs):
            boxes.append([int(xs.min()), int(ys.min()), int(xs.max()+1), int(ys.max()+1)])
        if first is None:
            first = frame.copy()
        if previous is not None:
            changes.append(float(np.abs(rgb[50:330,160:480] - previous[50:330,160:480].astype(np.int16)).mean()))
        if index % max(1, round(fps)) == 0:
            samples.append((index / fps, Image.fromarray(frame.copy())))
        previous = last = frame.copy()
    samples.append((index / fps, Image.fromarray(last)))
    sheet = Image.new("RGB", (640 * 3, 390 * ((len(samples)+2)//3)), "#181d26")
    draw = ImageDraw.Draw(sheet)
    for i, (seconds, frame) in enumerate(samples):
        x, y = (i % 3)*640, (i // 3)*390
        sheet.paste(frame, (x,y+24))
        draw.text((x+10,y+5), f"{source.stem}  {seconds:.2f}s", fill="white")
    sheet.save(output / f"{source.stem}-contact.jpg", quality=92)
    Image.fromarray(first).save(output / f"{source.stem}-first.png")
    Image.fromarray(last).save(output / f"{source.stem}-last.png")
    boxes = np.array(boxes)
    report = {"file":source.name, "sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
              "bytes":source.stat().st_size,"metadata":metadata,"decoded_frames":index+1,
              "measured_seconds":(index+1)/fps, "analysis_size":[640,360],
              "subject_bounds_min":boxes.min(axis=0).tolist(),"subject_bounds_max":boxes.max(axis=0).tolist(),
              "first_last_subject_mean_abs_difference":float(np.abs(first[50:330,160:480].astype(np.int16)-last[50:330,160:480].astype(np.int16)).mean()),
              "adjacent_subject_mean_abs_difference":float(np.mean(changes)),
              "peak_adjacent_subject_mean_abs_difference":float(max(changes))}
    (output / f"{source.stem}-inspection.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return report


if __name__ == "__main__":
    output = ROOT / "data/pet-animation/review-20261029"
    output.mkdir(parents=True,exist_ok=True)
    results = [inspect(p, output) for p in sorted((ROOT / "data/pet-animation/incoming").glob("*.mp4"))]
    print(json.dumps(results,ensure_ascii=False,indent=2))
