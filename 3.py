"""Stage 3: embed descriptions with bge-m3 and fill the vector + FTS indexes."""

import argparse
import json
import math
import sqlite3
import sys
import time
import tomllib
import urllib.request

import sqlite_vec

REQUEST_TIMEOUT = 69  # Nice
EMBEDDING_DIM = 1024


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
    # Normalize so vec0's default L2 distance ranks identically to cosine;
    # Ollama doesn't guarantee normalized output.
    norm = math.sqrt(sum(x * x for x in embedding))
    return [x / norm for x in embedding]


def embedding_text(desc: dict) -> str:
    tags = ", ".join(desc.get("keywords", []) + desc.get("humor_type", []))
    parts = [
        desc.get("scene", ""),
        desc.get("usage_context", ""),
        desc.get("transcription", ""),
        tags,
    ]
    return "\n".join(p for p in parts if p)


def embed(
    con: sqlite3.Connection, describe_cfg: dict, embed_cfg: dict, limit: int
) -> None:
    # Per meme, prefer the description matching [describe] exactly; fall back to
    # the same model with any effort, then to any model. Prompt_version stays
    # strict — a prompt swap is an intentional experiment, not a fallback axis.
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT
                m.hash,
                d.description_json,
                ROW_NUMBER() OVER (
                    PARTITION BY m.hash
                    ORDER BY
                        CASE
                            WHEN d.model = :model
                             AND IFNULL(d.effort_level, '') = IFNULL(:effort, '')
                                THEN 1
                            WHEN d.model = :model THEN 2
                            ELSE 3
                        END,
                        d.id DESC
                ) AS rn
            FROM memes m
            JOIN descriptions d ON d.hash = m.hash
            WHERE m.perceptual_duplicate_of IS NULL
              AND d.status = 'described'
              AND d.prompt_version = :prompt
        )
        SELECT hash, description_json FROM ranked WHERE rn = 1
        ORDER BY hash
        """,
        {
            "model": describe_cfg["model"],
            "prompt": describe_cfg["prompt_version"],
            "effort": describe_cfg.get("effort_level"),
        },
    ).fetchall()
    already = {h for (h,) in con.execute("SELECT hash FROM memes_vec")}
    rows = [(h, d) for h, d in rows if h not in already]
    if limit:
        rows = rows[:limit]

    done = failed = 0
    for digest, raw in rows:
        try:
            desc = json.loads(raw)
            vector = request_embedding(embed_cfg, embedding_text(desc))
            # Per-row commit keeps fts + vec inserts atomic and the run resumable.
            con.execute(
                "INSERT INTO memes_vec (hash, embedding) VALUES (?, ?)",
                (digest, sqlite_vec.serialize_float32(vector)),
            )
            con.execute("DELETE FROM memes_fts WHERE hash = ?", (digest,))
            con.execute(
                "INSERT INTO memes_fts (hash, transcription, keywords, template) "
                "VALUES (?, ?, ?, ?)",
                (
                    digest,
                    desc.get("transcription", ""),
                    ", ".join(desc.get("keywords", []) + desc.get("humor_type", [])),
                    desc.get("template", ""),
                ),
            )
            con.commit()
            done += 1
        except Exception as e:
            con.rollback()
            failed += 1
            print(
                f"{time.strftime('%H:%M:%S')} failed {digest[:12]}: {e}",
                file=sys.stderr,
            )
        print(
            f"{time.strftime('%H:%M:%S')} embedded {done}/{len(rows)}, {failed} failed"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 3: embed descriptions")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--limit", type=int, default=0, help="embed at most N memes")
    parser.add_argument(
        "--model",
        help="override [describe].model: whose descriptions get embedded",
    )
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    if args.model:
        cfg["describe"]["model"] = args.model

    con = connect(cfg["db_path"])
    embed(con, cfg["describe"], cfg["embed"], args.limit)
    con.close()


if __name__ == "__main__":
    main()
