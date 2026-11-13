"""Stage 2: describe memes with a VLM, storing structured JSON descriptions."""

import argparse
import base64
import bisect
import io
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from PIL import Image

from memy import common

# The VLM output contract is data, not code: the model sees it verbatim (as
# response_format on openai, --json-schema on Claude, prompt text on OpenCode,
# --output-schema on Codex),
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
MAX_SIDE = 1440  # still images only; selected animation frames retain native size
# Kept separate from common.REQUEST_TIMEOUT: a reasoning VLM call is a different
# beast from an embedding call. Without streaming this budget covers the entire
# generation, and a reasoning model at medium effort blows past a single minute.
REQUEST_TIMEOUT = 4 * 69  # Somewhat nice
# A spent subscription quota says nothing about the meme, so it must not become
# a failed_describe row: that row would count as "done" and the retry would be
# skipped. Wait the window out and re-send the same image instead.
USAGE_LIMIT_WAIT = 3600 // 4
USAGE_LIMIT_API_ERROR = "usage_limit_reached"
TRANSCRIBE_TIMEOUT = 420  # the first request may have to load the ASR model
# Whisper fills music and silence with placeholders and stock phrases from its
# training subtitles. Compared after dropping *...*, [...], (...), punctuation
# (including ♪) and case.
NON_SPEECH = {
    "",
    "music",
    "muzyka",
    "muzyka w tle",
    "signal",
    "dziękuję za uwagę",
    "thanks for watching",
    "thank you for watching",
    "napisy stworzone przez społeczność amara org",
    "subtitles by the amara org community",
}


SCENE_CUT = 0.15
VIDEO_SCENE_CUT = 0.3
VIDEO_MAX_FRAMES = 12


@dataclass
class DescriptionImage:
    path: Path
    timestamp: float | None = None  # seconds in an animation; None for a still
    video: bool = False  # frame of a video file; its soundtrack is not sent


def shot_frame_indices(
    starts: list[float], cuts: list[float], duration: float
) -> list[int]:
    """The first frame, then shot midpoints (middle and last frame without cuts)."""
    cuts = sorted({cut for cut in cuts if 0 < cut < duration})
    if cuts:
        boundaries = [0.0, *cuts, duration]
        targets = [(a + b) / 2 for a, b in pairwise(boundaries)]
    else:
        targets = [duration / 2, starts[-1]]
    indices = [0, *(bisect.bisect_right(starts, target) - 1 for target in targets)]
    return list(dict.fromkeys(indices))


def prepare_video_images(path: Path, workdir: Path) -> list[DescriptionImage]:
    """The GIF frame selection on a video, with a higher cut threshold and a cap.

    Real footage scores camera motion and compression above the GIF cut
    threshold. Above VIDEO_MAX_FRAMES frames, they are thinned evenly, always
    keeping the first.
    """
    starts, scores = common.video_frames(path)
    last_delay = starts[-1] - starts[-2] if len(starts) > 1 else 0.1
    duration = starts[-1] + (last_delay if last_delay > 0 else 0.1)
    cuts = [start for start, score in zip(starts, scores) if score > VIDEO_SCENE_CUT]
    indices = shot_frame_indices(starts, cuts, duration)
    if len(indices) > VIDEO_MAX_FRAMES:
        step = (len(indices) - 1) / (VIDEO_MAX_FRAMES - 1)
        indices = [indices[round(i * step)] for i in range(VIDEO_MAX_FRAMES)]
    frames = common.extract_video_frames(path, indices, workdir)
    return [
        DescriptionImage(frame, starts[index], video=True)
        for frame, index in zip(frames, sorted(indices))
    ]


