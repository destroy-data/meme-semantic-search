"""Stage 3: embed descriptions with bge-m3 and fill the vector + FTS indexes."""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable

import sqlite_vec

from memy import common


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


# Preference among one meme's descriptions (or index entries, same columns)
# for the active variant :model/:prompt/:effort; shared by embed and search.
FALLBACK_ORDER = """
    CASE
        WHEN model = :model AND prompt_version = :prompt
         AND IFNULL(effort_level, '') = IFNULL(:effort, '') THEN 1
        WHEN model = :model AND prompt_version = :prompt THEN 2
        WHEN prompt_version = :prompt THEN 3
        ELSE 4
    END
"""

# Bump when embedding_text() or fts_fields() change what gets indexed; entries
# built under an older version are rebuilt by the next embed run.
TEXT_VERSION = "t1"


def fts_fields(desc: dict) -> tuple[str, str, str]:
    return (
        desc.get("transcription", ""),
        ", ".join(desc.get("keywords", []) + desc.get("humor_type", [])),
        desc.get("template", ""),
    )


def remove_stale_entries(con: sqlite3.Connection) -> None:
    """Drop own entries whose description was replaced or failed since indexing."""
    with con:
        con.execute(
            """
            DELETE FROM index_entries
            WHERE source_hash = hash AND NOT EXISTS (
                SELECT 1 FROM descriptions d
                WHERE d.id = description_id AND d.hash = index_entries.hash
                  AND d.status = 'described'
            )
            """
        )


def pending_descriptions(
    con: sqlite3.Connection,
    describe_cfg: dict,
    models: list[str],
    selection: str,
) -> list[int]:
    """Description ids lacking a current entry or a vector from any of `models`.

    selection: "active" — per meme, the description search would pick for
    [describe] (FALLBACK_ORDER, newest first); "newest" — per meme, the newest
    success across every config; "all" — every successful description.
    """
    marks = ",".join(f":m{i}" for i in range(len(models)))
    rows = con.execute(
        f"""
        WITH ranked AS (
            SELECT
                d.id,
                ROW_NUMBER() OVER (
                    PARTITION BY m.hash
                    ORDER BY
                        CASE WHEN :selection = 'active' THEN {FALLBACK_ORDER}
                        ELSE 1 END,
                        d.id DESC
                ) AS rn
            FROM memes m
            JOIN descriptions d ON d.hash = m.hash
            WHERE m.perceptual_duplicate_of IS NULL
              AND EXISTS (SELECT 1 FROM meme_files f WHERE f.hash = m.hash)
              AND d.status = 'described'
        )
        SELECT r.id FROM ranked r
        LEFT JOIN index_entries e
            ON e.description_id = r.id AND e.source_hash = e.hash
           AND e.text_version = :text_version
        WHERE (r.rn = 1 OR :selection = 'all')
          AND (
            e.id IS NULL
            OR (SELECT COUNT(*) FROM embeddings v
                WHERE v.entry_id = e.id AND v.embed_model IN ({marks})) < :n_models
          )
        ORDER BY r.id
        """,
        {
            "selection": selection,
            "model": describe_cfg["model"],
            "prompt": describe_cfg["prompt_version"],
            "effort": describe_cfg.get("effort_level"),
            "text_version": TEXT_VERSION,
            "n_models": len(models),
            **{f"m{i}": m for i, m in enumerate(models)},
        },
    ).fetchall()
    return [i for (i,) in rows]


def embed_descriptions(
    con: sqlite3.Connection,
    describe_cfg: dict,
    embed_cfgs: list[dict],
    *,
    limit: int = 0,
    selection: str = "active",
    progress: Callable[[int, int, int], None] | None = None,
) -> None:
    """Index selected descriptions with every model in `embed_cfgs`."""
    remove_stale_entries(con)
    ids = pending_descriptions(
        con, describe_cfg, [c["model"] for c in embed_cfgs], selection
    )
    if limit:
        ids = ids[:limit]
    if not ids:
        print("embed: nothing to do")

    done = failed = 0
    if progress:
        progress(done, failed, len(ids))
    for description_id in ids:
        try:
            index_description(con, description_id, embed_cfgs)
            done += 1
        # Logged and re-picked by the next run, so nothing here may stop the loop.
        except Exception as e:  # noqa: BLE001
            con.rollback()
            failed += 1
            print(
                f"{time.strftime('%H:%M:%S')} failed description {description_id}: {e}",
                file=sys.stderr,
            )
        print(
            f"{time.strftime('%H:%M:%S')} embedded {done}/{len(ids)}, {failed} failed"
        )
        if progress:
            progress(done, failed, len(ids))


