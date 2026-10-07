"""Stage 2: describe memes with a VLM, storing structured JSON descriptions."""

import argparse
import base64
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

from PIL import Image

import common

# The VLM output contract is data, not code: the model sees it verbatim (as
# response_format on the openai backend, as --json-schema on claude-code-cli),
# and it changes on its own cadence. Note that `keywords` is one flat list
# holding both languages: 3-8 concepts, each given in Polish and English.
SCHEMA = json.loads((Path(__file__).parent / "description_schema.json").read_text())
HUMOR_TYPES = SCHEMA["properties"]["humor_type"]["items"]["enum"]
MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
MAX_SIDE = 1440  # larger images are downscaled before sending to the VLM
# Kept separate from common.REQUEST_TIMEOUT: a reasoning VLM call is a different
# beast from an embedding call. Without streaming this budget covers the entire
# generation, and a reasoning model at medium effort blows past a single minute.
REQUEST_TIMEOUT = 4 * 69  # Somewhat nice
# A spent subscription quota says nothing about the meme, so it must not become
# a failed_describe row: that row would count as "done" and the retry would be
# skipped. Wait the window out and re-send the same image instead.
USAGE_LIMIT_WAIT = 3600
USAGE_LIMIT_API_ERROR = "usage_limit_reached"


class UsageLimitError(RuntimeError):
    """Subscription quota is spent — a property of the account, not the image."""


def is_usage_limit(envelope: dict) -> bool:
    # Keyed on the envelope's typed fields, not on `result`: that is the text
    # shown to a human ("You've hit your session limit · resets 1am") and its
    # wording moves between CLI releases. A bare 429 without the typed kind is
    # some other throttle, but waiting it out is right for that too.
    return (
        envelope.get("api_error") == USAGE_LIMIT_API_ERROR
        or envelope.get("api_error_status") == 429
    )


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


def request_description_cli(
    cfg: dict, prompt: str, path: Path, *, verbose: bool = False
) -> str:
    """Describe through the local Claude Code CLI instead of an HTTP endpoint.

    Billed against the Claude Code subscription, so no api_key/base_url is used.
    Differences from the OpenAI-compatible path, all forced by the CLI: the
    image goes in as a file path for the Read tool (no data URL, no downscaling
    on our side), the schema travels as --json-schema instead of
    response_format, and the answer arrives wrapped in a result envelope.
    """
    argv = [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--allowed-tools",
        "Read",
        # The CLI's stand-in for response_format: it exposes a StructuredOutput
        # tool bound to this schema, validates the call against it and puts the
        # parsed object in the envelope. Same enforcement the HTTP path gets.
        "--json-schema",
        json.dumps(SCHEMA, ensure_ascii=False),
        # No CLAUDE.md (global or project), skills, plugins, hooks or MCP: the
        # subprocess should see the meme prompt, not this repo's coding context.
        # Unlike --bare it keeps normal auth, so the run stays on the subscription.
        "--safe-mode",
        "--add-dir",
        str(path.parent),
        "--model",
        cfg["model"],
    ]
    if cfg.get("effort_level"):
        argv += ["--effort", cfg["effort_level"]]
    # Key names need no nudge: the schema is the StructuredOutput tool's own
    # input schema, so the model cannot emit the prompt's Polish field labels.
    # This line exists for the superseded prompts (v1, v2, vgrok, vgrok2beta),
    # which still close with "answer with bare JSON" — written for
    # response_format, and on this path it would leave the tool uncalled.
    body = (
        f"{prompt}\n\nOpisz mema z pliku: {path}\n\n"
        "Wynik oddaj przez structured output, nie jako tekst."
    )
    proc = subprocess.run(
        argv,
        input=body,
        # Both streams captured, never inherited: stdout carries the result
        # envelope that tells a spent quota from a broken image, and stderr is
        # kept for the error message when there is no envelope at all. Under -v
        # stderr is echoed below instead.
        capture_output=True,
        text=True,
        timeout=REQUEST_TIMEOUT,
        check=False,  # a failed CLI run becomes a failed_describe row, not a crash
    )
    if verbose and proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    # A failed run still prints its result envelope (exit 1, is_error=true),
    # and that envelope is the only place the cause is spelled out.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        envelope = None
    if envelope is None:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        raise RuntimeError(
            f"claude exited {proc.returncode}: {detail[:500] or 'no output'}"
        )
    if proc.returncode != 0 or envelope.get("is_error"):
        # `result` first: the leading usage/session-id keys used to fill the
        # whole truncated message and hide the actual reason.
        detail = (
            f"{envelope.get('result', '')} "
            f"[api_error={envelope.get('api_error')}, "
            f"status={envelope.get('api_error_status')}, "
            f"exit={proc.returncode}]"
        )
        if is_usage_limit(envelope):
            raise UsageLimitError(detail)
        raise RuntimeError(f"claude: {detail}")
    # Schema-validated and already parsed by the CLI, so no fence-stripping or
    # brace-hunting here. Missing means the model answered in prose instead of
    # calling the tool — a failed_describe row, never a half-parsed description.
    data = envelope.get("structured_output")
    if not data:
        raise ValueError(f"no structured output: {envelope.get('result', '')[:200]}")
    return json.dumps(data, ensure_ascii=False)


KEY_WIDTH = 13  # "transcription", the longest field name


