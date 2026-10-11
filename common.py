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
        return tomllib.load(f)


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
        con.executescript(Path(__file__).with_name("schema.sql").read_text())
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
