"""Stage 4: search memes with hybrid vector + FTS retrieval fused via RRF."""

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys

import sqlite_vec

from memy import common, embed

CANDIDATES = 69  # per-list pool fed into RRF
RRF_K = 60
VIEWERS = ["imv", "feh", "nsxiv", "loupe"]


def fts_query(text: str) -> str:
    # Strip FTS5 syntax by keeping only word tokens, each quoted; OR keeps
    # recall high — RRF handles precision.
    tokens = re.findall(r"\w+", text)
    return " OR ".join(f'"{t}"' for t in tokens)


def selected_entries(variant: dict | None, exact: bool) -> tuple[str, dict]:
    """CTE `selected(id, hash)`: the index entries a search ranks.

    variant None: every entry of every meme (a meme scores by its best entry).
    Otherwise one entry per meme, ranked by embed.FALLBACK_ORDER: the exact
    variant, then same model and prompt, then same prompt, then anything,
    newest first. `exact` keeps only the exact variant. Only entries with a
    vector from :embed_model count, so the FTS side ranks the same descriptions.
    """
    sql = f"""
        WITH candidates AS (
            SELECT e.id, e.hash, e.model, e.prompt_version, e.effort_level,
                   e.description_id
            FROM index_entries e
            JOIN memes m ON m.hash = e.hash
            WHERE m.perceptual_duplicate_of IS NULL
              AND EXISTS (SELECT 1 FROM meme_files f WHERE f.hash = m.hash)
              AND EXISTS (SELECT 1 FROM embeddings v
                          WHERE v.entry_id = e.id AND v.embed_model = :embed_model)
              -- an own entry whose description was replaced is stale
              AND (e.source_hash != e.hash OR EXISTS (
                    SELECT 1 FROM descriptions d WHERE d.id = e.description_id
                      AND d.hash = e.hash AND d.status = 'described'))
              AND (NOT :exact OR (e.model = :model AND e.prompt_version = :prompt
                   AND IFNULL(e.effort_level, '') = IFNULL(:effort, '')))
        ),
        selected AS (
            SELECT id, hash FROM (
                SELECT id, hash, ROW_NUMBER() OVER (
                    PARTITION BY hash ORDER BY {embed.FALLBACK_ORDER}, description_id DESC
                ) AS rn
                FROM candidates
            )
            WHERE rn = 1 OR :all_variants
        )
    """
    variant = variant or {}
    params = {
        "all_variants": not variant,
        "exact": exact and bool(variant),
        "model": variant.get("model"),
        "prompt": variant.get("prompt_version"),
        "effort": variant.get("effort_level"),
    }
    return sql, params


def vec_hits(
    con: sqlite3.Connection,
    vector: list[float],
    embed_model: str,
    variant: dict | None,
    exact: bool,
) -> list[str]:
    cte, params = selected_entries(variant, exact)
    rows = con.execute(
        cte
        + """
        SELECT s.hash, MIN(vec_distance_l2(v.embedding, :vector)) AS distance
        FROM selected s
        JOIN embeddings v ON v.entry_id = s.id
        WHERE v.embed_model = :embed_model AND v.dims = :dims
        GROUP BY s.hash
        ORDER BY distance, s.hash
        LIMIT :k
        """,
        {
            **params,
            "embed_model": embed_model,
            "vector": sqlite_vec.serialize_float32(vector),
            "dims": len(vector),
            "k": CANDIDATES,
        },
    ).fetchall()
    return [h for h, _ in rows]


def fts_hits(
    con: sqlite3.Connection,
    query: str,
    embed_model: str,
    variant: dict | None,
    exact: bool,
) -> list[str]:
    match = fts_query(query)
    if not match:
        return []
    cte, params = selected_entries(variant, exact)
    rows = con.execute(
        cte
        + """
        SELECT s.hash, MIN(f.rank) AS best
        FROM entries_fts f
        JOIN selected s ON s.id = f.rowid
        WHERE entries_fts MATCH :match
        GROUP BY s.hash
        ORDER BY best, s.hash
        LIMIT :k
        """,
        {**params, "embed_model": embed_model, "match": match, "k": CANDIDATES},
    ).fetchall()
    return [h for h, _ in rows]


def search_with_hashes(
    con: sqlite3.Connection,
    embed_cfg: dict,
    query: str,
    top_n: int,
    *,
    embedding_weight: float = 0.5,
    variant: dict | None = None,
    exact: bool = False,
) -> list[tuple[str, str]]:
    """Return ranked pairs; 0 means FTS only, 1 means embeddings only.

    `embed_cfg["model"]` selects which embedding model's vectors are searched;
    its optional `query_prefix` is prepended to the embedded query only.
    `variant` ({model, prompt_version, effort_level}, e.g. `cfg["describe"]`)
    selects one description per meme; None searches every indexed description.
    """
    if not 0 <= embedding_weight <= 1:
        raise ValueError("embedding_weight must be between 0 and 1")
    model = embed_cfg["model"]
    rankings = []
    if embedding_weight > 0:
        prefix = embed_cfg.get("query_prefix", "")
        vector = common.request_embedding(embed_cfg, prefix + query)
        rankings.append(
            (embedding_weight, vec_hits(con, vector, model, variant, exact))
        )
    if embedding_weight < 1:
        rankings.append(
            (1 - embedding_weight, fts_hits(con, query, model, variant, exact))
        )
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
    variant: dict | None = None,
    exact: bool = False,
) -> list[str]:
    """Return ranked paths; the stable public API used by the CLI."""
    return [
        path
        for _, path in search_with_hashes(
            con,
            embed_cfg,
            query,
            top_n,
            embedding_weight=embedding_weight,
            variant=variant,
            exact=exact,
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
    parser.add_argument(
        "--embed-model",
        metavar="NAME",
        help="search this configured embedding model's vectors (default: [embed])",
    )
    parser.add_argument(
        "--desc-model", help="description variant: model (default: [describe])"
    )
    parser.add_argument(
        "--prompt", help="description variant: prompt_version (default: [describe])"
    )
    parser.add_argument(
        "--effort",
        help='description variant: effort_level, "" for none (default: [describe])',
    )
    variants = parser.add_mutually_exclusive_group()
    variants.add_argument(
        "--exact",
        action="store_true",
        help="only memes indexed from exactly this variant (no model/effort fallback)",
    )
    variants.add_argument(
        "--all-variants",
        action="store_true",
        help="search every indexed description; a meme scores by its best one",
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    variant = None
    if not args.all_variants:
        variant = {
            "model": args.desc_model or cfg["describe"]["model"],
            "prompt_version": args.prompt or cfg["describe"]["prompt_version"],
            "effort_level": cfg["describe"].get("effort_level")
            if args.effort is None
            else args.effort or None,
        }
    con = common.connect(cfg["db_path"])
    paths = search(
        con,
        common.embed_config(cfg, args.embed_model),
        " ".join(args.query),
        args.top,
        variant=variant,
        exact=args.exact,
    )
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
