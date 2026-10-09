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
    # Store one canonical form regardless of whether image_dir in the TOML was
    # relative. `describe --path` resolves its argument too, so this also makes
    # targeted re-description reliable.
    image_dir = image_dir.expanduser().resolve()
    known = {h for (h,) in con.execute("SELECT hash FROM memes")}
    scanned: dict[str, str] = {}
    seen = new = errors = 0
    for file in sorted(image_dir.rglob("*")):
        if not file.is_file() or file.suffix.lower() not in EXTENSIONS:
            continue
        seen += 1
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        scanned[str(file)] = digest
        if digest in known:
            con.execute(
                "INSERT INTO meme_files(path, hash) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET hash = excluded.hash",
                (str(file), digest),
            )
            continue
        try:
            with Image.open(file) as img:
                phash = str(imagehash.phash(img))
        # Pillow fails on broken files with whatever its decoder raises (OSError,
        # ValueError, SyntaxError, DecompressionBombError...); none of it may
        # stop the walk.
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"skipping {file}: {e}", file=sys.stderr)
            continue
        con.execute(
            "INSERT INTO memes (hash, path, phash) VALUES (?, ?, ?)",
            (digest, str(file), phash),
        )
        known.add(digest)
        con.execute(
            "INSERT INTO meme_files(path, hash) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET hash = excluded.hash",
            (str(file), digest),
        )
        new += 1
        if new % 69 == 0:  # Nice
            print(f"scanned {new} new / {seen} seen")
    select_primary_paths(con, scanned)
    con.commit()
    print(
        f"ingest: {seen} files seen, {new} new, {seen - new - errors} already known, {errors} unreadable"
    )


def file_matches(path: str, digest: str, scanned: dict[str, str]) -> bool:
    if path in scanned:
        return scanned[path] == digest
    try:
        with open(path, "rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest() == digest
    except OSError:
        return False


def select_primary_paths(
    con: sqlite3.Connection, scanned: dict[str, str] | None = None
) -> None:
    """Keep the primary copy, falling back only when it no longer matches."""
    scanned = scanned if scanned is not None else {}
    for digest, primary in con.execute("SELECT hash, path FROM memes").fetchall():
        if file_matches(primary, digest, scanned):
            continue
        copies = con.execute(
            "SELECT path FROM meme_files WHERE hash = ? ORDER BY path", (digest,)
        ).fetchall()
        for (path,) in copies:
            if file_matches(path, digest, scanned):
                con.execute("UPDATE memes SET path = ? WHERE hash = ?", (path, digest))
                break


def prune_missing(con: sqlite3.Connection) -> None:
    for path, digest in con.execute("SELECT path, hash FROM meme_files").fetchall():
        if not file_matches(path, digest, {}):
            con.execute("DELETE FROM meme_files WHERE path = ?", (path,))
    select_primary_paths(con)
    rows = con.execute("SELECT hash, path FROM memes").fetchall()
    missing = [h for h, p in rows if not file_matches(p, h, {})]
    if not missing:
        con.commit()
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
        help="remove unavailable copies; delete memes only when no copy remains",
    )


def run(args: argparse.Namespace, cfg: dict) -> None:
    # Scan is the stage that creates the DB, so it's the one applying schema.sql.
    con = common.connect(cfg["db_path"], init_schema=True)
    if not args.regroup_only:
        ingest(con, Path(cfg["image_dir"]).expanduser())
    if args.prune_missing:
        prune_missing(con)
    regroup(con, cfg["phash_threshold"])
    con.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 1: scan & dedup")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
