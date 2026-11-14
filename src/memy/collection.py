"""GUI-independent collection logic: physical files, filters and deletion."""

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from memy import scan as scan_stage
from memy.i18n import _


def current_result_paths(hits: list[tuple[str, str]]) -> tuple[list[str], int]:
    """Keep only hits whose file still has the indexed content hash.

    A file may be replaced in place between scans. In that case an older DB row
    can still point at its path, but displaying it would show a different meme
    than the one whose description matched the query. Hide that stale row in
    the GUI without modifying the database.
    """
    paths: list[str] = []
    hidden = 0
    for expected_hash, path in hits:
        try:
            with open(path, "rb") as file:
                actual_hash = hashlib.file_digest(file, "sha256").hexdigest()
        except OSError:
            hidden += 1
            continue
        if actual_hash != expected_hash:
            hidden += 1
            continue
        paths.append(path)
    return paths, hidden


def indexed_files_sql(con: sqlite3.Connection) -> str:
    """Use tracked paths only; old databases fall back until their first scan."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'meme_files'").fetchone():
        return "SELECT hash, path FROM meme_files"
    return "SELECT hash, path FROM memes"


def related_files(
    con: sqlite3.Connection, digest: str, *, identical: bool
) -> list[tuple[str, str, bool, bool]]:
    """Return physical paths with representative and primary-copy markers."""
    condition = (
        "m.hash = selected.hash"
        if identical
        else "COALESCE(m.perceptual_duplicate_of, m.hash) = "
        "COALESCE(selected.perceptual_duplicate_of, selected.hash)"
    )
    return con.execute(
        f"""
        WITH files AS ({indexed_files_sql(con)})
        SELECT m.hash, f.path, m.perceptual_duplicate_of IS NULL, f.path = m.path
        FROM memes selected
        JOIN memes m ON {condition}
        JOIN files f ON f.hash = m.hash
        WHERE selected.hash = ?
        ORDER BY m.perceptual_duplicate_of IS NOT NULL, m.hash, f.path
        """,
        (digest,),
    ).fetchall()


@dataclass
class CollectionFile:
    digest: str
    path: str
    group: str
    primary: bool
    status: str | None
    indexed: bool
    copies: int = 1


def collection_files(
    con: sqlite3.Connection, cfg: dict
) -> tuple[list[CollectionFile], int]:
    """Read all physical files, including unindexed and non-representative memes."""
    rows = con.execute(
        f"""
        WITH files AS ({indexed_files_sql(con)})
        SELECT m.hash, f.path, COALESCE(m.perceptual_duplicate_of, m.hash),
               f.path = m.path, d.status,
               EXISTS(SELECT 1 FROM index_entries e
                      JOIN embeddings v ON v.entry_id = e.id WHERE e.hash = m.hash)
        FROM memes m JOIN files f ON f.hash = m.hash
        LEFT JOIN descriptions d ON d.hash = m.hash AND d.model = ?
            AND d.prompt_version = ? AND d.effort_level IS ?
        ORDER BY f.path
        """,
        (cfg["model"], cfg["prompt_version"], cfg.get("effort_level")),
    ).fetchall()
    available, hidden = current_result_paths([(r[0], r[1]) for r in rows])
    available = set(available)
    files = [CollectionFile(*r) for r in rows if r[1] in available]
    counts = Counter(f.digest for f in files)
    for file in files:
        file.copies = counts[file.digest]
    return files, hidden


def filter_collection(
    files: list[CollectionFile], query: str, kind: str
) -> list[CollectionFile]:
    groups = Counter(f.group for f in files)
    query = query.strip().casefold()
    return [
        f
        for f in files
        if query in f.path.casefold()
        and (
            kind == "all"
            or kind == "duplicates"
            and groups[f.group] > 1
            or kind == "identical"
            and f.copies > 1
            or kind == "undescribed"
            and f.status is None
            or kind == "failed"
            and f.status == "failed_describe"
        )
    ]


def has_index(con, digest) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM index_entries e JOIN embeddings v ON v.entry_id = e.id "
            "WHERE e.hash = ?",
            (digest,),
        ).fetchone()
    )


def preserve_group_index(con, digest, group):
    """Keep a group's search index when its indexed member loses its last copy."""
    if not has_index(con, digest):
        return
    marks = ",".join("?" * len(group))
    rows = con.execute(
        f"SELECT hash, path FROM ({indexed_files_sql(con)}) WHERE hash IN ({marks})",
        group,
    ).fetchall()
    candidates = sorted(
        {h for h, p in rows if h != digest and scan_stage.file_matches(p, h, {})}
    )
    if not candidates:
        return
    # Prefer an existing indexed survivor; otherwise hand this group's entries
    # (vectors and FTS, with their provenance) to an available member. Do not
    # attribute the deleted image's description to another image or overwrite
    # a survivor's own index.
    if any(has_index(con, target) for target in candidates):
        return
    con.execute(
        "UPDATE index_entries SET hash = ? WHERE hash = ?", (candidates[0], digest)
    )


def delete_files(con, files, threshold):
    """Delete only confirmed paths whose SHA-256 still matches the database."""
    removed, errors = [], []
    # Upgrade legacy copy tracking without running scan or inference.
    had_meme_files = con.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'meme_files'"
    ).fetchone()
    con.execute(
        "CREATE TABLE IF NOT EXISTS meme_files (path TEXT PRIMARY KEY, "
        "hash TEXT NOT NULL REFERENCES memes(hash) ON DELETE CASCADE)"
    )
    if not had_meme_files:
        con.execute("INSERT OR IGNORE INTO meme_files SELECT path, hash FROM memes")
    con.commit()
    # Snapshot memberships: deleting a representative clears FK references,
    # but later deletions in this batch must still see the original group.
    groups = {}
    for digest, group in con.execute(
        "SELECT hash, COALESCE(perceptual_duplicate_of, hash) FROM memes"
    ):
        groups.setdefault(group, []).append(digest)
    memberships = {h: members for members in groups.values() for h in members}
    for digest, path in files:
        try:
            recorded = con.execute(
                "SELECT hash FROM meme_files WHERE path = ?", (path,)
            ).fetchone()
            if recorded != (digest,) or not scan_stage.file_matches(path, digest, {}):
                raise OSError(_("the file is gone or its content changed (SHA-256)"))
            Path(path).unlink()
        except OSError as error:
            errors.append((path, str(error)))
            continue
        with con:
            con.execute("DELETE FROM meme_files WHERE path = ?", (path,))
            copies = con.execute(
                "SELECT path FROM meme_files WHERE hash = ? ORDER BY path", (digest,)
            ).fetchall()
            survivors = [p for (p,) in copies if scan_stage.file_matches(p, digest, {})]
            if survivors:
                primary = con.execute(
                    "SELECT path FROM memes WHERE hash = ?", (digest,)
                ).fetchone()[0]
                if primary not in survivors:
                    con.execute(
                        "UPDATE memes SET path = ? WHERE hash = ?",
                        (survivors[0], digest),
                    )
            else:
                preserve_group_index(con, digest, memberships[digest])
                con.execute(
                    "UPDATE memes SET perceptual_duplicate_of = NULL "
                    "WHERE perceptual_duplicate_of = ?",
                    (digest,),
                )
                for table in ("index_entries", "descriptions", "memes"):
                    con.execute(f"DELETE FROM {table} WHERE hash = ?", (digest,))
        removed.append(path)
    if removed:
        scan_stage.regroup(con, threshold)
    return removed, errors
