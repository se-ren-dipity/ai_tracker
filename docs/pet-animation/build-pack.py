"""Build copy-ready Kling prompts and a self-contained production handoff zip."""
import csv
import argparse
import io
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def build(idle_only=False):
    data = json.loads((HERE / "actions.json").read_text(encoding="utf-8"))
    common = (HERE / "common-prompt.txt").read_text(encoding="utf-8").strip()
    actions = data["actions"]
    assert len({a["id"] for a in actions}) == len(actions)
    assert set(data["pilot"]) <= {a["id"] for a in actions}
    if idle_only:
        by_id = {a["id"]:a for a in actions}
        actions = [by_id[name] for name in data["idle_variants"]]
    output = ROOT / "output" / "pet" / ("tuntun-kling-idle-v1" if idle_only else "tuntun-kling-v1")
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for action in actions:
        prompt = common + "\n\n本段动作：" + action["name"] + "。\n" + "\n".join(action["steps"]) + "\n"
        if idle_only:
            prompt += "\n本段重点：" + action["check"] + "\n"
        files[f'prompts/{action["id"]}.txt'] = prompt.encode("utf-8")
    for name in ("tuntun-start-end.png", "tuntun-start-end.svg", "provenance.json"):
        files[f"reference/{name}"] = (HERE / "reference" / name).read_bytes()
    manifest = {**data, "actions":actions, "pilot":[], "received":[], "status":"idle_prompts_ready_videos_pending"} if idle_only else data
    files["actions.json"] = (json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
    files["START-HERE.md"] = (HERE / ("IDLE-START-HERE.md" if idle_only else "START-HERE.md")).read_bytes()
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    writer.writerow(["action", "name", "batch", "status", "model", "resolution", "duration", "take", "source_file", "review_notes"])
    for action in actions:
        writer.writerow([action["id"],action["name"],action["batch"],"not_generated","","",10,"","",action["check"]])
    files["generation-record-template.csv"] = records.getvalue().encode("utf-8-sig")
    # These are generated deliverables only; actual generation records live beside incoming videos.
    for relative, content in files.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    archive = ROOT / "output" / "pet" / ("tuntun-kling-idle-v1.zip" if idle_only else "tuntun-kling-starter-v1.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for relative, content in files.items():
            bundle.writestr(relative, content)
    print(f"Prepared {len(actions)} prompts and {len(files)} files: {archive}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle", action="store_true", help="Export only the six new idle performances.")
    build(parser.parse_args().idle)