def prepare_description_images(path: Path, workdir: Path) -> list[DescriptionImage]:
    """Select shot midpoints, or the middle and last frame without cuts.

    Pillow decodes GIF/WebP/APNG and supplies their frame durations. FFmpeg
    detects cuts on those decoded frames with the prompt's scene > 0.15 rule.
    Selected PNGs retain their original resolution. The caller owns workdir.
    Videos are decoded and scored by FFmpeg; see prepare_video_images.
    """
    path = path.resolve()
    if common.is_video(path):
        return prepare_video_images(path, workdir)
    with Image.open(path) as image:
        if not getattr(image, "is_animated", False):
            return [DescriptionImage(path)]
        if not shutil.which("ffmpeg"):
            raise RuntimeError("animated images require ffmpeg on PATH")
        # An APNG can have a separate still preview outside the animation.
        first = 1 if image.info.get("default_image") else 0
        starts, manifest = [], ["ffconcat version 1.0"]
        duration = 0.0
        with tempfile.TemporaryDirectory(prefix="animation-", dir=workdir) as tmp:
            frames_dir = Path(tmp)
            for index in range(first, image.n_frames):
                image.seek(index)
                image.load()  # WebP publishes this frame's duration only on load
                starts.append(duration)
                # Missing/zero durations use the usual 100 ms playback fallback.
                delay = float(image.info.get("duration", 100) or 100) / 1000
                frame = frames_dir / f"frame-{len(starts):06d}.png"
                image.convert("RGB").save(frame)
                manifest += [
                    f"file '{frame.name}'",
                    "option framerate 1000",  # retain millisecond timestamps
                    f"duration {delay:.9f}",
                ]
                duration += delay
            concat = frames_dir / "frames.ffconcat"
            concat.write_text("\n".join(manifest) + "\n", encoding="utf-8")
            returncode, stdout, stderr = run_description_cli(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-vf",
                    f"select='gt(scene,{SCENE_CUT})',metadata=print:file=-",
                    "-fps_mode",
                    "passthrough",
                    "-f",
                    "null",
                    "-",
                ],
                "",
            )
            if returncode:
                raise RuntimeError(f"ffmpeg scene detection failed: {stderr[:1000]}")
            cuts = [
                float(value) for value in re.findall(r"pts_time:([0-9.eE+-]+)", stdout)
            ]
            selected = []
            for index in shot_frame_indices(starts, cuts, duration):
                output = workdir / f"shot-{len(selected) + 1:03d}.png"
                shutil.copyfile(frames_dir / f"frame-{index + 1:06d}.png", output)
                selected.append(DescriptionImage(output, starts[index]))
            return selected


@dataclass
class Transcript:
    has_audio: bool  # False: the video has no audio stream
    text: str  # recognised speech, one segment per line; "" = none


def description_image_prompt(
    prompt: str, images: list[DescriptionImage], transcript: Transcript | None = None
) -> str:
    if images[0].timestamp is None:
        return prompt
    timestamps = ", ".join(f"{image.timestamp:.3f} s" for image in images)
    source = "jednego filmu" if images[0].video else "jednej animacji"
    text = (
        prompt + f"\n\nZałączone obrazy to wybrane klatki {source}, "
        f"w kolejności chronologicznej ({timestamps}). "
        "Klatki zostały już wyodrębnione: analizuj je razem i opisz jako jeden mem. "
        "Nie wykonuj ponownej ekstrakcji."
    )
    if images[0].video:
        text += "\n\n" + audio_note(transcript)
    return text


def audio_note(transcript: Transcript | None) -> str:
    if transcript is None:
        return "Ścieżka dźwiękowa filmu nie została dołączona."
    if not transcript.has_audio:
        return "Film nie ma ścieżki dźwiękowej."
    if not transcript.text:
        return (
            "Automatyczna transkrypcja nie rozpoznała w ścieżce dźwiękowej mowy "
            "ani śpiewu (np. sama muzyka lub cisza)."
        )
    return (
        "Automatyczna transkrypcja ścieżki dźwiękowej filmu (może zawierać błędy; "
        f"♪ oznacza śpiew):\n{transcript.text}\n"
        "Uwzględnij w opisie mema to, co słychać. Pole transcription zawiera "
        "wyłącznie tekst widoczny na klatkach."
    )


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


def image_data_url(path: Path, *, preserve_size: bool = False) -> str:
    suffix = path.suffix.lower()
    if suffix in MIME:
        with Image.open(path) as img:
            small_enough = preserve_size or max(img.size) <= MAX_SIDE
        if small_enough:
            b64 = base64.b64encode(path.read_bytes()).decode()
            return f"data:{MIME[suffix]};base64,{b64}"
    # Animation frames arrive as prepared PNGs and retain native resolution.
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def total_tokens(usage: dict | None) -> int | None:
    """Read reported usage, keeping missing/invalid counts distinct from zero."""
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    # OpenCode's step_finish tokens use their native input/output/cache shape.
    # Only use its explicit total; guessing could double-count reasoning/cache.
    if "input" in usage or "output" in usage:
        total = usage.get("total")
        return total if type(total) is int and total >= 0 else None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    else:
        counts = [usage.get("input_tokens"), usage.get("output_tokens")]
        # Claude reports cache reads/creation separately from input_tokens;
        # Codex includes cached input in input_tokens already.
        counts += [
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
        ]
    if all(type(count) is int and count >= 0 for count in counts):
        return sum(counts)
    return None


def request_description(
    cfg: dict, prompt: str, images: list[str]
) -> tuple[str, dict | None]:
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
                    *[
                        {"type": "image_url", "image_url": {"url": image}}
                        for image in images
                    ],
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
    return body["choices"][0]["message"]["content"], body.get("usage")


