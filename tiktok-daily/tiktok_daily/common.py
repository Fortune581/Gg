import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


class WorkflowError(RuntimeError):
    pass


def config():
    return json.loads((ROOT / "config.json").read_text())


def dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def request(url, *, data=None, method=None, headers=None, timeout=25, limit=5_000_000):
    # Do not retry POST: the remote operation might have succeeded before a timeout.
    hdr = {"User-Agent": "DailyTikTokZM/1.0 (original-content-research)", **(headers or {})}
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise WorkflowError("Response exceeded the configured download limit")
            return body
    except urllib.error.HTTPError as exc:
        # Avoid returning response bodies or request headers that could expose credentials.
        raise WorkflowError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).hostname}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WorkflowError(f"Network request failed for {urllib.parse.urlsplit(url).hostname}") from None


def json_request(url, **kwargs):
    try:
        return json.loads(request(url, **kwargs))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WorkflowError(f"Invalid JSON response from {urllib.parse.urlsplit(url).hostname}") from None


def now_local(cfg=None):
    return datetime.now(ZoneInfo((cfg or config())["timezone"]))


def due_time(day, cfg=None, now=None):
    cfg = cfg or config()
    current = now or now_local(cfg)
    target = datetime.fromisoformat(day + "T" + cfg["publish_time"]).replace(tzinfo=ZoneInfo(cfg["timezone"]))
    if current.date() != target.date():
        raise WorkflowError("Refusing to publish a package from a different Lusaka date")
    # A delayed runner may publish later the same day; never roll the post into tomorrow.
    target = max(target, current + timedelta(minutes=3))
    if target.date() != current.date():
        raise WorkflowError("Not enough time remains to schedule within today's local date")
    return target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalise(text):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", text.lower())).strip()


def topic_key(title):
    return hashlib.sha256(normalise(title).encode()).hexdigest()[:20]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_dir():
    return Path(os.environ.get("TIKTOK_MODELS_DIR", ROOT / ".models"))