def print_description(data: dict) -> None:
    """Dump a fresh description to stdout so prompt quality can be judged live.

    Values are wrapped to the terminal and hang-indented under their key —
    transcriptions run to many lines and would otherwise wreck the layout.
    """
    width = shutil.get_terminal_size((100, 24)).columns
    body_width = max(40, width - KEY_WIDTH - 3)
    indent = " " * (KEY_WIDTH + 3)
    # Iterate the schema, not the response: key order in a JSON object is not
    # guaranteed and models occasionally emit the fields alphabetically.
    keys = [k for k in SCHEMA["properties"] if k in data]
    keys += [k for k in data if k not in SCHEMA["properties"]]
    for key in keys:
        value = data[key]
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines: list[str] = []
        for paragraph in str(value).splitlines():
            # An empty paragraph is a blank line between meme panels; keep it.
            lines.extend(textwrap.wrap(paragraph, body_width) or [""])
        print(f"  {key:<{KEY_WIDTH}} {lines[0] if lines else ''}".rstrip())
        for line in lines[1:]:
            print(f"{indent}{line}".rstrip())


def rows_for_paths(con: sqlite3.Connection, paths: list[str]) -> list[tuple[str, str]]:
    """Look up specific files in `memes` so one meme can be described on demand.

    Naming a file is an explicit instruction, so this deliberately ignores both
    the queue query and the `perceptual_duplicate_of IS NULL` filter — a
    non-representative meme is still describable when asked for by name.
    """
    rows, missing = [], []
    for raw in paths:
        target = str(Path(raw).resolve())
        row = con.execute(
            "SELECT hash, path FROM memes WHERE path = ?", (target,)
        ).fetchone()
        if row is None:
            missing.append(target)
        else:
            rows.append(row)
    if missing:
        listing = "\n  ".join(missing)
        raise SystemExit(f"not in the memes table (run scan first):\n  {listing}")
    return rows


def describe_memes(
    con: sqlite3.Connection,
    cfg: dict,
    *,
    retry_failed: bool = False,
    limit: int = 0,
    any_effort: bool = False,
    verbose: bool = False,
    paths: list[str] | None = None,
) -> None:
    version = cfg["prompt_version"]
    model = cfg["model"]
    backend = cfg.get("backend", "openai")  # "claude-code-cli" = local CLI subprocess
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
    if paths:
        # --path overrides the queue entirely, skip logic included: re-describing
        # one meme under an unchanged config is the whole point (prompt
        # iteration), and descriptions_config_idx turns the write into a replace
        # rather than a duplicate. Announce it so nothing is overwritten silently.
        rows = rows_for_paths(con, paths)
        for digest, path in rows:
            prior = con.execute(
                "SELECT status FROM descriptions WHERE hash = ? AND model = ? "
                "AND prompt_version = ? AND IFNULL(effort_level, '') = IFNULL(?, '')",
                (digest, model, version, effort),
            ).fetchone()
            if prior:
                print(
                    f"replacing {prior[0]} row for {Path(path).name} "
                    f"({model}, {version}, effort={effort or 'none'})"
                )
    else:
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
        # Retried in place on a usage limit, so the loop advances only once
        # this meme is either described or recorded as failed.
        while True:
            try:
                if backend == "claude-code-cli":
                    started = time.monotonic()
                    raw = request_description_cli(
                        cfg, prompt, Path(path), verbose=verbose
                    )
                else:
                    image = image_data_url(Path(path))
                    started = (
                        time.monotonic()
                    )  # times the API call only, not downscaling
                    raw = request_description(cfg, prompt, image)
                elapsed = time.monotonic() - started
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
                if verbose:
                    where = Path(path)
                    print(
                        f"\n{time.strftime('%H:%M:%S')} [{done}/{len(rows)}] "
                        f"{elapsed:.1f}s/{REQUEST_TIMEOUT}s  "
                        f"{where.parent.name}/{where.name}"
                    )
                    print_description(data)
                break
            except UsageLimitError as e:
                # Not this meme's fault and not a permanent failure: no row
                # is written, the queue is left untouched, and the same image
                # goes out again once the window has rolled over.
                print(
                    f"{time.strftime('%H:%M:%S')} usage limit hit, waiting "
                    f"{USAGE_LIMIT_WAIT // 60} min before retrying {Path(path).name}: {e}"
                )
                time.sleep(USAGE_LIMIT_WAIT)
            except Exception as e:
                con.execute(
                    "INSERT OR REPLACE INTO descriptions "
                    "(hash, description_json, prompt_version, model, effort_level, status, error) "
                    "VALUES (?, NULL, ?, ?, ?, 'failed_describe', ?)",
                    (digest, version, model, effort, str(e)),
                )
                failed += 1
                print(
                    f"{time.strftime('%H:%M:%S')} failed {path}: {e}", file=sys.stderr
                )
                break
        con.commit()  # commit per image so an interrupted run loses nothing
        if not verbose:  # in verbose mode the per-meme header carries the count
            print(
                f"{time.strftime('%H:%M:%S')} described {done}/{len(rows)}, "
                f"{failed} failed"
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
        "--path",
        nargs="+",
        metavar="FILE",
        help="describe these files only; ignores the queue, the duplicate filter "
        "and skip logic, so an existing row for the same config is replaced",
    )
    parser.add_argument(
        "--any-effort",
        action="store_true",
        help="skip memes already described with the same model+prompt at any effort_level",
    )
    parser.add_argument("--model", help="override [describe].model from config")
    parser.add_argument("--effort", help="override [describe].effort_level from config")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print each description as it comes back",
    )


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
        verbose=args.verbose,
        paths=args.path,
    )
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 2: describe memes with a VLM")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