def run_description_cli(
    argv: list[str],
    body: str,
    *,
    cwd: str | None = None,
    verbose: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a CLI/helper with timeout and process-group interrupt handling."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        # Both streams captured, never inherited: stdout carries the result
        # envelope that tells a spent quota from a broken image, and stderr is
        # kept for the error message when there is no envelope at all. Under -v
        # stderr is echoed below instead.
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Own session, so the terminal's Ctrl+C reaches only us: the first press
        # lets this description finish (see describe_memes). On a second press
        # we can explicitly terminate the whole process group.
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(body, timeout=REQUEST_TIMEOUT)
    except KeyboardInterrupt:
        # subprocess.run() does not kill a child started in a new session when
        # interrupted. Signal the group so any CLI descendants are stopped too.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    except subprocess.TimeoutExpired:
        # Match subprocess.run's timeout behavior, including reaping the child.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    if verbose and stderr:
        print(stderr, end="", file=sys.stderr)
    return proc.returncode, stdout, stderr


def jsonl_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def validate_description(raw: str) -> None:
    """Enforce the canonical description contract across CLI backends."""
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) != set(SCHEMA["properties"]):
        raise ValueError("output must contain exactly the description fields")
    for key, spec in SCHEMA["properties"].items():
        value = data[key]
        if spec["type"] == "string":
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
        else:
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                raise ValueError(f"{key} must be an array of strings")
            if not spec["minItems"] <= len(value) <= spec["maxItems"]:
                raise ValueError(f"{key} has an invalid number of items")
            if spec.get("uniqueItems") and len(set(value)) != len(value):
                raise ValueError(f"{key} contains duplicates")
            enum = spec["items"].get("enum")
            if enum and any(v not in enum for v in value):
                raise ValueError(f"{key} contains an unknown value")


def description_json_prompt(prompt: str) -> str:
    return (
        f"{prompt}\n\nOpisz załączony mem. Zwróć wyłącznie JSON zgodny "
        "z poniższym schematem, bez Markdown ani komentarzy. "
        "Nazwy kluczy muszą być dokładnie takie jak w schemacie:\n"
        + json.dumps(SCHEMA, ensure_ascii=False)
    )


def request_description_cli(
    cfg: dict, prompt: str, images: list[DescriptionImage], *, verbose: bool = False
) -> tuple[str, dict | None]:
    """Describe through the local Claude Code CLI instead of an HTTP endpoint.

    Billed against the Claude Code subscription, so no api_key/base_url is used.
    Streamed image blocks need no Read tool. Only the schema-bound output
    tool is available; no built-in or MCP tools can access the host.
    """
    argv = [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--input-format",
        "stream-json",
        "--tools",
        "",
        "--disallowed-tools",
        "mcp__*",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--json-schema",
        json.dumps(SCHEMA, ensure_ascii=False),
        # No CLAUDE.md (global or project), skills, plugins, hooks or MCP: the
        # subprocess should see the meme prompt, not this repo's coding context.
        # Unlike --bare it keeps normal auth, so the run stays on the subscription.
        "--safe-mode",
        "--model",
        cfg["model"],
    ]
    if cfg.get("effort_level"):
        argv += ["--effort", cfg["effort_level"]]
    content = [
        {
            "type": "text",
            "text": f"{prompt}\n\nOpisz załączony mem. "
            "Wynik oddaj przez structured output, nie jako tekst.",
        }
    ]
    for source in images:
        # Preserve Claude's native-size inputs, including animation frames.
        with Image.open(source.path) as image:
            buf = io.BytesIO()
            image.convert("RGB").save(buf, "PNG")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(buf.getvalue()).decode(),
                },
            }
        )
    body = (
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": content}},
            ensure_ascii=False,
        )
        + "\n"
    )
    with tempfile.TemporaryDirectory(prefix="meme-claude-") as tmp:
        returncode, stdout, stderr = run_description_cli(
            argv, body, cwd=tmp, verbose=verbose
        )
    # A failed run still prints its result envelope (exit 1, is_error=true),
    # and that envelope is the only place the cause is spelled out.
    results = [event for event in jsonl_events(stdout) if event.get("type") == "result"]
    if not results:
        detail = ((stderr or "") + (stdout or "")).strip()
        raise RuntimeError(f"claude exited {returncode}: {detail[:500] or 'no output'}")
    envelope = results[-1]
    if returncode != 0 or envelope.get("is_error"):
        # `result` first: the leading usage/session-id keys used to fill the
        # whole truncated message and hide the actual reason.
        detail = (
            f"{envelope.get('result', '')} "
            f"[api_error={envelope.get('api_error')}, "
            f"status={envelope.get('api_error_status')}, "
            f"exit={returncode}]"
        )
        if is_usage_limit(envelope):
            raise UsageLimitError(detail)
        raise RuntimeError(f"claude: {detail}")
    data = envelope.get("structured_output")
    if not isinstance(data, dict) or not data:
        raise ValueError("claude completed without structured output")
    raw = json.dumps(data, ensure_ascii=False)
    validate_description(raw)
    return raw, envelope.get("usage")


