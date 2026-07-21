"""Stage 4: search memes with hybrid vector + FTS retrieval fused via RRF."""

import argparse
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import urllib.request

import sqlite_vec

REQUEST_TIMEOUT = 69  # Nice
EMBEDDING_DIM = 1024
CANDIDATES = 69  # per-list pool fed into RRF
RRF_K = 60
VIEWERS = ["imv", "feh", "nsxiv", "loupe"]


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
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
    norm = math.sqrt(sum(x * x for x in embedding))
    return [x / norm for x in embedding]


def fts_query(text: str) -> str:
    # Strip FTS5 syntax by keeping only word tokens, each quoted; OR keeps
    # recall high — RRF handles precision.
    tokens = re.findall(r"\w+", text)
    return " OR ".join(f'"{t}"' for t in tokens)


def vec_hits(con: sqlite3.Connection, vector: list[float]) -> list[str]:
    rows = con.execute(
        """
        SELECT v.hash FROM (
            SELECT hash, distance FROM memes_vec
            WHERE embedding MATCH ? AND k = ?
        ) v
        JOIN memes m ON m.hash = v.hash
        WHERE m.perceptual_duplicate_of IS NULL
        ORDER BY v.distance
        """,
        (sqlite_vec.serialize_float32(vector), CANDIDATES),
    ).fetchall()
    return [h for (h,) in rows]


def fts_hits(con: sqlite3.Connection, query: str) -> list[str]:
    match = fts_query(query)
    if not match:
        return []
    rows = con.execute(
        """
        SELECT f.hash FROM memes_fts f
        JOIN memes m ON m.hash = f.hash
        WHERE memes_fts MATCH ? AND m.perceptual_duplicate_of IS NULL
        ORDER BY rank LIMIT ?
        """,
        (match, CANDIDATES),
    ).fetchall()
    return [h for (h,) in rows]


def search(
    con: sqlite3.Connection, embed_cfg: dict, query: str, top_n: int
) -> list[str]:
    vector = request_embedding(embed_cfg, query)
    scores: dict[str, float] = {}
    for hits in (vec_hits(con, vector), fts_hits(con, query)):
        for rank, digest in enumerate(hits, start=1):
            scores[digest] = scores.get(digest, 0.0) + 1.0 / (RRF_K + rank)
    ranked = sorted(scores, key=lambda h: (-scores[h], h))[:top_n]
    return [
        con.execute("SELECT path FROM memes WHERE hash = ?", (h,)).fetchone()[0]
        for h in ranked
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 4: search memes")
    parser.add_argument(
        "query",
        nargs="+",
        help="free-text query (Polish or English), no quoting needed",
    )
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("-n", "--top", type=int, default=10, help="number of results")
    parser.add_argument(
        "--show", action="store_true", help="open results in an image viewer"
    )
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    con = connect(cfg["db_path"])
    paths = search(con, cfg["embed"], " ".join(args.query), args.top)
    con.close()

    for path in paths:
        print(path)
    if args.show and paths:
        viewer = next((v for v in VIEWERS if shutil.which(v)), None)
        if viewer:
            subprocess.run([viewer, *paths])
        else:
            print(
                f"no image viewer found (tried: {', '.join(VIEWERS)})", file=sys.stderr
            )


if __name__ == "__main__":
    main()
