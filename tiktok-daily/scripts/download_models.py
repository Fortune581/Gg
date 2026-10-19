"""Download pinned open models; fail on a checksum mismatch. No API key needed."""
import concurrent.futures
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tiktok_daily.common import ROOT, WorkflowError, model_dir, sha256


def download(asset):
    target = model_dir() / asset["filename"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and sha256(target) == asset["sha256"]:
        print("Verified cached", target.name, flush=True)
        return
    temp = target.with_suffix(target.suffix + ".part")
    print("Downloading", target.name, flush=True)
    with urllib.request.urlopen(asset["url"], timeout=60) as remote, open(temp, "wb") as local:
        while chunk := remote.read(1024 * 1024):
            local.write(chunk)
    if sha256(temp) != asset["sha256"]:
        temp.unlink(missing_ok=True)
        raise WorkflowError("Model checksum mismatch: " + target.name)
    temp.replace(target)
    print("Verified", target.name, flush=True)


if __name__ == "__main__":
    assets = json.loads((ROOT / "assets.lock.json").read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(download, assets.values()))
