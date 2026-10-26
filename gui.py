"""Stage 5: Dear PyGui GUI over stage 4 search."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

import dearpygui.dearpygui as dpg

import common
import embed as embed_stage
import scan as scan_stage
import search as search_stage


def variant_label(model: str, prompt_version: str, effort_level: str | None) -> str:
    suffix = f" / {effort_level}" if effort_level else ""
    return f"{model} @ {prompt_version}{suffix}"


def page_key(names: tuple[str, ...], fallback: int) -> int:
    """Prefer exported ImGui keys; ignore legacy native key codes below 512."""
    for name in names:
        value = getattr(dpg, name, None)
        if isinstance(value, int) and value >= 512:
            return value
    # DPG 2.3.1 exports GLFW codes in mvKey_Prior/Next instead of ImGui keys.
    return fallback


KEY_COLOR = (255, 215, 80, 255)
HINT_COLOR = (140, 140, 140, 255)

DEBOUNCE_S = 0.3  # idle time after the last keystroke before we search
PAGE_STEP = 10  # results skipped by PageUp/PageDown
PAGE_UP_KEY = page_key(("mvKey_PageUp", "mvKey_Prior"), 517)
PAGE_DOWN_KEY = page_key(("mvKey_PageDown", "mvKey_Next"), 518)
TOP_N_DEFAULT = 50
TOP_N_STEP = 10
TOP_N_RANGE = (10, 100)
ROW_H_FALLBACK = 21  # used until a rendered row can be measured
FOOTER_SPACE = 36  # keep the status line outside the scrolling content panes

# Modifier keys are queried as regular keys; the Mod* aliases are what ImGui
# reports on Wayland, the L/R ones are a cheap safety net on other backends.
CTRL_KEYS = (dpg.mvKey_ModCtrl, dpg.mvKey_LControl, dpg.mvKey_RControl)
SHIFT_KEYS = (dpg.mvKey_ModShift, dpg.mvKey_LShift, dpg.mvKey_RShift)
ALT_KEYS = (dpg.mvKey_ModAlt, dpg.mvKey_LAlt, dpg.mvKey_RAlt)

# Wayland/X11 clipboard wants the real image mime type, not a file path.
CLIPBOARD_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

SHORTCUTS = [
    ("Enter", "szukaj teraz (i tak szuka samo po chwili)"),
    ("↑ / ↓", "poprzedni / następny wynik"),
    ("PgUp / PgDn", f"skok o {PAGE_STEP} wyników"),
    ("Ctrl+Home / Ctrl+End", "pierwszy / ostatni wynik"),
    ("Alt+1…9", "skok do wyniku N"),
    ("Ctrl+C", "kopiuj OBRAZEK do schowka (wklej na czacie)"),
    ("Ctrl+Shift+C", "kopiuj ścieżkę do pliku"),
    ("Ctrl+O / Shift+Enter", "otwórz w przeglądarce obrazków"),
    ("Tab / Shift+Tab", "następny / poprzedni wariant opisu"),
    ("Ctrl+D", "pokaż / ukryj opis"),
    ("F11", "podgląd na całą szerokość"),
    ("Ctrl+= / Ctrl+-", f"więcej / mniej wyników (co {TOP_N_STEP})"),
    ("Esc", "wyczyść zapytanie, potem wyjdź"),
    ("Ctrl+Q", "wyjście"),
    ("F1", "ta ściąga"),
]


def key_down(keys: tuple[int, ...]) -> bool:
    return any(dpg.is_key_down(k) for k in keys)


def description_fields(raw: str | None) -> list[tuple[str | None, str]]:
    if not raw:
        return [(None, "(brak opisu)")]
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [(None, raw)]
    if not isinstance(d, dict):
        return [(None, str(d))]
    fields: list[tuple[str | None, str]] = []
    for key, value in d.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        fields.append((str(key), str(value)))
    return fields or [(None, "(pusty opis)")]


def copy_image_to_clipboard(path: str) -> str:
    """Put the image bytes (not the path) on the clipboard; returns a status line."""
    mime = CLIPBOARD_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
    if shutil.which("wl-copy"):
        cmd = ["wl-copy", "-t", mime]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", mime]
    else:
        return "brak wl-copy / xclip — nie mam czym kopiować"
    try:
        with open(path, "rb") as fh:
            # Both tools daemonize themselves once they own the selection.
            subprocess.run(cmd, stdin=fh, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return f"kopiowanie nie wyszło: {e}"
    return f"skopiowano obrazek: {os.path.basename(path)}"


def open_externally(path: str) -> str:
    viewer = next(
        (v for v in [*search_stage.VIEWERS, "xdg-open"] if shutil.which(v)), None
    )
    if not viewer:
        return "nie znalazłem przeglądarki obrazków"
    subprocess.Popen(
        [viewer, path],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return f"otwieram w {viewer}"


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


# DPG 2.x autoloads all Unicode ranges from a bound font, so we only need to
# point it at a TTF that actually has Polish glyphs (default DPG font doesn't).
FONT_CANDIDATES = [
    "/usr/share/fonts/inter/Inter-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]


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
               EXISTS(SELECT 1 FROM memes_vec v WHERE v.hash = m.hash)
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
            kind == "Wszystkie"
            or kind == "Duplikaty"
            and groups[f.group] > 1
            or kind == "Identyczne kopie"
            and f.copies > 1
            or kind == "Nieopisane"
            and f.status is None
            or kind == "Błędy"
            and f.status == "failed_describe"
        )
    ]


def preserve_group_index(con, digest, group):
    """Keep a group's search index when its indexed member loses its last copy."""
    vector = con.execute(
        "SELECT embedding FROM memes_vec WHERE hash = ?", (digest,)
    ).fetchone()
    if vector is None:
        return
    marks = ",".join("?" for _ in group)
    rows = con.execute(
        f"SELECT hash, path FROM ({indexed_files_sql(con)}) WHERE hash IN ({marks})",
        group,
    ).fetchall()
    candidates = sorted(
        {h for h, p in rows if h != digest and scan_stage.file_matches(p, h, {})}
    )
    if not candidates:
        return
    # Prefer an existing indexed survivor; otherwise retain this group's vector
    # on an available member. Do not attribute the deleted image's description
    # to another image or overwrite a survivor's own index.
    for target in candidates:
        if con.execute("SELECT 1 FROM memes_vec WHERE hash = ?", (target,)).fetchone():
            return
    target = candidates[0]
    con.execute(
        "INSERT INTO memes_vec(hash, embedding) VALUES (?, ?)", (target, vector[0])
    )
    con.execute("DELETE FROM memes_fts WHERE hash = ?", (target,))
    con.execute(
        "INSERT INTO memes_fts(hash, transcription, keywords, template) "
        "SELECT ?, transcription, keywords, template FROM memes_fts WHERE hash = ?",
        (target, digest),
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
                raise OSError("plik zniknął lub zmienił zawartość (SHA-256)")
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
                for table in ("descriptions", "memes_fts", "memes_vec", "memes"):
                    con.execute(f"DELETE FROM {table} WHERE hash = ?", (digest,))
        removed.append(path)
    if removed:
        scan_stage.regroup(con, threshold)
    return removed, errors


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.con = common.connect(cfg["db_path"], check_same_thread=False)
        self.paths: list[str] = []
        self.current_idx: int = -1
        self.current_tex: int | str | None = None
        self.current_raw: str | None = None
        self.preview_path: str | None = None
        self.preview_hash: str | None = None
        self.image_dir = Path(cfg["image_dir"]).expanduser().resolve()
        self.img_size: tuple[int, int] = (0, 0)
        self.variants: dict[str, str | None] = {}
        self.top_n = TOP_N_DEFAULT
        self.row_h = ROW_H_FALLBACK
        self.pending_copy: tuple[str, str] | None = None  # (kind, path)
        self.search_at: float | None = None  # debounce deadline, None = idle
        self.last_query = ""
        self.embedding_weight = 0.5
        self.last_embedding_weight = 0.5
        self.pane_w = 0
        self.show_desc = True
        self.fullscreen = False
        self.mode = "Szukaj"
        self.mode_state = {}
        self.checked = {"Szukaj": set(), "Lista": set()}
        self.collection = None
        self.collection_hidden = 0
        self.collection_filter = "Wszystkie"
        self.list_selected = None
        self.related_origin = None
        self.related_scroll = 0
        self.related_identical = False
        self.collection_widgets = {}
        self.restore_scroll = None
        self.pending_delete = []
        self.embedding_running = False
        self.embedding_events = SimpleQueue()
        self.embedding_counts = (0, 0, 0)

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        dpg.create_context()
        # Process callbacks on the render thread.
        dpg.configure_app(manual_callback_management=True)

        with dpg.font_registry():
            font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
            if font_path:
                f = dpg.add_font(font_path, 16)
                dpg.bind_font(f)

        dpg.add_texture_registry(tag="tex_reg")
        self.bind_keys()

        with dpg.window(
            tag="main",
            no_title_bar=True,
            no_resize=True,
            no_move=True,
            no_scrollbar=True,
            no_scroll_with_mouse=True,
        ):
            with dpg.table(header_row=False, width=-1):
                dpg.add_table_column()
                dpg.add_table_column(
                    tag="search_balance_column",
                    width_fixed=True,
                    init_width_or_weight=155,
                )
                dpg.add_table_column(width_fixed=True, init_width_or_weight=160)
                with dpg.table_row():
                    dpg.add_input_text(
                        tag="query",
                        hint="opisz mem…",
                        width=-1,
                        callback=self.on_query_change,
                    )
                    with dpg.group(tag="search_balance", horizontal=True):
                        dpg.add_slider_int(
                            tag="embedding_weight",
                            default_value=50,
                            min_value=0,
                            max_value=100,
                            width=140,
                            format="%d%% emb.",
                            clamped=True,
                            callback=self.on_balance_change,
                        )
                    with dpg.tooltip("embedding_weight"):
                        dpg.add_text(
                            "W lewo: dopasowanie tekstu (FTS).\n"
                            "W prawo: podobieństwo znaczenia (embeddingi).\n"
                            "50%: oba równie ważne."
                        )
                    dpg.add_radio_button(
                        ("Szukaj", "Lista"),
                        tag="view_mode",
                        default_value=self.mode,
                        horizontal=True,
                        callback=self.on_mode_change,
                    )

            with dpg.group(tag="search_content", horizontal=True):
                with dpg.child_window(
                    tag="list_pane", width=380, height=-FOOTER_SPACE, border=False
                ):
                    pass
                with dpg.child_window(
                    tag="list_content",
                    width=570,
                    height=-FOOTER_SPACE,
                    border=False,
                    show=False,
                ):
                    dpg.add_combo(
                        [
                            "Wszystkie",
                            "Duplikaty",
                            "Identyczne kopie",
                            "Nieopisane",
                            "Błędy",
                        ],
                        default_value="Wszystkie",
                        width=-1,
                        callback=self.on_filter_change,
                    )
                    dpg.add_button(
                        label="Embeduj opisane",
                        tag="embed_described",
                        callback=self.start_embedding,
                    )
                    with dpg.tooltip("embed_described"):
                        dpg.add_text(
                            "Uzupełnia brakujące embeddingi w całej kolekcji,\n"
                            "niezależnie od promptu, modelu, filtrów i zaznaczenia.\n"
                            "Dla każdego reprezentanta wybiera najnowszy udany opis."
                        )
                    dpg.add_text("", tag="embed_progress", wrap=530)
                    dpg.add_group(tag="collection_rows")
                    dpg.add_button(
                        label="Usuń zaznaczone pliki",
                        callback=lambda: self.request_delete(self.checked[self.mode]),
                    )
                with dpg.child_window(
                    tag="related",
                    width=570,
                    height=-FOOTER_SPACE,
                    border=False,
                    show=False,
                ):
                    pass
                with dpg.child_window(
                    tag="preview_pane",
                    autosize_x=True,
                    height=-FOOTER_SPACE,
                    border=False,
                ):
                    dpg.add_text("", tag="preview_path", show=False, wrap=700)
                    with dpg.group(tag="preview_actions", horizontal=True, show=False):
                        dpg.add_button(
                            label="Grupa duplikatów",
                            callback=lambda: self.open_related(identical=False),
                        )
                        dpg.add_button(
                            label="Identyczne kopie",
                            callback=lambda: self.open_related(identical=True),
                        )
                        dpg.add_button(
                            label="Wróć do wyniku",
                            callback=self.close_related,
                        )
                    with dpg.group(tag="file_actions", horizontal=True, show=False):
                        dpg.add_button(
                            label="Kopiuj obraz",
                            callback=lambda: self.queue_copy("image"),
                        )
                        dpg.add_button(
                            label="Kopiuj ścieżkę",
                            callback=lambda: self.queue_copy("path"),
                        )
                        dpg.add_button(label="Otwórz", callback=self.open_current)
                    dpg.add_combo(
                        tag="desc_variant",
                        items=[],
                        callback=self.on_variant_change,
                        show=False,
                        width=-1,
                    )
                    dpg.add_group(tag="preview_desc", show=False)

            with dpg.table(tag="footer", header_row=False, width=-1):
                dpg.add_table_column()
                dpg.add_table_column(width_fixed=True, init_width_or_weight=110)
                with dpg.table_row():
                    dpg.add_text("", tag="status")
                    dpg.add_text("— F1: skróty", tag="status_hint", color=HINT_COLOR)

        dpg.add_window(
            label="Skróty klawiszowe",
            tag="help",
            show=False,
            width=560,
            height=460,
            pos=(340, 120),
        )
        with dpg.window(
            label="Potwierdź usunięcie",
            tag="delete_confirm",
            modal=True,
            show=False,
            width=700,
            height=420,
        ):
            dpg.add_text("Pliki zostaną trwale usunięte z dysku.")
            dpg.add_text("", tag="delete_keep", wrap=660)
            with dpg.child_window(height=250):
                dpg.add_text("", tag="delete_paths", wrap=660)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Usuń", callback=self.confirm_delete)
                dpg.add_button(
                    label="Anuluj", callback=lambda: dpg.hide_item("delete_confirm")
                )
        with dpg.window(
            label="Wynik usuwania",
            tag="delete_report",
            modal=True,
            show=False,
            width=700,
            height=420,
        ):
            with dpg.child_window(height=320):
                dpg.add_text("", tag="delete_errors", wrap=660)
            dpg.add_button(
                label="Zamknij", callback=lambda: dpg.hide_item("delete_report")
            )
        with dpg.table(parent="help", header_row=False, borders_innerH=False):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=180)
            dpg.add_table_column()
            for keys, what in SHORTCUTS:
                with dpg.table_row():
                    dpg.add_text(keys, color=KEY_COLOR)
                    dpg.add_text(what)

        dpg.create_viewport(title="mem search", width=1280, height=800)
        dpg.setup_dearpygui()
        dpg.set_primary_window("main", True)
        dpg.show_viewport()
        dpg.focus_item("query")

    def bind_keys(self) -> None:
        """All shortcuts are global handlers: the query box normally owns the
        keyboard, so anything bound to a bare letter would be swallowed by
        typing. Letters therefore always need a modifier;
        arrows, PgUp/PgDn, Esc and F-keys are free because a single-line input
        does nothing with them. Plain Home/End are deliberately left to the text
        cursor — Ctrl+Home/End do the list jumps instead.
        """
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=lambda: self.step(1))
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=lambda: self.step(-1))
            dpg.add_key_press_handler(
                PAGE_DOWN_KEY, callback=lambda: self.step(PAGE_STEP)
            )
            dpg.add_key_press_handler(
                PAGE_UP_KEY, callback=lambda: self.step(-PAGE_STEP)
            )
            dpg.add_key_press_handler(dpg.mvKey_Home, callback=self.on_home)
            dpg.add_key_press_handler(dpg.mvKey_End, callback=self.on_end)
            dpg.add_key_press_handler(dpg.mvKey_Return, callback=self.on_return)
            dpg.add_key_press_handler(dpg.mvKey_NumPadEnter, callback=self.on_return)
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self.on_escape)
            dpg.add_key_press_handler(dpg.mvKey_Tab, callback=self.on_tab)
            dpg.add_key_press_handler(dpg.mvKey_C, callback=self.on_copy)
            dpg.add_key_press_handler(dpg.mvKey_O, callback=self.on_open)
            dpg.add_key_press_handler(dpg.mvKey_D, callback=self.on_toggle_desc)
            dpg.add_key_press_handler(dpg.mvKey_Q, callback=self.on_quit)
            dpg.add_key_press_handler(dpg.mvKey_F11, callback=self.on_toggle_full)
            dpg.add_key_press_handler(dpg.mvKey_F1, callback=self.on_toggle_help)
            dpg.add_key_press_handler(
                dpg.mvKey_Plus, callback=lambda: self.bump_top_n(TOP_N_STEP)
            )
            dpg.add_key_press_handler(
                dpg.mvKey_Minus, callback=lambda: self.bump_top_n(-TOP_N_STEP)
            )
            for digit in range(1, 10):
                key = getattr(dpg, f"mvKey_{digit}")
                dpg.add_key_press_handler(
                    key, callback=lambda s, a, d=digit: self.on_digit(d)
                )

    # --------------------------------------------------------------- search

    def on_query_change(self, *_: object) -> None:
        # Search-as-you-type: arm the debounce, the run loop fires it.
        self.search_at = time.monotonic() + DEBOUNCE_S

    def on_balance_change(self, _sender: object, value: int) -> None:
        self.embedding_weight = value / 100
        if self.mode == "Szukaj":
            self.on_query_change()

    def maybe_search(self) -> None:
        if self.search_at is None or time.monotonic() < self.search_at:
            return
        self.search_at = None
        self.do_search()

    def do_search(self) -> None:
        if self.related_origin is not None:
            self.close_related()
        if self.mode == "Lista":
            self.render_collection()
            return
        query = dpg.get_value("query").strip()
        if not query:
            self.last_query = ""
            self.paths = []
            self.current_idx = -1
            dpg.delete_item("list_pane", children_only=True)
            self.clear_preview()
            dpg.set_value("status", "")
            return
        try:
            hits = search_stage.search_with_hashes(
                self.con,
                self.cfg["embed"],
                query,
                self.top_n,
                embedding_weight=self.embedding_weight,
            )
            paths, hidden = current_result_paths(hits)
        # Endpoint down or timing out (OSError), a malformed or wrong-sized
        # embedding response (ValueError, KeyError), or a DB hiccup.
        except (OSError, ValueError, KeyError, sqlite3.Error) as e:
            dpg.set_value("status", f"błąd: {e}")
            return

        self.last_query = query
        self.last_embedding_weight = self.embedding_weight
        self.paths = paths
        dpg.delete_item("list_pane", children_only=True)
        for i, path in enumerate(paths):
            dpg.add_selectable(
                label=f"{i + 1:>3}. {os.path.basename(path)}",
                tag=f"row_{i}",
                parent="list_pane",
                callback=self.on_select,
                user_data=i,
            )
        if paths:
            self.current_idx = -1
            self.select_index(0)
            suffix = f", ukryto {hidden} nieaktualnych" if hidden else ""
            dpg.set_value("status", f"{len(paths)} wyników (z {self.top_n}{suffix})")
        else:
            self.current_idx = -1
            dpg.set_value("status", "brak wyników")
            self.clear_preview()

    def bump_top_n(self, delta: int) -> None:
        if self.mode != "Szukaj":
            return
        if not key_down(CTRL_KEYS):
            return
        low, high = TOP_N_RANGE
        new = min(high, max(low, self.top_n + delta))
        if new == self.top_n:
            return
        self.top_n = new
        if self.last_query:
            self.do_search()
        else:
            dpg.set_value("status", f"limit wyników: {self.top_n}")

    # ----------------------------------------------------------- navigation

    def on_select(self, _sender: object, _app_data: object, user_data: int) -> None:
        self.select_index(user_data)
        dpg.focus_item("query")

    def select_index(self, idx: int) -> None:
        if self.mode != "Szukaj" or self.related_origin is not None:
            return
        if not (0 <= idx < len(self.paths)):
            return
        if idx == self.current_idx and self.preview_path == self.paths[idx]:
            # Selectables toggle on click; keep the currently previewed row selected.
            dpg.set_value(f"row_{idx}", True)
            return
        if dpg.does_item_exist(f"row_{self.current_idx}"):
            dpg.set_value(f"row_{self.current_idx}", False)
        self.current_idx = idx
        dpg.set_value(f"row_{idx}", True)
        self.scroll_into_view(idx)
        self.show(self.paths[idx])

    def scroll_into_view(self, idx: int) -> None:
        """Keyboard nav must drag the viewport along; DPG won't do it for us."""
        measured = dpg.get_item_rect_size(f"row_{idx}")[1]
        if measured:
            self.row_h = measured
        pane_h = dpg.get_item_rect_size("list_pane")[1]
        if not pane_h:
            return
        top, bottom = idx * self.row_h, (idx + 1) * self.row_h
        y = dpg.get_y_scroll("list_pane")
        if top < y:
            dpg.set_y_scroll("list_pane", top)
        elif bottom > y + pane_h:
            dpg.set_y_scroll("list_pane", bottom - pane_h)

    def step(self, delta: int) -> None:
        if not self.paths:
            return
        self.select_index(min(len(self.paths) - 1, max(0, self.current_idx + delta)))

    def on_home(self, *_: object) -> None:
        if key_down(CTRL_KEYS):
            self.select_index(0)

    def on_end(self, *_: object) -> None:
        if key_down(CTRL_KEYS):
            self.select_index(len(self.paths) - 1)

    def on_digit(self, digit: int) -> None:
        if key_down(ALT_KEYS):
            self.select_index(digit - 1)

    def on_return(self, *_: object) -> None:
        if key_down(SHIFT_KEYS):
            self.on_open()
            return
        self.search_at = None
        self.do_search()

    def on_escape(self, *_: object) -> None:
        if dpg.is_item_shown("delete_confirm"):
            dpg.hide_item("delete_confirm")
        elif dpg.is_item_shown("delete_report"):
            dpg.hide_item("delete_report")
        elif dpg.is_item_shown("related"):
            self.close_related()
        elif dpg.is_item_shown("help"):
            dpg.hide_item("help")
        elif dpg.get_value("query"):
            dpg.set_value("query", "")
            self.on_query_change()
        else:
            dpg.stop_dearpygui()

    def on_quit(self, *_: object) -> None:
        if key_down(CTRL_KEYS):
            dpg.stop_dearpygui()

    # ------------------------------------------------------------- actions

    def current_path(self) -> str | None:
        if self.preview_path:
            return self.preview_path
        dpg.set_value("status", "nie ma czego skopiować")
        return None

    def relative_path(self, path: str) -> str:
        return os.path.relpath(path, self.image_dir)

    def request_delete(self, paths, *, keep=None):
        if self.embedding_running:
            dpg.set_value("status", "Usuwanie będzie dostępne po embedowaniu.")
            return
        paths = set(paths)
        rows = self.con.execute(indexed_files_sql(self.con)).fetchall()
        self.pending_delete = [(h, p) for h, p in rows if p in paths and p != keep]
        self.pending_delete = sorted(set(self.pending_delete), key=lambda row: row[1])
        if not self.pending_delete:
            dpg.set_value("status", "Nie zaznaczono plików do usunięcia.")
            return
        dpg.set_value(
            "delete_keep", f"Zachowaj: {self.relative_path(keep)}" if keep else ""
        )
        dpg.set_value(
            "delete_paths",
            "\n".join(self.relative_path(p) for _, p in self.pending_delete),
        )
        dpg.show_item("delete_confirm")

    def delete_rest(self, digest, keep):
        rows = related_files(self.con, digest, identical=False)
        paths, _ = current_result_paths([(h, p) for h, p, _, _ in rows])
        self.request_delete(paths, keep=keep)

    def file_menu(self, widget, digest, path):
        with dpg.popup(widget, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(
                label="Usuń plik", callback=lambda: self.request_delete([path])
            )
            dpg.add_menu_item(
                label="Usuń resztę z grupy",
                callback=lambda: self.delete_rest(digest, path),
            )

    def confirm_delete(self, *_):
        if self.embedding_running:
            dpg.set_value("status", "Usuwanie będzie dostępne po embedowaniu.")
            return
        dpg.hide_item("delete_confirm")
        preview, origin = self.preview_path, self.related_origin
        removed, errors = delete_files(
            self.con, self.pending_delete, self.cfg["phash_threshold"]
        )
        self.pending_delete = []
        self.collection, self.collection_hidden = collection_files(
            self.con, self.cfg["describe"]
        )
        available = {f.path for f in self.collection}
        for checked in self.checked.values():
            checked.intersection_update(available)
        for mode, state in list(self.mode_state.items()):
            state = list(state)
            if state[1] not in available:
                state[1] = None
            if state[5] not in available:
                state[5] = None
            self.mode_state[mode] = tuple(state)
        self.related_origin = None
        self.clear_preview()
        if self.mode == "Szukaj":
            self.do_search()
        else:
            self.last_query = ""
            self.render_collection()
        if preview in available:
            self.show(preview)
            if origin in available:
                self.open_related(identical=self.related_identical)
                self.related_origin = origin
        self.apply_layout()
        message = f"Usunięto {len(removed)} plików; błędy: {len(errors)}."
        dpg.set_value("status", message)
        if errors:
            dpg.set_value(
                "delete_errors",
                message
                + "\n\n"
                + "\n\n".join(
                    f"{self.relative_path(p)}: {error}" for p, error in errors
                ),
            )
            dpg.show_item("delete_report")

    def open_related(self, *, identical: bool) -> None:
        if self.preview_hash is None:
            return
        self.related_identical = identical
        rows = related_files(self.con, self.preview_hash, identical=identical)
        paths, hidden = current_result_paths([(h, p) for h, p, _, _ in rows])
        available = set(paths)
        if self.related_origin is None:
            self.related_origin = self.preview_path
            pane = "list_pane" if self.mode == "Szukaj" else "list_content"
            self.related_scroll = dpg.get_y_scroll(pane)
        dpg.delete_item("related", children_only=True)
        dpg.add_button(label="Wróć", parent="related", callback=self.close_related)
        if self.mode == "Lista":
            dpg.add_button(
                label="Usuń zaznaczone pliki",
                parent="related",
                callback=lambda: self.request_delete(self.checked[self.mode]),
            )
        title = "Identyczne kopie" if identical else "Grupa duplikatów"
        dpg.add_text(title, parent="related")
        dpg.add_text(self.relative_path(self.preview_path), parent="related", wrap=680)
        dpg.add_text(
            f"{len(paths)} plików; kliknij ścieżkę, aby podejrzeć obraz.",
            parent="related",
        )
        if len(paths) <= 1:
            dpg.add_text(
                "Brak innych identycznych kopii."
                if identical
                else "Brak innych duplikatów.",
                parent="related",
            )
        if hidden:
            dpg.add_text(f"Ukryto {hidden} nieaktualnych ścieżek.", parent="related")
        for digest, path, representative, primary in rows:
            if path not in available:
                continue
            markers = []
            if representative and not identical:
                markers.append("reprezentant")
            if primary:
                markers.append("kopia główna")
            suffix = f" ({', '.join(markers)})" if markers else ""
            if self.mode == "Lista":
                dpg.add_checkbox(
                    parent="related",
                    default_value=path in self.checked[self.mode],
                    callback=self.on_check,
                    user_data=path,
                )
            widget = dpg.add_selectable(
                label=self.relative_path(path) + suffix,
                default_value=path == self.preview_path,
                parent="related",
                callback=self.on_related_select,
                user_data=path,
            )
            if self.mode == "Lista":
                self.file_menu(widget, digest, path)
        dpg.show_item("related")
        self.apply_layout()

    def on_related_select(
        self, _sender: object, _app_data: object, user_data: str
    ) -> None:
        self.show(user_data)
        dpg.focus_item("query")

    def on_copy(self, *_: object) -> None:
        if not key_down(CTRL_KEYS):
            return
        path = self.current_path()
        if not path:
            return
        # Deferred to after the frame: the query box is focused, so ImGui also
        # treats Ctrl+C as "copy the input text" and would clobber whatever we
        # put on the clipboard mid-frame.
        self.pending_copy = ("path" if key_down(SHIFT_KEYS) else "image", path)

    def flush_copy(self) -> None:
        if self.pending_copy is None:
            return
        kind, path = self.pending_copy
        self.pending_copy = None
        if kind == "path":
            dpg.set_clipboard_text(path)
            dpg.set_value("status", f"skopiowano ścieżkę: {path}")
        else:
            dpg.set_value("status", copy_image_to_clipboard(path))

    def on_open(self, *_: object) -> None:
        # Reachable as Ctrl+O and as Shift+Enter (which routes through here).
        if not (key_down(CTRL_KEYS) or key_down(SHIFT_KEYS)):
            return
        self.open_current()

    # ---------------------------------------------------------- view toggles

    def on_mode_change(self, _sender: object, app_data: str) -> None:
        if self.mode == "Lista" and self.search_at is not None:
            self.render_collection()
        pane = "list_pane" if self.mode == "Szukaj" else "list_content"
        self.mode_state[self.mode] = (
            dpg.get_value("query"),
            self.preview_path,
            dpg.get_y_scroll(pane),
            dpg.get_y_scroll("preview_pane"),
            dpg.get_value("status"),
            self.related_origin,
            dpg.get_y_scroll("related"),
            self.related_identical,
            self.related_scroll,
        )
        self.mode = app_data
        dpg.configure_item("search_balance_column", show=self.mode == "Szukaj")
        dpg.configure_item("search_balance", show=self.mode == "Szukaj")
        self.search_at = None
        self.pending_copy = None
        self.related_origin = None
        dpg.hide_item("related")
        state = self.mode_state.get(self.mode, ("", None, 0, 0, "", None, 0, False, 0))
        # Recreate the input so ImGui's active editing buffer cannot overwrite
        # the other mode's restored text on the next frame.
        query_parent = dpg.get_item_parent("query")
        dpg.delete_item("query")
        dpg.add_input_text(
            tag="query",
            parent=query_parent,
            before="search_balance",
            width=-1,
            default_value=state[0],
            callback=self.on_query_change,
            hint="opisz mem…" if self.mode == "Szukaj" else "filtruj nazwy i ścieżki…",
        )
        if self.mode == "Lista":
            if self.collection is None:
                self.collection, self.collection_hidden = collection_files(
                    self.con, self.cfg["describe"]
                )
            self.render_collection()
        if state[1]:
            self.show(state[1])
        else:
            self.clear_preview()
        if state[5]:
            # The related widget is shared; rebuild it for the restored mode.
            self.open_related(identical=state[7])
            self.related_origin = state[5]
            self.related_scroll = state[8]
        self.apply_layout()
        pane = "list_pane" if self.mode == "Szukaj" else "list_content"
        self.restore_scroll = {
            pane: state[2],
            "preview_pane": state[3],
            "related": state[6],
        }
        if state[4] or self.mode == "Szukaj":
            dpg.set_value("status", state[4])
        if self.mode == "Szukaj" and (
            state[0].strip() != self.last_query
            or self.embedding_weight != self.last_embedding_weight
        ):
            self.on_query_change()
        dpg.focus_item("query")

    def on_filter_change(self, _sender, value):
        self.collection_filter = value
        self.render_collection()

    def start_embedding(self, *_):
        if self.embedding_running or self.mode != "Lista":
            return
        self.embedding_running = True
        self.embedding_counts = (0, 0, 0)
        dpg.configure_item("embed_described", enabled=False)
        dpg.set_value("embed_progress", "Wczytywanie kolejki embeddingów…")
        dpg.hide_item("delete_confirm")
        self.pending_delete = []
        Thread(target=self.embed_worker, daemon=True).start()

    def embed_worker(self):
        # Own connection; only the render thread may touch DPG or self.con.
        error = None
        try:
            with closing(common.connect(self.cfg["db_path"])) as con:
                embed_stage.embed_descriptions(
                    con,
                    self.cfg["describe"],
                    self.cfg["embed"],
                    all_descriptions=True,
                    progress=lambda done, failed, total: self.embedding_events.put(
                        ("progress", (done, failed, total))
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            self.embedding_events.put(("finished", error))

    def poll_embedding(self):
        while True:
            try:
                kind, data = self.embedding_events.get_nowait()
            except Empty:
                return
            if kind == "progress":
                self.embedding_counts = data
                done, failed, total = data
                dpg.set_value(
                    "embed_progress",
                    f"Embeddingi: {done + failed}/{total}; gotowe: {done}, błędy: {failed}",
                )
                continue
            self.embedding_running = False
            dpg.configure_item("embed_described", enabled=True)
            self.collection, self.collection_hidden = collection_files(
                self.con, self.cfg["describe"]
            )
            if self.mode == "Lista":
                scroll = dpg.get_y_scroll("list_content")
                self.render_collection()
                self.restore_scroll = {"list_content": scroll}
            # Refresh search too, including when restored after a mode switch.
            self.last_query = ""
            if self.mode == "Szukaj":
                self.on_query_change()
            done, failed, total = self.embedding_counts
            if data is not None:
                message = f"Błąd embedowania: {data}"
            elif total:
                message = f"Embedowanie zakończone: {done}/{total}; błędy: {failed}."
            else:
                message = "Wszystkie opisane memy mają już embeddingi."
            dpg.set_value("embed_progress", message)

    def on_check(self, _sender, value, path):
        selected = self.checked[self.mode]
        selected.add(path) if value else selected.discard(path)
        dpg.set_value("status", f"Zaznaczono {len(selected)} plików")

    def select_collection(self, _sender, _value, path):
        self.list_selected = path
        for candidate, widget in self.collection_widgets.items():
            dpg.set_value(widget, candidate == path)
        self.show(path)

    def render_collection(self):
        files = filter_collection(
            self.collection or [], dpg.get_value("query"), self.collection_filter
        )
        dpg.delete_item("collection_rows", children_only=True)
        self.collection_widgets = {}
        if files and self.collection_filter == "Duplikaty":
            groups = {}
            for file in files:
                groups.setdefault(file.group, []).append(file)
            counts = Counter(f.group for f in self.collection or [])
            for digest, members in groups.items():
                with dpg.tree_node(
                    parent="collection_rows",
                    label=f"{Path(members[0].path).name} — {counts[digest]} plików",
                    default_open=False,
                ):
                    self.add_collection_table(members)
        elif files:
            with dpg.group(parent="collection_rows"):
                self.add_collection_table(files)
        else:
            dpg.add_text("Brak plików pasujących do filtrów.", parent="collection_rows")
        if self.list_selected and not any(f.path == self.list_selected for f in files):
            self.list_selected = None
            self.clear_preview()
        dpg.set_value(
            "status",
            f"{len(files)} plików; zaznaczono {len(self.checked['Lista'])}; ukryto {self.collection_hidden} nieaktualnych",
        )

    def add_collection_table(self, files):
        with dpg.table(
            header_row=True, resizable=True, policy=dpg.mvTable_SizingStretchProp
        ):
            dpg.add_table_column(label="✓", width_fixed=True, init_width_or_weight=24)
            for label, weight in (
                ("Nazwa", 3),
                ("Folder", 2),
                ("Kopie", 1),
                ("Stan indeksowania", 2),
            ):
                dpg.add_table_column(label=label, init_width_or_weight=weight)
            for file in files:
                with dpg.table_row():
                    dpg.add_checkbox(
                        default_value=file.path in self.checked["Lista"],
                        callback=self.on_check,
                        user_data=file.path,
                    )
                    widget = dpg.add_selectable(
                        label=Path(file.path).name,
                        default_value=file.path == self.list_selected,
                        callback=self.select_collection,
                        user_data=file.path,
                        span_columns=True,
                    )
                    self.collection_widgets[file.path] = widget
                    self.file_menu(widget, file.digest, file.path)
                    dpg.add_text(str(Path(self.relative_path(file.path)).parent))
                    dpg.add_text(str(file.copies))
                    state = {
                        None: "Nieopisany",
                        "described": "Opisany",
                        "failed_describe": "Błąd opisu",
                    }[file.status]
                    if file.indexed:
                        state += " · w indeksie"
                    dpg.add_text(state)

    def close_related(self, *_):
        origin = self.related_origin
        self.related_origin = None
        dpg.hide_item("related")
        self.apply_layout()
        if origin:
            self.show(origin)
        pane = "list_pane" if self.mode == "Szukaj" else "list_content"
        self.restore_scroll = {pane: self.related_scroll}

    def queue_copy(self, kind):
        if self.current_path():
            self.pending_copy = (kind, self.preview_path)

    def open_current(self, *_):
        if self.current_path():
            try:
                dpg.set_value("status", open_externally(self.preview_path))
            except OSError as error:
                dpg.set_value("status", f"nie mogę otworzyć: {error}")

    def on_toggle_desc(self, *_: object) -> None:
        if not key_down(CTRL_KEYS):
            return
        self.show_desc = not self.show_desc
        self.apply_layout()

    def on_toggle_full(self, *_: object) -> None:
        self.fullscreen = not self.fullscreen
        self.apply_layout()

    def on_toggle_help(self, *_: object) -> None:
        if dpg.is_item_shown("help"):
            dpg.hide_item("help")
        else:
            dpg.show_item("help")

    def apply_layout(self) -> None:
        related = dpg.is_item_shown("related")
        dpg.configure_item(
            "list_pane",
            show=self.mode == "Szukaj" and not self.fullscreen and not related,
        )
        dpg.configure_item(
            "list_content",
            show=self.mode == "Lista" and not self.fullscreen and not related,
        )
        has_desc = self.preview_path is not None and self.show_desc
        dpg.configure_item("desc_variant", show=bool(self.variants) and self.show_desc)
        dpg.configure_item("preview_desc", show=has_desc)
        self.pane_w = 0  # force a refit on the next frame

    # -------------------------------------------------------------- preview

    def on_variant_change(self, _sender: object, app_data: str) -> None:
        self.render_description(self.variants.get(app_data))
        # The combo keeps nav focus after picking; hand the keyboard back.
        dpg.focus_item("query")

    def on_tab(self, *_: object) -> None:
        labels = list(self.variants)
        if len(labels) < 2:
            return
        delta = -1 if key_down(SHIFT_KEYS) else 1
        current = dpg.get_value("desc_variant")
        idx = labels.index(current) if current in labels else 0
        label = labels[(idx + delta) % len(labels)]
        dpg.set_value("desc_variant", label)
        self.render_description(self.variants.get(label))

    def render_description(self, raw: str | None) -> None:
        self.current_raw = raw
        dpg.delete_item("preview_desc", children_only=True)
        pane_w = dpg.get_item_rect_size("preview_pane")[0] or 800
        wrap_w = max(200, pane_w - 20)
        for key, value in description_fields(raw):
            if key is not None:
                dpg.add_text(f"{key}:", parent="preview_desc", color=KEY_COLOR)
                dpg.add_text(value, parent="preview_desc", wrap=wrap_w, indent=16)
            else:
                dpg.add_text(value, parent="preview_desc", wrap=wrap_w)
        dpg.configure_item("preview_desc", show=self.show_desc)

    def fit_image(self) -> None:
        w, h = self.img_size
        if not (w and h and dpg.does_item_exist("preview_img")):
            return
        pane_w, pane_h = dpg.get_item_rect_size("preview_pane") or (800, 700)
        if not (pane_w and pane_h):
            return
        # In fullscreen the description is hidden, so the image may use the
        # whole pane; otherwise leave room for the variant combo + text below.
        avail_h = (
            pane_h - 90 if self.fullscreen or not self.show_desc else pane_h * 0.6 - 90
        )
        scale = min(pane_w / w, avail_h / h, 1.0)
        dpg.configure_item(
            "preview_img", width=max(1, int(w * scale)), height=max(1, int(h * scale))
        )

    def show(self, path: str) -> None:
        # Messages from copy/open/load actions belong to the previous preview.
        dpg.set_value("status", "")
        indexed = self.con.execute(
            f"SELECT hash FROM ({indexed_files_sql(self.con)}) WHERE path = ?",
            (path,),
        ).fetchall()
        try:
            with open(path, "rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
        except OSError as e:
            dpg.set_value("status", f"nie mogę wczytać: {path}: {e}")
            return
        if (digest,) not in indexed:
            dpg.set_value("status", f"plik zniknął lub zmienił zawartość: {path}")
            return
        # load_image reports an unreadable file by returning None, not raising.
        loaded = dpg.load_image(path)
        if loaded is None:
            dpg.set_value("status", f"nie mogę wczytać: {path}")
            return
        w, h, _ch, data = loaded

        new_tex = dpg.add_static_texture(
            width=w, height=h, default_value=data, parent="tex_reg"
        )

        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")

        dpg.add_image(
            new_tex,
            tag="preview_img",
            parent="preview_pane",
            before="preview_path",
        )
        self.preview_path = path
        self.preview_hash = digest
        dpg.set_value("preview_path", f"{w} × {h} px · {self.relative_path(path)}")
        dpg.show_item("preview_path")
        dpg.show_item("file_actions")
        group = related_files(self.con, digest, identical=False)
        available, _ = current_result_paths([(h, p) for h, p, _, _ in group])
        dpg.configure_item("preview_actions", show=len(available) > 1)
        self.img_size = (w, h)
        self.fit_image()

        rows = self.con.execute(
            """
            SELECT d.model, d.prompt_version, d.effort_level, d.description_json
            FROM descriptions d
            WHERE d.hash = ? AND d.status = 'described'
            ORDER BY d.created_at DESC
            """,
            (digest,),
        ).fetchall()
        self.variants = {
            variant_label(model, prompt, effort): raw
            for model, prompt, effort, raw in rows
        }
        desc_cfg = self.cfg["describe"]
        active_label = variant_label(
            desc_cfg["model"], desc_cfg["prompt_version"], desc_cfg.get("effort_level")
        )
        labels = list(self.variants)
        selected = (
            active_label
            if active_label in self.variants
            else (labels[0] if labels else "")
        )
        dpg.configure_item(
            "desc_variant",
            items=labels,
            default_value=selected,
            show=bool(labels) and self.show_desc,
        )
        if selected:
            dpg.set_value("desc_variant", selected)
        self.render_description(self.variants.get(selected) if selected else None)

        if self.current_tex is not None:
            dpg.delete_item(self.current_tex)
        self.current_tex = new_tex

    def clear_preview(self) -> None:
        self.preview_path = None
        self.preview_hash = None
        dpg.hide_item("preview_path")
        dpg.hide_item("file_actions")
        dpg.hide_item("preview_actions")
        dpg.hide_item("related")
        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")
        if dpg.does_item_exist("preview_desc"):
            dpg.delete_item("preview_desc", children_only=True)
            dpg.configure_item("preview_desc", show=False)
        if dpg.does_item_exist("desc_variant"):
            dpg.configure_item("desc_variant", show=False)
        self.variants = {}
        self.current_raw = None
        self.img_size = (0, 0)
        if self.current_tex is not None:
            dpg.delete_item(self.current_tex)
            self.current_tex = None

    # ------------------------------------------------------------- main loop

    def handle_resize(self) -> None:
        """Refit the image and re-wrap the description when the pane changes
        width (window resize, fullscreen toggle) - neither adapts on its own."""
        pane_w = dpg.get_item_rect_size("preview_pane")[0]
        if not pane_w or abs(pane_w - self.pane_w) < 20:
            return
        self.pane_w = pane_w
        dpg.configure_item("preview_path", wrap=max(200, pane_w - 20))
        self.fit_image()
        if self.variants:
            self.render_description(self.current_raw)

    def run(self) -> None:
        self.build()
        while dpg.is_dearpygui_running():
            dpg.run_callbacks(dpg.get_callback_queue())
            self.poll_embedding()
            self.maybe_search()
            self.handle_resize()
            dpg.render_dearpygui_frame()
            if self.restore_scroll is not None:
                for pane, scroll in self.restore_scroll.items():
                    dpg.set_y_scroll(pane, scroll)
                self.restore_scroll = None
            self.flush_copy()
        dpg.destroy_context()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    common.add_config_argument(parser)


def run(args: argparse.Namespace, cfg: dict) -> None:
    App(cfg).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 5: search GUI (Dear PyGui)")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