def index_description(
    con: sqlite3.Connection, description_id: int, embed_cfgs: list[dict]
) -> None:
    """Index one successful description: its FTS entry plus one vector per model.

    Other variants of the meme are left alone. Each vector commits on its own,
    so a failing model keeps the ones that succeeded; the error is re-raised.
    """
    row = con.execute(
        "SELECT hash, description_json, model, prompt_version, effort_level "
        "FROM descriptions WHERE id = ? AND status = 'described'",
        (description_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no successful description with id {description_id}")
    digest, raw, model, prompt, effort = row
    desc = json.loads(raw)
    text = embedding_text(desc)
    text_sha = hashlib.sha256(text.encode()).hexdigest()
    entry = con.execute(
        "SELECT id, text_version, text_sha256 FROM index_entries "
        "WHERE hash = ? AND description_id = ?",
        (digest, description_id),
    ).fetchone()
    with con:
        if entry and entry[1:] != (TEXT_VERSION, text_sha):
            # Changed composition: the old FTS text and vectors no longer match.
            con.execute("DELETE FROM index_entries WHERE id = ?", (entry[0],))
            entry = None
        if entry is None:
            entry_id = con.execute(
                "INSERT INTO index_entries (hash, source_hash, description_id, model, "
                "prompt_version, effort_level, text_version, text_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (digest, digest, description_id, model, prompt, effort)
                + (TEXT_VERSION, text_sha),
            ).lastrowid
            con.execute(
                "INSERT INTO entries_fts (rowid, transcription, keywords, template) "
                "VALUES (?, ?, ?, ?)",
                (entry_id, *fts_fields(desc)),
            )
        else:
            entry_id = entry[0]
    have = {
        m
        for (m,) in con.execute(
            "SELECT embed_model FROM embeddings WHERE entry_id = ?", (entry_id,)
        )
    }
    errors = []
    for cfg in embed_cfgs:
        if cfg["model"] in have:
            continue
        try:
            vector = common.request_embedding(cfg, text)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cfg['model']}: {e}")
            continue
        with con:
            con.execute(
                "INSERT INTO embeddings (entry_id, embed_model, dims, embedding) "
                "VALUES (?, ?, ?, ?)",
                (
                    entry_id,
                    cfg["model"],
                    len(vector),
                    sqlite_vec.serialize_float32(vector),
                ),
            )
    if errors:
        raise RuntimeError("; ".join(errors))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)
    parser.add_argument(
        "--limit", type=int, default=0, help="embed at most N descriptions"
    )
    parser.add_argument(
        "--model",
        help="override [describe].model: whose descriptions get embedded",
    )
    parser.add_argument(
        "--all-variants",
        action="store_true",
        help="embed every successful description (any model, prompt and effort), "
        "not only the active config's best per meme",
    )
    parser.add_argument(
        "--embed-model",
        action="append",
        metavar="NAME",
        help="only this configured embedding model (repeatable); default: all of "
        "[embed] and [[embed.extra]]",
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    if args.model:
        cfg["describe"]["model"] = args.model
    names = getattr(args, "embed_model", None)
    embed_cfgs = (
        [common.embed_config(cfg, name) for name in dict.fromkeys(names)]
        if names
        else common.embed_configs(cfg)
    )

    con = common.connect(cfg["db_path"])
    embed_descriptions(
        con,
        cfg["describe"],
        embed_cfgs,
        limit=args.limit,
        selection="all" if getattr(args, "all_variants", False) else "active",
    )
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 3: embed descriptions")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
