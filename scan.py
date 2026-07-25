"""Stage 1: scan images into the DB and (re)group perceptual near-duplicates."""

import argparse
import hashlib
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import imagehash
from PIL import Image

import common

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Rep-selection prefers the row with the most invested pipeline work: embedded
# beats any-successful-description beats nothing. Failed attempts don't count as
# "work worth preserving".
RANK_EMBEDDED = 2
RANK_DESCRIBED = 1
RANK_NONE = 0


def ingest(con: sqlite3.Connection, image_dir: Path) -> None:
    known = {h for (h,) in con.execute("SELECT hash FROM memes")}
    seen = new = errors = 0
    for file in sorted(image_dir.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in EXTENSIONS:
            continue
        seen += 1
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if digest in known:
            con.execute("UPDATE memes SET path = ? WHERE hash = ?", (str(file), digest))
            continue
        try:
            with Image.open(file) as img:
                phash = str(imagehash.phash(img))
        except Exception as e:
            errors += 1
            print(f"skipping {file}: {e}", file=sys.stderr)
            continue
        con.execute(
            "INSERT INTO memes (hash, path, phash) VALUES (?, ?, ?)",
            (digest, str(file), phash),
        )
        known.add(digest)
        new += 1
        if new % 69 == 0:  # Nice
            print(f"scanned {new} new / {seen} seen")
    con.commit()
    print(
        f"ingest: {seen} files seen, {new} new, {seen - new - errors} already known, {errors} unreadable"
    )


def prune_missing(con: sqlite3.Connection) -> None:
    rows = con.execute("SELECT hash, path FROM memes").fetchall()
    missing = [h for h, p in rows if not Path(p).exists()]
    if not missing:
        print("prune: no missing files")
        return
    marks = ",".join("?" * len(missing))
    # Null out FK references from other rows so DELETE doesn't violate the FK.
    con.execute(
        f"UPDATE memes SET perceptual_duplicate_of = NULL "
        f"WHERE perceptual_duplicate_of IN ({marks})",
        missing,
    )
    con.execute(f"DELETE FROM descriptions WHERE hash IN ({marks})", missing)
    con.execute(f"DELETE FROM memes_fts WHERE hash IN ({marks})", missing)
    con.execute(f"DELETE FROM memes_vec WHERE hash IN ({marks})", missing)
    con.execute(f"DELETE FROM memes WHERE hash IN ({marks})", missing)
    con.commit()
    print(f"prune: removed {len(missing)} rows with missing files")


def regroup(con: sqlite3.Connection, threshold: int) -> None:
    rows = con.execute(
        f"""
        SELECT m.hash, m.phash,
          CASE
            WHEN EXISTS (SELECT 1 FROM memes_vec WHERE hash = m.hash) THEN {RANK_EMBEDDED}
            WHEN EXISTS (SELECT 1 FROM descriptions d WHERE d.hash = m.hash AND d.status = 'described') THEN {RANK_DESCRIBED}
            ELSE {RANK_NONE}
          END AS rank
        FROM memes m
        """
    ).fetchall()
    phashes = [int(phash, 16) for _, phash, _ in rows]

    parent = list(range(len(rows)))

    def find_group(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # O(n²), but fuck it
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if (phashes[i] ^ phashes[j]).bit_count() <= threshold:
                parent[find_group(i)] = find_group(j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find_group(i)].append(i)

    updates = []
    duplicates = 0
    for members in groups.values():
        # Representative = row with the furthest pipeline progress (embedded >
        # described > nothing), so describe/embed work already done is never
        # thrown away; ties broken by hash (deterministic).
        rep = min(members, key=lambda i: (-rows[i][2], rows[i][0]))
        for i in members:
            updates.append((None if i == rep else rows[rep][0], rows[i][0]))
        duplicates += len(members) - 1

    con.executemany(
        "UPDATE memes SET perceptual_duplicate_of = ? WHERE hash = ?", updates
    )
    con.commit()
    multi = sum(1 for m in groups.values() if len(m) > 1)
    print(
        f"regroup: {len(rows)} rows, {multi} duplicate groups, {duplicates} marked as duplicates"
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)
    parser.add_argument(
        "--regroup-only",
        action="store_true",
        help="skip ingest, only recompute perceptual duplicate groups",
    )
    parser.add_argument(
        "--prune-missing",
        action="store_true",
        help="delete rows whose path no longer exists on disk",
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    # Scan is the stage that creates the DB, so it's the one applying schema.sql.
    con = common.connect(cfg["db_path"], init_schema=True)
    if args.prune_missing:
        prune_missing(con)
    if not args.regroup_only:
        ingest(con, Path(cfg["image_dir"]).expanduser())
    regroup(con, cfg["phash_threshold"])
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 1: scan & dedup")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
