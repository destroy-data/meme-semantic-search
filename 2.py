"""Stage 2: describe memes with a VLM, storing structured JSON descriptions."""

import argparse
import base64
import io
import json
import sqlite3
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

import sqlite_vec
from PIL import Image

HUMOR_TYPES = [
    "irony",
    "absurd",
    "dark",
    "cringe",
    "wholesome",
    "relatable",
    "situational",
    "shitpost",
    "savage",
    "political",
    "meta",
]
SCHEMA = {
    "type": "object",
    "properties": {
        "transcription": {"type": "string"},
        "template": {"type": "string"},
        "scene": {"type": "string"},
        "usage_context": {"type": "string"},
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 5,
            "maxItems": 10,
            "uniqueItems": True,
        },
        "humor_type": {
            "type": "array",
            "items": {"type": "string", "enum": HUMOR_TYPES},
            "minItems": 1,
            "maxItems": 2,
        },
    },
    "required": [
        "transcription",
        "template",
        "scene",
        "usage_context",
        "keywords",
        "humor_type",
    ],
}
MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
MAX_SIDE = 1568  # larger images are downscaled before sending to the VLM
REQUEST_TIMEOUT = 138


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MIME:
        with Image.open(path) as img:
            small_enough = max(img.size) <= MAX_SIDE
        if small_enough:
            b64 = base64.b64encode(path.read_bytes()).decode()
            return f"data:{MIME[suffix]};base64,{b64}"
    # Oversized images get downscaled; gifs are flattened to their first frame.
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def request_description(cfg: dict, prompt: str, image: str) -> str:
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        # No max_tokens: qwen3-vl reasoning tokens count against the budget but
        # not against usage.completion_tokens, so any explicit cap silently
        # truncates mid-CoT and returns empty content.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "meme_description", "schema": SCHEMA},
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
    }
    if cfg.get("effort_level"):
        payload["reasoning_effort"] = cfg["effort_level"]
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = json.load(resp)
    return body["choices"][0]["message"]["content"]


def describe(
    con: sqlite3.Connection, cfg: dict, retry_failed: bool, limit: int
) -> None:
    version = cfg["prompt_version"]
    model = cfg["model"]
    effort = cfg.get("effort_level")  # None = not set / not sent to API
    prompt = (Path(__file__).parent / "prompts" / f"{version}.txt").read_text()
    # Skip logic: describe memes that don't yet have a description row under the
    # current (model, prompt_version, effort_level) tuple. `IS` handles NULL
    # equality correctly. By default any existing row (success or failure) counts
    # as done; --retry-failed only counts successes, so failures get re-attempted.
    dedup_filter = "AND d.status = 'described'" if retry_failed else ""
    rows = con.execute(
        f"""
        SELECT m.hash, m.path FROM memes m
        WHERE m.perceptual_duplicate_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM descriptions d
              WHERE d.hash = m.hash
                AND d.model = ?
                AND d.prompt_version = ?
                AND d.effort_level IS ?
                {dedup_filter}
          )
        ORDER BY m.hash
        """,
        (model, version, effort),
    ).fetchall()
    if limit:
        rows = rows[:limit]

    done = failed = 0
    for digest, path in rows:
        try:
            raw = request_description(cfg, prompt, image_data_url(Path(path)))
            data = json.loads(raw)
            missing = [k for k in SCHEMA["required"] if k not in data]
            if missing:
                raise ValueError(f"VLM output missing keys: {missing}")
            con.execute(
                "INSERT OR REPLACE INTO descriptions "
                "(hash, description_json, prompt_version, model, effort_level, status, error) "
                "VALUES (?, ?, ?, ?, ?, 'described', NULL)",
                (digest, raw, version, model, effort),
            )
            done += 1
        except Exception as e:
            con.execute(
                "INSERT OR REPLACE INTO descriptions "
                "(hash, description_json, prompt_version, model, effort_level, status, error) "
                "VALUES (?, NULL, ?, ?, ?, 'failed_describe', ?)",
                (digest, version, model, effort, str(e)),
            )
            failed += 1
            print(f"{time.strftime('%H:%M:%S')} failed {path}: {e}", file=sys.stderr)
        con.commit()  # commit per image so an interrupted run loses nothing
        print(
            f"{time.strftime('%H:%M:%S')} described {done}/{len(rows)}, {failed} failed"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2: describe memes with a VLM")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-pick rows with status=failed_describe",
    )
    parser.add_argument("--limit", type=int, default=0, help="describe at most N memes")
    parser.add_argument("--model", help="override [describe].model from config")
    parser.add_argument("--effort", help="override [describe].effort_level from config")
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    if args.model:
        cfg["describe"]["model"] = args.model
    if args.effort:
        cfg["describe"]["effort_level"] = args.effort

    con = connect(cfg["db_path"])
    describe(con, cfg["describe"], args.retry_failed, args.limit)
    con.close()


if __name__ == "__main__":
    main()