class DescriptionCLIError(RuntimeError):
    """An incomplete CLI run must not mark images as done (quota/auth/network)."""


class CodexCLIError(DescriptionCLIError):
    pass


class OpenCodeCLIError(DescriptionCLIError):
    pass


class TranscriptionError(DescriptionCLIError):
    """The ASR service failed, which says nothing about the video."""


def speech_text(segments: list[dict]) -> str:
    """Join recognised segments, dropping placeholders and immediate repeats."""
    lines = []
    for segment in segments:
        text = " ".join(str(segment.get("text", "")).split())
        bare = re.sub(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)", " ", text)
        bare = " ".join(re.sub(r"[^\w\s]", " ", bare).split()).casefold()
        if bare not in NON_SPEECH and (not lines or lines[-1] != text):
            lines.append(text)
    return "\n".join(lines)


def request_transcription(cfg: dict, wav: Path) -> tuple[str, str | None]:
    """POST the WAV as an OpenAI-style multipart transcription request."""
    boundary = uuid.uuid4().hex
    fields = {"response_format": "verbose_json"}
    for key in ("model", "language"):
        if cfg.get(key):
            fields[key] = cfg[key]
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n".encode()
        for name, value in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'
    ).encode()
    body += wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    url = cfg["base_url"].rstrip("/") + cfg.get("endpoint", "/audio/transcriptions")
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TRANSCRIBE_TIMEOUT) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode(errors="replace")
        raise TranscriptionError(
            f"transcription failed: HTTP {e.code} from {url}: {detail}"
        ) from e
    except (OSError, ValueError) as e:
        raise TranscriptionError(f"transcription failed ({url}): {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("text", ""), str):
        raise TranscriptionError(f"unexpected transcription response from {url}")
    segments = data.get("segments")
    if not isinstance(segments, list):  # services answering only with plain text
        segments = [{"text": line} for line in data.get("text", "").splitlines()]
    return speech_text(segments), data.get("language")


def video_transcript(
    con: sqlite3.Connection, digest: str, path: str, cfg: dict | None
) -> Transcript | None:
    """Cached soundtrack transcript; None for images or without [transcribe]."""
    if not cfg or not common.is_video(path):
        return None
    row = con.execute(
        "SELECT has_audio, text FROM audio_transcripts WHERE hash = ?", (digest,)
    ).fetchone()
    if row:
        return Transcript(bool(row[0]), row[1])
    text, language = "", None
    with tempfile.TemporaryDirectory(prefix="meme-audio-") as tmp:
        wav = common.extract_audio(Path(path), Path(tmp))
        if wav is not None:
            text, language = request_transcription(cfg, wav)
    con.execute(
        "INSERT OR REPLACE INTO audio_transcripts "
        "(hash, has_audio, text, language, model) VALUES (?, ?, ?, ?, ?)",
        (digest, wav is not None, text, language, cfg.get("model")),
    )
    con.commit()  # transcribed once, even if describing this video fails
    return Transcript(wav is not None, text)


def prepare_cli_attachments(
    images: list[DescriptionImage], workdir: Path
) -> list[Path]:
    attachments = []
    for index, source in enumerate(images, 1):
        image_path = workdir / f"image-{index:03d}.png"
        with Image.open(source.path) as image:
            image = image.convert("RGB")
            if source.timestamp is None:
                image.thumbnail((MAX_SIDE, MAX_SIDE))
            image.save(image_path)
        attachments.append(image_path)
    return attachments


def request_description_opencode_cli(
    cfg: dict,
    prompt: str,
    images: list[DescriptionImage],
    *,
    transcript: Transcript | None = None,
    verbose: bool = False,
) -> tuple[str, dict | None]:
    """OpenCode V2: attached images, isolated config and no model tools."""
    model = cfg["model"]
    if cfg.get("effort_level"):
        if "#" in model:
            raise ValueError("opencode: use effort_level or a #variant, not both")
        model += "#" + cfg["effort_level"]
    with tempfile.TemporaryDirectory(prefix="meme-opencode-") as tmp:
        workdir = Path(tmp)
        attachments = prepare_cli_attachments(images, workdir)
        sizes = []
        for attachment in attachments:
            with Image.open(attachment) as image:
                sizes.append(image.size)
        deny = [{"action": "*", "resource": "*", "effect": "deny"}]
        # V2 implements even agent/provider config and image handling as plugins.
        # Keep only those built-ins and provider authentication/routing.
        builtin_plugins = (
            "opencode.config.agent",
            "opencode.config.provider",
            "opencode.config.image",
            "opencode.config.policy",
            "opencode.models.dev",
            "opencode.provider.*",
        )
        config = {
            "update": "disable",
            "share": "disabled",
            "snapshots": False,
            "formatter": False,
            "lsp": False,
            "plugins": ["-*", *builtin_plugins],
            "permissions": deny,
            "policies": [
                {"action": "tool.use", "resource": "*", "effect": "deny"},
                {"action": "integration.use", "resource": "mcp:*", "effect": "deny"},
                {"action": "integration.use", "resource": "skill:*", "effect": "deny"},
                {"action": "integration.use", "resource": "plugin:*", "effect": "deny"},
                *[
                    {
                        "action": "integration.use",
                        "resource": f"plugin:{plugin}",
                        "effect": "allow",
                    }
                    for plugin in builtin_plugins
                ],
            ],
            "agents": {
                "meme-describe": {
                    "mode": "primary",
                    "system": prompt,
                    "permissions": deny,
                }
            },
            # Frame selection and still scaling are already done by Python.
            "media": {
                "image": {
                    "auto_resize": False,
                    "max_width": max(width for width, _ in sizes),
                    "max_height": max(height for _, height in sizes),
                    "max_base64_bytes": max(
                        (path.stat().st_size + 2) // 3 * 4 for path in attachments
                    ),
                }
            },
        }
        env = dict(os.environ)
        env.pop("OPENCODE_CONFIG", None)
        env.pop("OPENCODE_CLI_CONFIG_CONTENT", None)
        env.update(
            # OpenCode run uses PWD in preference to the subprocess cwd.
            PWD=tmp,
            XDG_CONFIG_HOME=str(workdir / "xdg-config"),
            OPENCODE_CONFIG_DIR=str(workdir / "config"),
            OPENCODE_CONFIG_PROJECT_DISABLE="1",
            OPENCODE_DISABLE_CLAUDE_CODE="1",
            OPENCODE_DISABLE_FILEWATCHER="1",
            OPENCODE_CONFIG_CONTENT=json.dumps(config, ensure_ascii=False),
        )
        argv = [
            "opencode",
            "run",
            "--standalone",
            "--format",
            "json",
            "--agent",
            "meme-describe",
            "--title",
            "Meme description",
            "--model",
            model,
        ]
        for attachment in attachments:
            argv += ["--file", str(attachment)]
        argv += [
            "--",
            description_image_prompt(description_json_prompt(""), images, transcript),
        ]
        try:
            returncode, stdout, stderr = run_description_cli(
                argv, "", cwd=tmp, env=env, verbose=verbose
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(
                f"opencode timed out after {error.timeout} seconds"
            ) from error
        except OSError as error:
            raise OpenCodeCLIError(f"opencode: {error}") from error
        events = jsonl_events(stdout)
        errors = [event for event in events if event.get("type") == "error"]
        finished = [event for event in events if event.get("type") == "step_finish"]
        if returncode or errors:
            detail = (
                json.dumps(errors, ensure_ascii=False) if errors else stderr or stdout
            )
            hint = ""
            if cfg["model"].startswith("opencode/") and any(
                event.get("error", {}).get("type") == "provider.free-limit"
                for event in errors
            ):
                # This gateway error also occurs with restricted agents;
                # its typed name alone does not prove that quota was consumed.
                hint = (
                    "\nOpenCode free-tier requests can also be rejected when tools "
                    "are disabled. This adapter deliberately sends tools: []; "
                    "the error does not establish that your quota is exhausted. "
                    "See https://github.com/anomalyco/opencode/issues/51241"
                )
            raise OpenCodeCLIError(
                f"opencode exited {returncode}: {detail[:1000] or 'no completed step'}{hint}"
            )
        if finished and finished[-1].get("part", {}).get("reason") != "stop":
            raise OpenCodeCLIError("opencode did not finish the description normally")
        if any(event.get("type") == "tool_use" for event in events):
            raise OpenCodeCLIError("opencode unexpectedly attempted to use a tool")
        raw = "".join(
            event["part"]["text"] for event in events if event.get("type") == "text"
        )
        if not raw.strip():
            raise OpenCodeCLIError("opencode completed without a description")
        validate_description(raw)
        # V2's catch-up path can emit only completed text, with no step events.
        # Exit 0 plus a valid full description still means success; usage is
        # unknown then, rather than a made-up zero.
        usage = finished[-1].get("part", {}).get("tokens") if finished else None
        return raw, usage


def request_description_codex_cli(
    cfg: dict,
    prompt: str,
    images: list[DescriptionImage],
    *,
    transcript: Transcript | None = None,
    verbose: bool = False,
) -> tuple[str, dict | None]:
    """Use the versioned prompt as Codex's replacement model instructions."""
    with tempfile.TemporaryDirectory(prefix="meme-codex-") as tmp:
        workdir = Path(tmp)
        output = workdir / "description.json"
        schema_path = workdir / "schema.json"
        instructions_path = workdir / "instructions.txt"
        instructions_path.write_text(prompt, encoding="utf-8")
        # Codex structured output requires additionalProperties=false and does
        # not support uniqueItems. Keep the canonical schema untouched; check
        # uniqueness locally below.
        schema = json.loads(json.dumps(SCHEMA))
        schema["additionalProperties"] = False
        for spec in schema["properties"].values():
            spec.pop("uniqueItems", None)
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        attachments = prepare_cli_attachments(images, workdir)
        argv = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--disable",
            "hooks",
            "--disable",
            "apps",
            "--disable",
            "remote_plugin",
            "--disable",
            "plugins",
            "--disable",
            "multi_agent",
            "--disable",
            "goals",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--disable",
            "code_mode",
            "--disable",
            "view_image",
            "--disable",
            "skill_search",
            "--enable",
            "skip_host_skill_discovery",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "--color",
            "never",
            "--model",
            cfg["model"],
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output),
            "-c",
            "model_instructions_file=" + json.dumps(str(instructions_path)),
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            'web_search="disabled"',
            "-c",
            "features.shell_tool=false",
            "-c",
            "tools.experimental_request_user_input.enabled=false",
            "-c",
            "tools.update_plan.enabled=false",
            "-c",
            'approval_policy="never"',
        ]
        env = None
        if cfg.get("_codex_provider_id"):
            provider = dict(cfg["_codex_provider"])
            env = dict(os.environ)
            # Keep credentials out of argv, process listings and timeout errors.
            token = provider.pop("experimental_bearer_token", None)
            if token is not None:
                env["MEME_CODEX_PROVIDER_TOKEN"] = token
                provider["env_key"] = "MEME_CODEX_PROVIDER_TOKEN"
            headers = dict(provider.pop("env_http_headers", {}))
            for index, (name, value) in enumerate(
                provider.pop("http_headers", {}).items()
            ):
                key = f"MEME_CODEX_PROVIDER_HEADER_{index}"
                env[key] = value
                headers[name] = key
            if headers:
                provider["env_http_headers"] = headers
            provider_id = cfg["_codex_provider_id"]
            argv += ["-c", "model_provider=" + json.dumps(provider_id)]
            fields = []
            for name, value in provider.items():
                # These settings are scalar values, except header-name → env
                # mappings. JSON string escaping is also valid TOML escaping.
                if isinstance(value, dict):
                    encoded = (
                        "{"
                        + ", ".join(
                            json.dumps(k) + "=" + json.dumps(v)
                            for k, v in value.items()
                        )
                        + "}"
                    )
                else:
                    encoded = json.dumps(value)
                fields.append(json.dumps(name) + "=" + encoded)
            if fields:
                table = "{" + ", ".join(fields) + "}"
                argv += [
                    "-c",
                    "model_providers={" + json.dumps(provider_id) + "=" + table + "}",
                ]
        for attachment in attachments:
            argv += ["--image", str(attachment)]
        if cfg.get("effort_level"):
            argv += ["-c", "model_reasoning_effort=" + json.dumps(cfg["effort_level"])]
        argv.append("-")
        body = description_image_prompt(
            "Opisz załączony mem. Zwróć wyłącznie JSON zgodny ze schematem.",
            images,
            transcript,
        )
        try:
            cli_options = {"cwd": tmp, "verbose": verbose}
            if env is not None:
                cli_options["env"] = env
            returncode, stdout, stderr = run_description_cli(argv, body, **cli_options)
        except subprocess.TimeoutExpired as error:
            # A slow image must not stop the queue like quota/auth failures do.
            raise TimeoutError(
                f"codex timed out after {error.timeout} seconds"
            ) from error
        except OSError as error:
            raise CodexCLIError(f"codex: {error}") from error
        events = jsonl_events(stdout)
        failures = [e for e in events if e.get("type") == "turn.failed"]
        completed = [e for e in events if e.get("type") == "turn.completed"]
        if returncode or failures or not completed:
            errors = failures or [e for e in events if e.get("type") == "error"]
            detail = (
                json.dumps(errors, ensure_ascii=False) if errors else stderr or stdout
            )
            raise CodexCLIError(
                f"codex exited {returncode}: {detail[:1000] or 'no completed turn'}"
            )
        if not output.is_file() or not output.read_text(encoding="utf-8").strip():
            raise CodexCLIError("codex completed without a final response")
        raw = output.read_text(encoding="utf-8")
        validate_description(raw)
        return raw, completed[-1].get("usage")


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
        label = key
        if key == "the_clue" and sys.stdout.isatty():
            label = f"\033[1m{key}\033[22m"
        padding = " " * max(0, KEY_WIDTH - len(key))
        print(f"  {label}{padding} {lines[0] if lines else ''}".rstrip())
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
            "SELECT hash, path FROM meme_files WHERE path = ?", (target,)
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
    undescribed_only: bool = False,
    undescribed_first: bool = False,
    shuffle: bool = False,
    verbose: bool = False,
    paths: list[str] | None = None,
    transcribe: dict | None = None,
) -> None:
    if undescribed_only and paths:
        raise SystemExit("--undescribed-only cannot be combined with --path")
    version = cfg["prompt_version"]
    model = cfg["model"]
    backend = cfg.get("backend", "openai")
    if backend not in ("openai", "claude-code-cli", "codex-cli", "opencode-cli"):
        raise SystemExit(f"unknown describe backend: {backend}")
    if backend == "opencode-cli":
        if not re.fullmatch(r"[^/#]+/[^#]+(?:#[^#]+)?", model):
            raise SystemExit("opencode: model must use provider/model[#variant]")
        if cfg.get("effort_level") and "#" in model:
            raise SystemExit("opencode: use effort_level or a #variant, not both")
    executable = {
        "claude-code-cli": "claude",
        "codex-cli": "codex",
        "opencode-cli": "opencode",
    }.get(backend)
    if executable and not shutil.which(executable):
        raise SystemExit(f"{backend}: executable not found: {executable}")
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
    undescribed_filter = (
        "AND NOT EXISTS (SELECT 1 FROM descriptions prior "
        "WHERE prior.hash = m.hash AND prior.status = 'described')"
        if undescribed_only
        else ""
    )
    order_by = "RANDOM()" if shuffle else "m.hash"
    if undescribed_first:
        order_by = (
            "EXISTS (SELECT 1 FROM descriptions prior "
            "WHERE prior.hash = m.hash AND prior.status = 'described'), " + order_by
        )
    if paths:
        # --path overrides the queue entirely, skip logic included: re-describing
        # one meme under an unchanged config is the whole point (prompt
        # iteration), and descriptions_config_idx turns the write into a replace
        # rather than a duplicate. Announce it so nothing is overwritten silently.
        rows = rows_for_paths(con, paths)
        if shuffle:
            random.shuffle(rows)
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
              AND EXISTS (SELECT 1 FROM meme_files f WHERE f.hash = m.hash)
              {undescribed_filter}
              AND NOT EXISTS (
                  SELECT 1 FROM descriptions d
                  WHERE d.hash = m.hash
                    AND d.model = ?
                    AND d.prompt_version = ?
                    {effort_filter}
                    {dedup_filter}
              )
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
        if limit:
            rows = rows[:limit]

    # First Ctrl+C only raises this flag: the description in flight is paid for
    # (minutes, or subscription quota), so it is finished and committed before
    # the loop stops. A second Ctrl+C is the default KeyboardInterrupt.
    stop = False

    def request_stop(signum, frame) -> None:
        nonlocal stop
        stop = True
        signal.signal(signal.SIGINT, signal.default_int_handler)
        print(
            "\nstopping after the current description (Ctrl+C again to abort now)",
            file=sys.stderr,
        )

    previous_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        done, failed = describe_rows(
            con, cfg, rows, prompt, backend, verbose, lambda: stop, transcribe
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    if stop:
        print(f"stopped: described {done}/{len(rows)}, {failed} failed")
        # 128 + SIGINT; exiting rather than returning also keeps `index` from
        # moving on to embed.
        raise SystemExit(130)


def describe_rows(
    con: sqlite3.Connection,
    cfg: dict,
    rows: list[tuple[str, str]],
    prompt: str,
    backend: str,
    verbose: bool,
    stopping,
    transcribe: dict | None = None,
) -> tuple[int, int]:
    """Describe rows in order; videos are transcribed first when [transcribe]
    is configured, and the model gets the frames plus the transcript."""
    version = cfg["prompt_version"]
    model = cfg["model"]
    effort = cfg.get("effort_level")
    done = failed = 0
    for digest, path in rows:
        if stopping():
            break
        # Retried in place on a usage limit, so the loop advances only once
        # this meme is either described or recorded as failed.
        while True:
            try:
                tokens = None
                usage_json = None
                transcript = video_transcript(con, digest, path, transcribe)
                if verbose and transcript is not None:
                    lines = transcript.text.count("\n") + bool(transcript.text)
                    print(
                        f"{Path(path).name}: "
                        + (
                            f"{lines} transcript lines"
                            if transcript.has_audio
                            else "no audio stream"
                        )
                    )
                with tempfile.TemporaryDirectory(prefix="meme-images-") as tmp:
                    images = prepare_description_images(Path(path), Path(tmp))
                    image_prompt = description_image_prompt(prompt, images, transcript)
                    if verbose and images[0].timestamp is not None:
                        print(
                            f"{Path(path).name}: selected {len(images)} animation frames"
                        )
                    if backend == "claude-code-cli":
                        started = time.monotonic()
                        raw, usage = request_description_cli(
                            cfg, image_prompt, images, verbose=verbose
                        )
                    elif backend == "codex-cli":
                        started = time.monotonic()
                        raw, usage = request_description_codex_cli(
                            cfg, prompt, images, transcript=transcript, verbose=verbose
                        )
                    elif backend == "opencode-cli":
                        started = time.monotonic()
                        raw, usage = request_description_opencode_cli(
                            cfg, prompt, images, transcript=transcript, verbose=verbose
                        )
                    else:
                        urls = [
                            image_data_url(
                                image.path, preserve_size=image.timestamp is not None
                            )
                            for image in images
                        ]
                        started = time.monotonic()
                        raw, usage = request_description(cfg, image_prompt, urls)
                if isinstance(usage, dict):
                    usage_json = json.dumps(usage, ensure_ascii=False)
                    tokens = total_tokens(usage)
                elapsed = time.monotonic() - started
                data = json.loads(raw)
                missing = [k for k in SCHEMA["required"] if k not in data]
                if missing:
                    raise ValueError(f"VLM output missing keys: {missing}")
                con.execute(
                    "INSERT OR REPLACE INTO descriptions "
                    "(hash, description_json, prompt_version, model, effort_level, "
                    "total_tokens, usage_json, status, error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'described', NULL)",
                    (digest, raw, version, model, effort, tokens, usage_json),
                )
                done += 1
                if verbose:
                    where = Path(path)
                    token_log = f"[{tokens:,} tokens]  " if tokens is not None else ""
                    print(
                        f"\n{time.strftime('%H:%M:%S')} [{done}/{len(rows)}] "
                        f"{elapsed:.1f}s/{REQUEST_TIMEOUT}s  "
                        f"{token_log}"
                        f"{where.parent.name}/{where.name}"
                    )
                    print_description(data)
                break
            except DescriptionCLIError as e:
                # JSONL errors need not carry a typed quota code. Stop without
                # poisoning the queue or guessing from localized prose.
                raise SystemExit(
                    f"{e}\nStopped; no description row written for {path}. Re-run to resume."
                ) from e
            except UsageLimitError as e:
                # Not this meme's fault and not a permanent failure: no row
                # is written, the queue is left untouched, and the same image
                # goes out again once the window has rolled over.
                print(
                    f"{time.strftime('%H:%M:%S')} usage limit hit, waiting "
                    f"{USAGE_LIMIT_WAIT // 60} min before retrying {Path(path).name}: {e}"
                )
                # Slept in short slices so Ctrl+C during the wait exits at once:
                # nothing is in flight, so there is nothing to finish.
                deadline = time.monotonic() + USAGE_LIMIT_WAIT
                while time.monotonic() < deadline and not stopping():
                    time.sleep(1)
                if stopping():
                    break
            # Any other error is this image's failure: recorded and moved past,
            # since a single bad image must never kill a run.
            except Exception as e:  # noqa: BLE001
                con.execute(
                    "INSERT OR REPLACE INTO descriptions "
                    "(hash, description_json, prompt_version, model, effort_level, "
                    "total_tokens, usage_json, status, error) "
                    "VALUES (?, NULL, ?, ?, ?, ?, ?, 'failed_describe', ?)",
                    (digest, version, model, effort, tokens, usage_json, str(e)),
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
    return done, failed


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-pick rows with status=failed_describe",
    )
    parser.add_argument("--limit", type=int, default=0, help="describe at most N memes")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--path",
        nargs="+",
        metavar="FILE",
        help="describe these files only; ignores the queue, the duplicate filter "
        "and skip logic, so an existing row for the same config is replaced",
    )
    selection.add_argument(
        "--undescribed-only",
        action="store_true",
        help="only pick memes without any successful description, regardless of "
        "model, prompt or effort; use --retry-failed to retry failures",
    )
    selection.add_argument(
        "--undescribed-first",
        action="store_true",
        help="describe memes without any successful description first, then other "
        "eligible memes; apply --limit after this ordering",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="randomize description order before --limit; with --undescribed-first, "
        "randomize within each priority group",
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
        undescribed_only=args.undescribed_only,
        undescribed_first=args.undescribed_first,
        shuffle=args.shuffle,
        verbose=args.verbose,
        paths=args.path,
        transcribe=cfg.get("transcribe"),
    )
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 2: describe memes with a VLM")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
