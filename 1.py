"""Stage 1: scan images into the DB and (re)group perceptual near-duplicates."""

import argparse
import hashlib
import sqlite3
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

import imagehash
import sqlite_vec
from PIL import Image

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
STATUS_RANK = {
    "scanned": 0,
    "failed_describe": 1,
    "described": 2,
    "failed_embed": 3,
    "embedded": 4,
}


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(Path(__file__).with_name("schema.sql").read_text())
    con.execute("PRAGMA foreign_keys = ON")
    return con


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
            "INSERT INTO memes (hash, path, phash, status) VALUES (?, ?, ?, 'scanned')",
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


def regroup(con: sqlite3.Connection, threshold: int) -> None:
    rows = con.execute("SELECT hash, phash, status FROM memes").fetchall()
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
        # Representative = row with the furthest pipeline status (embedded >
        # described > scanned), so describe/embed work already done is never
        # thrown away; ties broken by hash (deterministic).
        rep = min(members, key=lambda i: (-STATUS_RANK[rows[i][2]], rows[i][0]))
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1: scan & dedup")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--regroup-only",
        action="store_true",
        help="skip ingest, only recompute perceptual duplicate groups",
    )
    args = parser.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)

    con = connect(cfg["db_path"])
    if not args.regroup_only:
        ingest(con, Path(cfg["image_dir"]).expanduser())
    regroup(con, cfg["phash_threshold"])
    con.close()


if __name__ == "__main__":
    main()
