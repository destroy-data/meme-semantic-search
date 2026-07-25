"""Stage 2: describe memes with a VLM, storing structured JSON descriptions."""

import argparse
import base64
import io
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

import common

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
        "the_clue": {"type": "string"},
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
        "the_clue",
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
MAX_SIDE = 1440  # larger images are downscaled before sending to the VLM
# Kept separate from common.REQUEST_TIMEOUT: a reasoning VLM call is a different
# beast from an embedding call and will want its own budget.
REQUEST_TIMEOUT = 69  # Nice


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


def describe_memes(
    con: sqlite3.Connection,
    cfg: dict,
    *,
    retry_failed: bool = False,
    limit: int = 0,
    any_effort: bool = False,
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
    # --any-effort: a description under the same (model, prompt) counts as done
    # regardless of effort_level, so effort experiments don't trigger re-runs.
    effort_filter = "" if any_effort else "AND d.effort_level IS ?"
    params = [model, version] if any_effort else [model, version, effort]
    rows = con.execute(
        f"""
        SELECT m.hash, m.path FROM memes m
        WHERE m.perceptual_duplicate_of IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM descriptions d
              WHERE d.hash = m.hash
                AND d.model = ?
                AND d.prompt_version = ?
                {effort_filter}
                {dedup_filter}
          )
        ORDER BY m.hash
        """,
        params,
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-pick rows with status=failed_describe",
    )
    parser.add_argument("--limit", type=int, default=0, help="describe at most N memes")
    parser.add_argument(
        "--any-effort",
        action="store_true",
        help="skip memes already described with the same model+prompt at any effort_level",
    )
    parser.add_argument("--model", help="override [describe].model from config")
    parser.add_argument("--effort", help="override [describe].effort_level from config")


def run(args: argparse.Namespace, cfg: dict) -> None:
    if args.model:
        cfg["describe"]["model"] = args.model
    if args.effort:
        cfg["describe"]["effort_level"] = args.effort

    con = common.connect(cfg["db_path"])
    describe_memes(
        con,
        cfg["describe"],
        retry_failed=args.retry_failed,
        limit=args.limit,
        any_effort=args.any_effort,
    )
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 2: describe memes with a VLM")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
