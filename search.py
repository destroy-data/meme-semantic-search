"""Stage 4: search memes with hybrid vector + FTS retrieval fused via RRF."""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys

import sqlite_vec

import common

CANDIDATES = 69  # per-list pool fed into RRF
RRF_K = 60
VIEWERS = ["imv", "feh", "nsxiv", "loupe"]


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


def search_with_hashes(
    con: sqlite3.Connection,
    embed_cfg: dict,
    query: str,
    top_n: int,
    *,
    embedding_weight: float = 0.5,
) -> list[tuple[str, str]]:
    """Return ranked pairs; 0 means FTS only, 1 means embeddings only."""
    if not 0 <= embedding_weight <= 1:
        raise ValueError("embedding_weight must be between 0 and 1")
    rankings = []
    if embedding_weight > 0:
        vector = common.request_embedding(embed_cfg, query)
        rankings.append((embedding_weight, vec_hits(con, vector)))
    if embedding_weight < 1:
        rankings.append((1 - embedding_weight, fts_hits(con, query)))
    scores: dict[str, float] = {}
    for weight, hits in rankings:
        for rank, digest in enumerate(hits, start=1):
            scores[digest] = scores.get(digest, 0.0) + weight / (RRF_K + rank)
    ranked = sorted(scores, key=lambda h: (-scores[h], h))[:top_n]
    return [
        (h, con.execute("SELECT path FROM memes WHERE hash = ?", (h,)).fetchone()[0])
        for h in ranked
    ]


def search(
    con: sqlite3.Connection,
    embed_cfg: dict,
    query: str,
    top_n: int,
    *,
    embedding_weight: float = 0.5,
) -> list[str]:
    """Return ranked paths; the stable public API used by the CLI."""
    return [
        path
        for _, path in search_with_hashes(
            con, embed_cfg, query, top_n, embedding_weight=embedding_weight
        )
    ]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "query",
        nargs="+",
        help="free-text query (Polish or English), no quoting needed",
    )
    common.add_config_argument(parser)
    parser.add_argument("-n", "--top", type=int, default=10, help="number of results")
    parser.add_argument(
        "--show", action="store_true", help="open results in an image viewer"
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    con = common.connect(cfg["db_path"])
    paths = search(con, cfg["embed"], " ".join(args.query), args.top)
    con.close()

    for path in paths:
        print(path)
    if args.show and paths:
        viewer = next((v for v in VIEWERS if shutil.which(v)), None)
        if viewer:
            subprocess.run([viewer, *paths], check=False)  # viewer's exit code is noise
        else:
            print(
                f"no image viewer found (tried: {', '.join(VIEWERS)})", file=sys.stderr
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 4: search memes")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
