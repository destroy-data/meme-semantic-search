"""Shared plumbing for every stage: config, DB connection, query embeddings."""

import argparse
import json
import math
import sqlite3
import tomllib
import urllib.request
from pathlib import Path

import sqlite_vec

REQUEST_TIMEOUT = 69  # Nice
EMBEDDING_DIM = 1024


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    describe = cfg.get("describe", {})
    if describe.get("backend") == "codex-cli" and describe.get("codex_profile"):
        load_codex_profile(describe)
    return cfg


def load_codex_profile(cfg: dict) -> None:
    """Import model defaults and one provider, never user permissions/tools."""
    profile_path = Path(cfg["codex_profile"]).expanduser()
    with profile_path.open("rb") as f:
        profile = tomllib.load(f)
    base_path = profile_path.with_name("config.toml")
    base = {}
    if base_path != profile_path and base_path.is_file():
        with base_path.open("rb") as f:
            base = tomllib.load(f)
    for source, target in (
        ("model", "model"),
        ("model_reasoning_effort", "effort_level"),
    ):
        value = profile.get(source, base.get(source))
        if value is not None:
            cfg.setdefault(target, value)
    provider_id = profile.get("model_provider", base.get("model_provider", "openai"))
    provider = {
        **base.get("model_providers", {}).get(provider_id, {}),
        **profile.get("model_providers", {}).get(provider_id, {}),
    }
    if not provider and provider_id != "openai":
        raise ValueError(f"Codex provider definition not found: {provider_id}")
    # Provider helpers can run host commands. Only import inert connection
    # settings; all other Codex config remains ignored by the describe adapter.
    allowed = {
        "name",
        "base_url",
        "wire_api",
        "env_key",
        "experimental_bearer_token",
        "http_headers",
        "env_http_headers",
        "requires_openai_auth",
        "supports_websockets",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "websocket_connect_timeout_ms",
    }
    unsupported = provider.keys() - allowed
    if unsupported:
        raise ValueError(f"unsupported Codex provider fields: {sorted(unsupported)}")
    cfg["_codex_provider_id"] = provider_id
    cfg["_codex_provider"] = provider


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config.toml", help="path to config TOML")


def connect(
    db_path: str, *, init_schema: bool = False, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open the DB with sqlite-vec loaded.

    `init_schema` runs schema.sql (idempotent CREATE ... IF NOT EXISTS); only the
    scan stage needs it, every other stage expects the schema to already exist.
    `check_same_thread=False` is for the GUI, whose callbacks run on the DPG
    render thread — one at a time, so there's no real concurrent access.
    """
    con = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    if init_schema:
        # Seed legacy databases once; archived memes must not regain stale paths.
        had_meme_files = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'meme_files'"
        ).fetchone()
        con.executescript(Path(__file__).with_name("schema.sql").read_text())
        if not had_meme_files:
            con.execute(
                "INSERT OR IGNORE INTO meme_files(path, hash) SELECT path, hash FROM memes"
            )
            con.commit()
    columns = {row[1] for row in con.execute("PRAGMA table_info(descriptions)")}
    added_columns = False
    for name, sql_type in (("total_tokens", "INTEGER"), ("usage_json", "TEXT")):
        if columns and name not in columns:
            con.execute(f"ALTER TABLE descriptions ADD COLUMN {name} {sql_type}")
            added_columns = True
    if added_columns:
        con.commit()
    con.execute("PRAGMA foreign_keys = ON")
    return con


def request_embedding(cfg: dict, text: str) -> list[float]:
    payload = {"model": cfg["model"], "input": text}
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/embeddings",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = json.load(resp)
    embedding = body["data"][0]["embedding"]
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"expected {EMBEDDING_DIM} dims, got {len(embedding)}")
    # Normalize so vec0's default L2 distance ranks identically to cosine;
    # Ollama doesn't guarantee normalized output.
    norm = math.sqrt(sum(x * x for x in embedding))
    return [x / norm for x in embedding]
