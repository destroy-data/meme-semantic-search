"""Stage 3: embed descriptions with bge-m3 and fill the vector + FTS indexes."""

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable

import sqlite_vec

import common


def embedding_text(desc: dict) -> str:
    tags = ", ".join(desc.get("keywords", []) + desc.get("humor_type", []))
    # Template names drift badly between runs ("Two Marmots" / "Marmot Holding
    # Back Friend" / ...), which makes them unreliable for FTS's exact-token
    # matching but fine for the vector, where near-synonyms land close together.
    # "unknown" is dropped: it carries no meaning and, being shared by a large
    # share of memes, would pull all of them toward one another.
    template = desc.get("template", "")
    parts = [
        "" if template == "unknown" else template,
        desc.get("scene", ""),
        desc.get("the_clue", ""),
        desc.get("transcription", ""),
        tags,
    ]
    return "\n".join(p for p in parts if p)


def embed_descriptions(
    con: sqlite3.Connection,
    describe_cfg: dict,
    embed_cfg: dict,
    *,
    limit: int = 0,
    all_descriptions: bool = False,
    progress: Callable[[int, int, int], None] | None = None,
) -> None:
    # Per meme, prefer the description matching [describe] exactly; fall back to
    # the same model with any effort, then to any model. Prompt_version stays
    # strict — a prompt swap is an intentional experiment, not a fallback axis.
    # The GUI opts into all descriptions: newest success across every config,
    # with no preference for the active model, prompt or effort.
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
                            WHEN :all_descriptions THEN 1
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
              AND EXISTS (SELECT 1 FROM meme_files f WHERE f.hash = m.hash)
              AND d.status = 'described'
              AND (:all_descriptions OR d.prompt_version = :prompt)
        )
        SELECT hash, description_json FROM ranked WHERE rn = 1
        ORDER BY hash
        """,
        {
            "all_descriptions": all_descriptions,
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
    if progress:
        progress(done, failed, len(rows))
    for digest, raw in rows:
        try:
            embed_description(con, digest, raw, embed_cfg)
            done += 1
        # Logged and re-picked by the next run, so nothing here may stop the loop.
        except Exception as e:  # noqa: BLE001
            con.rollback()
            failed += 1
            print(
                f"{time.strftime('%H:%M:%S')} failed {digest[:12]}: {e}",
                file=sys.stderr,
            )
        print(
            f"{time.strftime('%H:%M:%S')} embedded {done}/{len(rows)}, {failed} failed"
        )
        if progress:
            progress(done, failed, len(rows))


def embed_description(
    con: sqlite3.Connection,
    digest: str,
    raw: str,
    cfg: dict,
    *,
    replace: bool = False,
) -> None:
    """Index one exact description; preserve existing indexes on failure."""
    desc = json.loads(raw)
    vector = common.request_embedding(cfg, embedding_text(desc))
    with con:
        if replace:
            con.execute("DELETE FROM memes_vec WHERE hash = ?", (digest,))
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)
    parser.add_argument("--limit", type=int, default=0, help="embed at most N memes")
    parser.add_argument(
        "--model",
        help="override [describe].model: whose descriptions get embedded",
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    if args.model:
        cfg["describe"]["model"] = args.model

    con = common.connect(cfg["db_path"])
    embed_descriptions(con, cfg["describe"], cfg["embed"], limit=args.limit)
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 3: embed descriptions")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
