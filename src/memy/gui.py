"""Stage 5: Dear PyGui GUI over stage 4 search."""

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

import dearpygui.dearpygui as dpg
from PIL import Image, ImageOps

from memy import common, i18n
from memy import describe as describe_stage
from memy import embed as embed_stage
from memy import search as search_stage
from memy.collection import (
    collection_files,
    current_result_paths,
    delete_files,
    filter_collection,
    indexed_files_sql,
    related_files,
)
from memy.i18n import N_, _, ngettext, pgettext


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
SPLITTER_WIDTH = 8
MIN_PANE_WIDTH = 200
# Labels below are translated where they are shown: the module is imported
# before the configured language is set.
COMPARE_PARAMETERS = (
    ("model", N_("Model")),
    ("prompt_version", N_("Prompt")),
    ("effort_level", N_("Reasoning effort")),
)
MODES = (("search", N_("Search")), ("list", N_("List")), ("compare", N_("Compare")))
COLLECTION_FILTERS = (
    ("all", N_("All")),
    ("duplicates", N_("Duplicates")),
    ("identical", N_("Identical copies")),
    ("undescribed", N_("Undescribed")),
    ("failed", N_("Errors")),
)

# Modifier keys are queried as regular keys; the Mod* aliases are what ImGui
# reports on Wayland, the L/R ones are a cheap safety net on other backends.
CTRL_KEYS = (dpg.mvKey_ModCtrl, dpg.mvKey_LControl, dpg.mvKey_RControl)
SHIFT_KEYS = (dpg.mvKey_ModShift, dpg.mvKey_LShift, dpg.mvKey_RShift)
ALT_KEYS = (dpg.mvKey_ModAlt, dpg.mvKey_LAlt, dpg.mvKey_RAlt)


def shortcuts() -> list[tuple[str, str]]:
    return [
        ("Enter", _("search now (it also searches by itself after a moment)")),
        ("↑ / ↓", _("previous / next meme")),
        ("PgUp / PgDn", _("jump by {n} results").format(n=PAGE_STEP)),
        ("Ctrl+Home / Ctrl+End", _("first / last result")),
        ("Alt+1…9", _("jump to result N")),
        ("Ctrl+C", _("copy IMAGE / VIDEO to the clipboard (paste in a chat)")),
        ("Ctrl+Shift+C", _("copy the file path")),
        ("Ctrl+O / Shift+Enter", _("open in an image viewer")),
        ("Tab / Shift+Tab", _("next / previous description variant")),
        ("Ctrl+D", _("show / hide the description")),
        ("F11", _("full-width preview")),
        ("Ctrl+= / Ctrl+-", _("more / fewer results (by {n})").format(n=TOP_N_STEP)),
        ("Esc", _("back / clear the query / quit")),
        ("Ctrl+Q", _("quit")),
        ("F1", _("this cheat sheet")),
    ]


def key_down(keys: tuple[int, ...]) -> bool:
    return any(dpg.is_key_down(k) for k in keys)


def description_fields(raw: str | None) -> list[tuple[str | None, str]]:
    if not raw:
        return [(None, _("(no description)"))]
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
    return fields or [(None, _("(empty description)"))]


def copy_image_to_clipboard(path: str) -> str:
    """Put the meme on the clipboard; returns a status line.

    Still images go as PNG bytes: Chromium/Electron and Qt (Telegram) apps
    read pasted images only as image/png, so JPEG/WebP bytes paste nothing.
    Videos and animations go as a file reference (text/uri-list), as file
    managers copy files: apps reject video bytes, and PNG would drop frames.
    """
    as_file = is_animation(path)
    mime = "text/uri-list" if as_file else "image/png"
    if shutil.which("wl-copy"):
        cmd = ["wl-copy", "-t", mime]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", mime]
    else:
        return _("no wl-copy / xclip — nothing to copy with")
    try:
        if as_file:
            data = (Path(path).resolve().as_uri() + "\r\n").encode()
        else:
            with Image.open(path) as image:
                if image.format == "PNG":
                    data = Path(path).read_bytes()
                else:
                    # PNG carries no EXIF orientation and cannot store CMYK.
                    image = ImageOps.exif_transpose(image)
                    if image.mode not in ("1", "L", "LA", "I", "P", "RGB", "RGBA"):
                        image = image.convert("RGBA")
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    data = buffer.getvalue()
        # Both tools daemonize themselves once they own the selection.
        subprocess.run(cmd, input=data, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return _("copying failed: {error}").format(error=e)
    message = (
        _("copied video: {name}")
        if common.is_video(path)
        else _("copied image: {name}")
    )
    return message.format(name=os.path.basename(path))


def is_animation(path: str) -> bool:
    if common.is_video(path):
        return True
    try:
        with Image.open(path) as image:
            return bool(getattr(image, "is_animated", False))
    except Exception:  # noqa: BLE001 - unreadable files simply get no frame buttons
        return False


def open_externally(path: str) -> str:
    # Image viewers cannot play videos; let the desktop pick a player.
    viewers = [] if common.is_video(path) else search_stage.VIEWERS
    viewer = next((v for v in [*viewers, "xdg-open"] if shutil.which(v)), None)
    if not viewer:
        return _("no image viewer found")
    subprocess.Popen(
        [viewer, path],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _("opening in {viewer}").format(viewer=viewer)


# DPG 2.x autoloads all Unicode ranges from a bound font, so we only need to
# point it at a TTF that actually has Polish glyphs (default DPG font doesn't).
FONT_CANDIDATES = [
    "/usr/share/fonts/inter/Inter-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]


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
        # Video first frames and description frame sets, keyed by content hash;
        # extracted on demand and removed on exit.
        self.frame_dir = tempfile.TemporaryDirectory(prefix="meme-gui-frames-")
        self.frame_sets: dict[str, list[tuple[str, float | None]]] = {}
        self.frame_index = 0
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
        self.list_widths = {"search": 380, "list": 570}
        self.split_drag: tuple[float, float] | None = None
        self.show_desc = True
        self.fullscreen = False
        # Modes and filters are ids internally; widgets show translated labels.
        self.mode_labels = {mode: _(label) for mode, label in MODES}
        self.filter_labels = {kind: _(label) for kind, label in COLLECTION_FILTERS}
        # Filter combo sentinels sit among real model/prompt/effort values.
        self.compare_any = _("Any")
        self.compare_default_effort = _("Not set")
        self.mode = "search"
        self.mode_state = {}
        self.checked = {"search": set(), "list": set()}
        self.collection = None
        self.collection_hidden = 0
        self.collection_filter = "all"
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
        self.comparing = False
        self.compare_variants = ["", ""]
        self.compare_paths: list[str] = []
        self.compare_source_paths: list[str] = []
        self.compare_index = 0
        self.compare_origin: tuple[str | None, str, float, str] | None = None
        self.compare_size = (0, 0)
        self.compare_filters = [
            dict.fromkeys((key for key, _label in COMPARE_PARAMETERS), self.compare_any)
            for _side in range(2)
        ]
        self.compare_catalog: dict[str, tuple[str, str, str | None]] = {}
        self.compare_last_path: str | None = None
        self.compare_scroll = 0.0
        self.compare_job: tuple[str, str, str] | None = None
        self.compare_queue: deque[tuple[tuple[str, str, str], dict, dict]] = deque()
        self.compare_job_events = SimpleQueue()
        self.compare_job_messages: dict[tuple[str, str], str] = {}

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
            dpg.add_radio_button(
                list(self.mode_labels.values()),
                tag="view_mode",
                default_value=self.mode_labels[self.mode],
                horizontal=True,
                callback=self.on_mode_change,
            )
            with dpg.table(tag="search_controls", header_row=False, width=-1):
                dpg.add_table_column()
                dpg.add_table_column(
                    tag="search_balance_column",
                    width_fixed=True,
                    init_width_or_weight=155,
                )
                with dpg.table_row():
                    dpg.add_input_text(
                        tag="query",
                        hint=_("describe a meme…"),
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
                            _(
                                "Left: text matching (FTS).\n"
                                "Right: similarity of meaning (embeddings).\n"
                                "Middle: both weigh the same."
                            )
                        )
            with dpg.group(tag="search_content", horizontal=True, horizontal_spacing=0):
                # Anchor SameLine inside the group when F11 hides the left panes.
                dpg.add_spacer(width=0, height=0)
                with dpg.child_window(
                    tag="list_pane",
                    width=self.list_widths["search"],
                    height=-FOOTER_SPACE,
                    border=False,
                ):
                    pass
                with dpg.child_window(
                    tag="list_content",
                    width=self.list_widths["list"],
                    height=-FOOTER_SPACE,
                    border=False,
                    show=False,
                ):
                    dpg.add_combo(
                        list(self.filter_labels.values()),
                        default_value=self.filter_labels[self.collection_filter],
                        width=-1,
                        callback=self.on_filter_change,
                    )
                    dpg.add_button(
                        label=_("Embed described"),
                        tag="embed_described",
                        callback=self.start_embedding,
                    )
                    with dpg.tooltip("embed_described"):
                        dpg.add_text(
                            _(
                                "Fills in missing embeddings across the whole collection,\n"
                                "regardless of prompt, model, filters and selection.\n"
                                "Picks the newest successful description of each representative."
                            )
                        )
                    dpg.add_text("", tag="embed_progress", wrap=530)
                    dpg.add_group(tag="collection_rows")
                    dpg.add_button(
                        label=_("Delete selected files"),
                        callback=lambda: self.request_delete(self.checked[self.mode]),
                    )
                with dpg.child_window(
                    tag="related",
                    width=self.list_widths[self.mode],
                    height=-FOOTER_SPACE,
                    border=False,
                    show=False,
                ):
                    pass
                dpg.add_button(
                    tag="pane_splitter",
                    width=SPLITTER_WIDTH,
                    height=-FOOTER_SPACE,
                )
                with dpg.tooltip("pane_splitter"):
                    dpg.add_text(_("Drag to resize the panels."))
                with dpg.child_window(
                    tag="preview_pane",
                    autosize_x=True,
                    height=-FOOTER_SPACE,
                    border=False,
                ):
                    dpg.add_text("", tag="preview_path", show=False, wrap=700)
                    with dpg.group(tag="frame_nav", horizontal=True, show=False):
                        dpg.add_button(
                            label="<", small=True, callback=lambda: self.step_frame(-1)
                        )
                        dpg.add_button(
                            label=">", small=True, callback=lambda: self.step_frame(1)
                        )
                        dpg.add_text("", tag="frame_label")
                        with dpg.tooltip("frame_nav"):
                            dpg.add_text(_("Frames sent to the model when describing."))
                    with dpg.group(tag="preview_actions", horizontal=True, show=False):
                        dpg.add_button(
                            label=_("Duplicate group"),
                            callback=lambda: self.open_related(identical=False),
                        )
                        dpg.add_button(
                            label=_("Identical copies"),
                            callback=lambda: self.open_related(identical=True),
                        )
                        dpg.add_button(
                            label=_("Back to result"),
                            callback=self.close_related,
                        )
                    with dpg.group(tag="file_actions", horizontal=True, show=False):
                        dpg.add_button(
                            label=_("Copy image"),
                            tag="copy_file",
                            callback=lambda: self.queue_copy("image"),
                        )
                        dpg.add_button(
                            label=_("Copy path"),
                            callback=lambda: self.queue_copy("path"),
                        )
                        dpg.add_button(label=_("Open"), callback=self.open_current)
                    dpg.add_combo(
                        tag="desc_variant",
                        items=[],
                        callback=self.on_variant_change,
                        show=False,
                        width=-1,
                    )
                    dpg.add_button(
                        label=_("Compare descriptions"),
                        tag="compare_open",
                        callback=self.open_comparison,
                        show=False,
                    )
                    dpg.add_group(tag="preview_desc", show=False)

            with dpg.group(tag="compare_view", show=False):
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label=_("Back"),
                        tag="compare_back",
                        callback=self.close_comparison,
                    )
                    dpg.add_button(
                        label=_("Previous meme"),
                        tag="compare_previous",
                        callback=lambda: self.step_comparison(-1),
                    )
                    dpg.add_button(
                        label=_("Next meme"),
                        tag="compare_next",
                        callback=lambda: self.step_comparison(1),
                    )
                    dpg.add_text("", tag="compare_position")
                    dpg.add_text("", tag="compare_queue_status", color=HINT_COLOR)
                with (
                    dpg.group(tag="compare_image_row", horizontal=True),
                    dpg.group(tag="compare_image_info"),
                ):
                    dpg.add_text("", tag="compare_path", wrap=700)
                    dpg.add_text(
                        _("Click the image to enlarge it."),
                        tag="compare_image_hint",
                        color=HINT_COLOR,
                    )
                    dpg.add_button(
                        label=_("Copy comparison"),
                        tag="compare_copy",
                        callback=self.copy_comparison,
                        enabled=False,
                    )
                    dpg.add_button(
                        label=_("Copy all pairs"),
                        tag="compare_copy_all",
                        callback=self.copy_all_comparisons,
                        enabled=False,
                    )
                    with dpg.tooltip("compare_copy_all"):
                        dpg.add_text(
                            _(
                                "Copies memes from the current list that have both selected variants."
                            )
                        )
                with dpg.table(
                    header_row=False,
                    width=-1,
                    policy=dpg.mvTable_SizingStretchSame,
                ):
                    dpg.add_table_column()
                    dpg.add_table_column()
                    with dpg.table_row():
                        for side in range(2):
                            with dpg.group():
                                with dpg.table(
                                    header_row=False,
                                    width=-1,
                                    policy=dpg.mvTable_SizingStretchSame,
                                ):
                                    for _key, _label in COMPARE_PARAMETERS:
                                        dpg.add_table_column()
                                    with dpg.table_row():
                                        for key, label in COMPARE_PARAMETERS:
                                            with dpg.group():
                                                dpg.add_text(
                                                    _(label),
                                                    tag=f"compare_filter_label_{side}_{key}",
                                                )
                                                dpg.add_combo(
                                                    tag=f"compare_filter_{side}_{key}",
                                                    items=[self.compare_any],
                                                    default_value=self.compare_any,
                                                    width=-1,
                                                    callback=self.on_compare_filter,
                                                    user_data=(side, key),
                                                )
                                dpg.add_text("", tag=f"compare_match_count_{side}")
                                dpg.add_combo(
                                    tag=f"compare_variant_{side}",
                                    items=[],
                                    width=-1,
                                    callback=self.on_compare_variant,
                                    user_data=side,
                                )
                                dpg.add_text(
                                    _(
                                        "No description matches the selected parameters."
                                    ),
                                    tag=f"compare_missing_{side}",
                                    show=False,
                                    color=HINT_COLOR,
                                )
                                dpg.add_button(
                                    label=_("Describe and embed this meme"),
                                    tag=f"compare_generate_{side}",
                                    callback=self.start_comparison_generation,
                                    user_data=side,
                                    show=False,
                                )
                                with dpg.tooltip(f"compare_generate_{side}"):
                                    dpg.add_text(
                                        _(
                                            "Uses the exact variant from the list and the\n"
                                            "backend from the config. This meme's search\n"
                                            "index will be updated from this description."
                                        )
                                    )
                with dpg.child_window(
                    tag="compare_fields", height=-FOOTER_SPACE, border=False
                ):
                    pass

            with dpg.table(tag="footer", header_row=False, width=-1):
                dpg.add_table_column()
                dpg.add_table_column(width_fixed=True, init_width_or_weight=110)
                with dpg.table_row():
                    dpg.add_text("", tag="status")
                    dpg.add_text(
                        _("— F1: shortcuts"), tag="status_hint", color=HINT_COLOR
                    )

        dpg.add_window(
            label=_("Keyboard shortcuts"),
            tag="help",
            show=False,
            width=560,
            height=460,
            pos=(340, 120),
        )
        with dpg.window(
            label=_("Meme preview"),
            tag="compare_zoom",
            modal=True,
            show=False,
            width=700,
            height=420,
        ):
            dpg.add_button(
                label=_("Close"),
                tag="compare_zoom_close",
                callback=self.close_comparison_zoom,
            )
        with dpg.window(
            label=_("Confirm deletion"),
            tag="delete_confirm",
            modal=True,
            show=False,
            width=700,
            height=420,
        ):
            dpg.add_text(_("The files will be permanently deleted from disk."))
            dpg.add_text("", tag="delete_keep", wrap=660)
            with dpg.child_window(height=250):
                dpg.add_text("", tag="delete_paths", wrap=660)
            with dpg.group(horizontal=True):
                dpg.add_button(label=_("Delete"), callback=self.confirm_delete)
                dpg.add_button(
                    label=_("Cancel"), callback=lambda: dpg.hide_item("delete_confirm")
                )
        with dpg.window(
            label=_("Deletion result"),
            tag="delete_report",
            modal=True,
            show=False,
            width=700,
            height=420,
        ):
            with dpg.child_window(height=320):
                dpg.add_text("", tag="delete_errors", wrap=660)
            dpg.add_button(
                label=_("Close"), callback=lambda: dpg.hide_item("delete_report")
            )
        with dpg.table(parent="help", header_row=False, borders_innerH=False):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=180)
            dpg.add_table_column()
            for keys, what in shortcuts():
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

    def on_query_change(self, *_args: object) -> None:
        # Search-as-you-type: arm the debounce, the run loop fires it.
        self.search_at = time.monotonic() + DEBOUNCE_S

    def on_balance_change(self, _sender: object, value: int) -> None:
        self.embedding_weight = value / 100
        if self.mode == "search":
            self.on_query_change()

    def maybe_search(self) -> None:
        if self.comparing:
            return
        if self.search_at is None or time.monotonic() < self.search_at:
            return
        self.search_at = None
        self.do_search()

    def do_search(self) -> None:
        if self.related_origin is not None:
            self.close_related()
        if self.mode == "list":
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
                variant=self.cfg["describe"],
            )
            paths, hidden = current_result_paths(hits)
        # Endpoint down or timing out (OSError), a malformed or wrong-sized
        # embedding response (ValueError, KeyError), or a DB hiccup.
        except (OSError, ValueError, KeyError, sqlite3.Error) as e:
            dpg.set_value("status", _("error: {error}").format(error=e))
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
            status = ngettext(
                "{n} result (of {top_n})", "{n} results (of {top_n})", len(paths)
            ).format(n=len(paths), top_n=self.top_n)
            if hidden:
                status += ngettext(
                    "; {n} outdated hidden", "; {n} outdated hidden", hidden
                ).format(n=hidden)
            dpg.set_value("status", status)
        else:
            self.current_idx = -1
            dpg.set_value("status", _("no results"))
            self.clear_preview()

    def bump_top_n(self, delta: int) -> None:
        if self.mode != "search":
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
            dpg.set_value("status", _("result limit: {n}").format(n=self.top_n))

    # ----------------------------------------------------------- navigation

    def on_select(self, _sender: object, _app_data: object, user_data: int) -> None:
        self.select_index(user_data)
        dpg.focus_item("query")

    def select_index(self, idx: int) -> None:
        if self.comparing:
            self.select_comparison(idx)
            return
        if self.mode != "search" or self.related_origin is not None:
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
        if self.comparing:
            if not dpg.is_item_shown("compare_zoom") and not any(
                dpg.is_item_focused(tag) or dpg.is_item_active(tag)
                for tag in (
                    *(f"compare_variant_{side}" for side in range(2)),
                    *(
                        f"compare_filter_{side}_{key}"
                        for side in range(2)
                        for key, _label in COMPARE_PARAMETERS
                    ),
                )
            ):
                self.step_comparison(delta)
            return
        if not self.paths:
            return
        self.select_index(min(len(self.paths) - 1, max(0, self.current_idx + delta)))

    def on_home(self, *_args: object) -> None:
        if key_down(CTRL_KEYS):
            self.select_index(0)

    def on_end(self, *_args: object) -> None:
        if key_down(CTRL_KEYS):
            paths = self.compare_paths if self.comparing else self.paths
            self.select_index(len(paths) - 1)

    def on_digit(self, digit: int) -> None:
        if key_down(ALT_KEYS):
            self.select_index(digit - 1)

    def on_return(self, *_args: object) -> None:
        if key_down(SHIFT_KEYS):
            self.on_open()
            return
        if self.comparing:
            return
        self.search_at = None
        self.do_search()

    def on_escape(self, *_args: object) -> None:
        if dpg.is_item_shown("delete_confirm"):
            dpg.hide_item("delete_confirm")
        elif dpg.is_item_shown("delete_report"):
            dpg.hide_item("delete_report")
        elif dpg.is_item_shown("compare_zoom"):
            self.close_comparison_zoom()
        elif dpg.is_item_shown("help"):
            dpg.hide_item("help")
        elif self.comparing:
            self.close_comparison()
        elif dpg.is_item_shown("related"):
            self.close_related()
        elif dpg.get_value("query"):
            dpg.set_value("query", "")
            self.on_query_change()
        else:
            dpg.stop_dearpygui()

    def on_quit(self, *_args: object) -> None:
        if key_down(CTRL_KEYS):
            dpg.stop_dearpygui()

    # ------------------------------------------------------------- actions

    def current_path(self) -> str | None:
        if self.preview_path:
            return self.preview_path
        dpg.set_value("status", _("nothing to copy"))
        return None

    def relative_path(self, path: str) -> str:
        return os.path.relpath(path, self.image_dir)

    def request_delete(self, paths, *, keep=None):
        if self.embedding_running or self.compare_job is not None or self.compare_queue:
            dpg.set_value("status", _("Deletion will be available after embedding."))
            return
        paths = set(paths)
        rows = self.con.execute(indexed_files_sql(self.con)).fetchall()
        self.pending_delete = [(h, p) for h, p in rows if p in paths and p != keep]
        self.pending_delete = sorted(set(self.pending_delete), key=lambda row: row[1])
        if not self.pending_delete:
            dpg.set_value("status", _("No files selected for deletion."))
            return
        dpg.set_value(
            "delete_keep",
            _("Keep: {path}").format(path=self.relative_path(keep)) if keep else "",
        )
        dpg.set_value(
            "delete_paths",
            "\n".join(self.relative_path(p) for _digest, p in self.pending_delete),
        )
        dpg.show_item("delete_confirm")

    def delete_rest(self, digest, keep):
        rows = related_files(self.con, digest, identical=False)
        paths, _hidden = current_result_paths([(row[0], row[1]) for row in rows])
        self.request_delete(paths, keep=keep)

    def file_menu(self, widget, digest, path):
        with dpg.popup(widget, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(
                label=_("Delete file"), callback=lambda: self.request_delete([path])
            )
            dpg.add_menu_item(
                label=_("Delete the rest of the group"),
                callback=lambda: self.delete_rest(digest, path),
            )

    def confirm_delete(self, *_args):
        if self.embedding_running or self.compare_job is not None or self.compare_queue:
            dpg.set_value("status", _("Deletion will be available after embedding."))
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
        if self.mode == "search":
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
        message = ngettext(
            "Deleted {n} file; errors: {errors}.",
            "Deleted {n} files; errors: {errors}.",
            len(removed),
        ).format(n=len(removed), errors=len(errors))
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
        paths, hidden = current_result_paths([(row[0], row[1]) for row in rows])
        available = set(paths)
        if self.related_origin is None:
            self.related_origin = self.preview_path
            pane = "list_pane" if self.mode == "search" else "list_content"
            self.related_scroll = dpg.get_y_scroll(pane)
        dpg.delete_item("related", children_only=True)
        dpg.add_button(label=_("Back"), parent="related", callback=self.close_related)
        if self.mode == "list":
            dpg.add_button(
                label=_("Delete selected files"),
                parent="related",
                callback=lambda: self.request_delete(self.checked[self.mode]),
            )
        title = _("Identical copies") if identical else _("Duplicate group")
        dpg.add_text(title, parent="related")
        dpg.add_text(self.relative_path(self.preview_path), parent="related", wrap=680)
        dpg.add_text(
            ngettext(
                "{n} file; click a path to preview the image.",
                "{n} files; click a path to preview the image.",
                len(paths),
            ).format(n=len(paths)),
            parent="related",
        )
        if len(paths) <= 1:
            dpg.add_text(
                _("No other identical copies.")
                if identical
                else _("No other duplicates."),
                parent="related",
            )
        if hidden:
            dpg.add_text(
                ngettext(
                    "Hid {n} outdated path.", "Hid {n} outdated paths.", hidden
                ).format(n=hidden),
                parent="related",
            )
        for digest, path, representative, primary in rows:
            if path not in available:
                continue
            markers = []
            if representative and not identical:
                markers.append(_("representative"))
            if primary:
                markers.append(_("primary copy"))
            suffix = f" ({', '.join(markers)})" if markers else ""
            if self.mode == "list":
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
            if self.mode == "list":
                self.file_menu(widget, digest, path)
        dpg.show_item("related")
        self.apply_layout()

    def on_related_select(
        self, _sender: object, _app_data: object, user_data: str
    ) -> None:
        self.show(user_data)
        dpg.focus_item("query")

    def on_copy(self, *_args: object) -> None:
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
            dpg.set_value("status", _("copied path: {path}").format(path=path))
        else:
            dpg.set_value("status", copy_image_to_clipboard(path))

    def on_open(self, *_args: object) -> None:
        # Reachable as Ctrl+O and as Shift+Enter (which routes through here).
        if not (key_down(CTRL_KEYS) or key_down(SHIFT_KEYS)):
            return
        self.open_current()

    # ---------------------------------------------------------- view toggles

    def on_mode_change(self, _sender: object, label: str) -> None:
        mode = next(m for m, text in self.mode_labels.items() if text == label)
        if mode == "compare":
            self.open_comparison(from_preview=False)
            return
        if self.comparing:
            self.close_comparison()
        if mode == self.mode:
            return
        if self.mode == "list" and self.search_at is not None:
            self.render_collection()
        pane = "list_pane" if self.mode == "search" else "list_content"
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
        self.mode = mode
        dpg.configure_item("search_balance_column", show=self.mode == "search")
        dpg.configure_item("search_balance", show=self.mode == "search")
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
            hint=_("describe a meme…")
            if self.mode == "search"
            else _("filter names and paths…"),
        )
        if self.mode == "list":
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
        pane = "list_pane" if self.mode == "search" else "list_content"
        self.restore_scroll = {
            pane: state[2],
            "preview_pane": state[3],
            "related": state[6],
        }
        if state[4] or self.mode == "search":
            dpg.set_value("status", state[4])
        if self.mode == "search" and (
            state[0].strip() != self.last_query
            or self.embedding_weight != self.last_embedding_weight
        ):
            self.on_query_change()
        dpg.focus_item("query")

    def on_filter_change(self, _sender, label):
        self.collection_filter = next(
            kind for kind, text in self.filter_labels.items() if text == label
        )
        self.render_collection()

    def start_embedding(self, *_args):
        if (
            self.embedding_running
            or self.compare_job is not None
            or self.compare_queue
            or self.mode != "list"
        ):
            return
        self.embedding_running = True
        self.embedding_counts = (0, 0, 0)
        dpg.configure_item("embed_described", enabled=False)
        dpg.set_value("embed_progress", _("Loading the embedding queue…"))
        dpg.hide_item("delete_confirm")
        self.pending_delete = []
        Thread(target=self.embed_worker, daemon=True).start()
        for side in range(2):
            self.update_comparison_generation(side)

    def embed_worker(self):
        # Own connection; only the render thread may touch DPG or self.con.
        error = None
        try:
            with closing(common.connect(self.cfg["db_path"])) as con:
                embed_stage.embed_descriptions(
                    con,
                    self.cfg["describe"],
                    common.embed_configs(self.cfg),
                    selection="newest",
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
                    _(
                        "Embeddings: {processed}/{total}; done: {done}, errors: {failed}"
                    ).format(
                        processed=done + failed, total=total, done=done, failed=failed
                    ),
                )
                continue
            self.embedding_running = False
            dpg.configure_item("embed_described", enabled=True)
            for side in range(2):
                self.update_comparison_generation(side)
            self.collection, self.collection_hidden = collection_files(
                self.con, self.cfg["describe"]
            )
            if self.mode == "list":
                scroll = dpg.get_y_scroll("list_content")
                self.render_collection()
                self.restore_scroll = {"list_content": scroll}
            # Refresh search too, including when restored after a mode switch.
            self.last_query = ""
            if self.mode == "search":
                self.on_query_change()
            done, failed, total = self.embedding_counts
            if data is not None:
                message = _("Embedding error: {error}").format(error=data)
            elif total:
                message = _(
                    "Embedding finished: {done}/{total}; errors: {failed}."
                ).format(done=done, total=total, failed=failed)
            else:
                message = _("All described memes already have embeddings.")
            dpg.set_value("embed_progress", message)

    def on_check(self, _sender, value, path):
        selected = self.checked[self.mode]
        selected.add(path) if value else selected.discard(path)
        dpg.set_value(
            "status",
            ngettext("Selected {n} file", "Selected {n} files", len(selected)).format(
                n=len(selected)
            ),
        )

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
        if files and self.collection_filter == "duplicates":
            groups = {}
            for file in files:
                groups.setdefault(file.group, []).append(file)
            counts = Counter(f.group for f in self.collection or [])
            for digest, members in groups.items():
                with dpg.tree_node(
                    parent="collection_rows",
                    label=ngettext(
                        "{name} — {n} file", "{name} — {n} files", counts[digest]
                    ).format(name=Path(members[0].path).name, n=counts[digest]),
                    default_open=False,
                ):
                    self.add_collection_table(members)
        elif files:
            with dpg.group(parent="collection_rows"):
                self.add_collection_table(files)
        else:
            dpg.add_text(_("No files match the filters."), parent="collection_rows")
        if self.list_selected and not any(f.path == self.list_selected for f in files):
            self.list_selected = None
            self.clear_preview()
        dpg.set_value(
            "status",
            ngettext(
                "{n} file; selected: {selected}; outdated hidden: {hidden}",
                "{n} files; selected: {selected}; outdated hidden: {hidden}",
                len(files),
            ).format(
                n=len(files),
                selected=len(self.checked["list"]),
                hidden=self.collection_hidden,
            ),
        )

    def add_collection_table(self, files):
        with dpg.table(
            header_row=True, resizable=True, policy=dpg.mvTable_SizingStretchProp
        ):
            dpg.add_table_column(label="✓", width_fixed=True, init_width_or_weight=24)
            for label, weight in (
                (_("Name"), 3),
                (_("Folder"), 2),
                (_("Copies"), 1),
                (_("Indexing state"), 2),
            ):
                dpg.add_table_column(label=label, init_width_or_weight=weight)
            states = {
                None: pgettext("file state", "Undescribed"),
                "described": pgettext("file state", "Described"),
                "failed_describe": pgettext("file state", "Description error"),
            }
            for file in files:
                with dpg.table_row():
                    dpg.add_checkbox(
                        default_value=file.path in self.checked["list"],
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
                    state = states[file.status]
                    if file.indexed:
                        state += " · " + pgettext("file state", "indexed")
                    dpg.add_text(state)

    def close_related(self, *_args):
        origin = self.related_origin
        self.related_origin = None
        dpg.hide_item("related")
        self.apply_layout()
        if origin:
            self.show(origin)
        pane = "list_pane" if self.mode == "search" else "list_content"
        self.restore_scroll = {pane: self.related_scroll}

    def queue_copy(self, kind):
        if self.current_path():
            self.pending_copy = (kind, self.preview_path)

    def open_current(self, *_args):
        if self.current_path():
            try:
                dpg.set_value("status", open_externally(self.preview_path))
            except OSError as error:
                dpg.set_value("status", _("cannot open: {error}").format(error=error))

    def on_toggle_desc(self, *_args: object) -> None:
        if self.comparing:
            return
        if not key_down(CTRL_KEYS):
            return
        self.show_desc = not self.show_desc
        self.apply_layout()

    def on_toggle_full(self, *_args: object) -> None:
        if self.comparing:
            return
        self.fullscreen = not self.fullscreen
        self.apply_layout()

    def on_toggle_help(self, *_args: object) -> None:
        if dpg.is_item_shown("help"):
            dpg.hide_item("help")
        else:
            dpg.show_item("help")

    def apply_layout(self) -> None:
        dpg.configure_item("search_controls", show=not self.comparing)
        dpg.configure_item("search_content", show=not self.comparing)
        dpg.configure_item("compare_view", show=self.comparing)
        related = self.related_origin is not None
        dpg.configure_item("related", show=related and not self.fullscreen)
        dpg.configure_item(
            "list_pane",
            show=self.mode == "search" and not self.fullscreen and not related,
        )
        dpg.configure_item(
            "list_content",
            show=self.mode == "list" and not self.fullscreen and not related,
        )
        has_desc = self.preview_path is not None and self.show_desc
        dpg.configure_item("desc_variant", show=bool(self.variants) and self.show_desc)
        dpg.configure_item("preview_desc", show=has_desc)
        dpg.configure_item("pane_splitter", show=not self.fullscreen)
        self.split_drag = None
        self.pane_w = 0  # force a refit on the next frame

    def resize_panes(self) -> None:
        """Keep the panels within the window and track a splitter drag."""
        if self.fullscreen or self.comparing:
            self.split_drag = None
            return
        available = (
            dpg.get_available_content_region("search_content")[0] - SPLITTER_WIDTH
        )
        if available <= 0:
            return
        pane = "list_pane" if self.mode == "search" else "list_content"
        if self.related_origin is not None:
            pane = "related"
        width = self.list_widths[self.mode]
        if dpg.is_item_active("pane_splitter"):
            mouse_x = dpg.get_mouse_pos(local=False)[0]
            if self.split_drag is None:
                self.split_drag = (mouse_x, dpg.get_item_rect_size(pane)[0])
            start_x, start_width = self.split_drag
            width = start_width + mouse_x - start_x
        else:
            self.split_drag = None
        # On very narrow windows split the remaining room equally.
        minimum = min(MIN_PANE_WIDTH, available / 2)
        width = int(max(minimum, min(width, available - minimum)))
        if self.split_drag is not None:
            self.list_widths[self.mode] = width
        for tag in ("list_pane", "list_content", "related"):
            if dpg.is_item_shown(tag):
                dpg.configure_item(tag, width=width)
        height = dpg.get_item_rect_size(pane)[1]
        if height > 0:
            dpg.configure_item("pane_splitter", height=height)

    # ----------------------------------------------------------- comparison

    def comparison_job_position(self, digest: str | None, label: str) -> int | None:
        key = (digest, label)
        if self.compare_job is not None and self.compare_job[1:] == key:
            return 0
        for position, (job, _cfg, _embed_cfg) in enumerate(self.compare_queue, 1):
            if job[1:] == key:
                return position
        return None

    def update_comparison_queue(self) -> None:
        dpg.set_value(
            "compare_queue_status",
            _("Descriptions: running {running}; queued: {queued}").format(
                running=int(self.compare_job is not None),
                queued=len(self.compare_queue),
            )
            if self.compare_job is not None or self.compare_queue
            else "",
        )
        for side in range(2):
            self.update_comparison_generation(side)

    def update_comparison_generation(self, side: int) -> None:
        label = self.compare_variants[side]
        key = (self.preview_hash, label)
        missing = label not in self.variants
        message = self.compare_job_messages.get(key)
        position = self.comparison_job_position(self.preview_hash, label)
        if position is not None and position > 0:
            message = _("Queued: position {n}.").format(n=position)
        if message:
            dpg.set_value(f"compare_missing_{side}", message)
        dpg.configure_item(f"compare_missing_{side}", show=missing or bool(message))
        dpg.configure_item(
            f"compare_generate_{side}",
            label=_("Describing…")
            if position == 0
            else _("Queued")
            if position is not None
            else _("Add to queue")
            if self.compare_job is not None
            else _("Describe and embed this meme"),
            show=bool(
                self.comparing
                and self.preview_path
                and self.preview_hash
                and label in self.compare_catalog
                and missing
            ),
            enabled=not self.embedding_running and position is None,
        )

    def start_comparison_generation(
        self, _sender: object, _value: object, side: int
    ) -> None:
        label = self.compare_variants[side]
        if (
            not self.comparing
            or self.embedding_running
            or not self.preview_path
            or not self.preview_hash
            or label not in self.comparison_matches(side)
            or label in self.variants
            or self.comparison_job_position(self.preview_hash, label) is not None
        ):
            return
        model, prompt, effort = self.compare_catalog[label]
        cfg = deepcopy(self.cfg["describe"])
        cfg.update(model=model, prompt_version=prompt)
        # NULL must clear the active configuration's effort, not inherit it.
        cfg["effort_level"] = effort
        job = (self.preview_path, self.preview_hash, label)
        self.compare_queue.append((job, cfg, common.embed_configs(self.cfg)))
        self.start_next_comparison_generation()

    def start_next_comparison_generation(self) -> None:
        if self.compare_job is not None or not self.compare_queue:
            self.update_comparison_queue()
            return
        job, cfg, embed_cfgs = self.compare_queue.popleft()
        self.compare_job = job
        self.compare_job_messages[job[1:]] = _("Describing…")
        dpg.configure_item("embed_described", enabled=False)
        dpg.hide_item("delete_confirm")
        self.pending_delete = []
        self.update_comparison_queue()
        Thread(
            target=self.comparison_generation_worker,
            args=(job, cfg, embed_cfgs),
            daemon=True,
        ).start()

    def comparison_generation_worker(
        self, job: tuple[str, str, str], cfg: dict, embed_cfgs: list[dict]
    ) -> None:
        path, digest, _label = job
        error = None
        stage = "description"
        try:
            with closing(common.connect(self.cfg["db_path"])) as con:
                indexed = con.execute(
                    f"SELECT hash FROM ({indexed_files_sql(con)}) WHERE path = ?",
                    (path,),
                ).fetchone()
                if indexed != (digest,):
                    raise ValueError(
                        _("The file is no longer indexed under this hash.")
                    )
                parameters = (
                    digest,
                    cfg["model"],
                    cfg["prompt_version"],
                    cfg.get("effort_level"),
                )

                def successful_description() -> int | None:
                    row = con.execute(
                        "SELECT id FROM descriptions WHERE hash = ? "
                        "AND model = ? AND prompt_version = ? AND effort_level IS ? "
                        "AND status = 'described'",
                        parameters,
                    ).fetchone()
                    return row[0] if row else None

                description_id = successful_description()
                if description_id is None:
                    backend = cfg.get("backend", "openai")
                    if backend not in (
                        "openai",
                        "claude-code-cli",
                        "codex-cli",
                        "opencode-cli",
                    ):
                        raise ValueError(
                            _("Unknown backend: {backend}").format(backend=backend)
                        )
                    prompt = (
                        Path(__file__).parent
                        / "prompts"
                        / f"{cfg['prompt_version']}.txt"
                    ).read_text()
                    # Describe the verified snapshot, even if the source changes
                    # while the model is working. Other copies share this hash.
                    with tempfile.TemporaryDirectory(prefix="meme-compare-") as tmp:
                        snapshot = Path(tmp) / Path(path).name
                        shutil.copyfile(path, snapshot)
                        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
                            raise ValueError(
                                _("The file content has changed; run a scan.")
                            )
                        describe_stage.describe_rows(
                            con,
                            cfg,
                            [(digest, str(snapshot))],
                            prompt,
                            backend,
                            False,
                            lambda: False,
                            self.cfg.get("transcribe"),
                        )
                    description_id = successful_description()
                    if description_id is None:
                        row = con.execute(
                            "SELECT error FROM descriptions WHERE hash = ? AND model = ? "
                            "AND prompt_version = ? AND effort_level IS ?",
                            parameters,
                        ).fetchone()
                        raise ValueError(
                            row[0] if row else _("No description was saved.")
                        )
                # Committed description becomes visible before the embedding request.
                self.compare_job_events.put(("described", job, None))
                stage = "embedding"
                embed_stage.index_description(con, description_id, embed_cfgs)
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            self.compare_job_events.put((stage, job, error))

    def poll_comparison_generation(self) -> None:
        while True:
            try:
                stage, job, error = self.compare_job_events.get_nowait()
            except Empty:
                return
            path, digest, label = job
            key = (digest, label)
            if stage == "described":
                self.compare_job_messages[key] = _("Description ready. Embedding…")
            else:
                self.compare_job = None
                dpg.configure_item("embed_described", enabled=not self.compare_queue)
                if error:
                    self.compare_job_messages[key] = (
                        _("Description saved. Creating the embedding failed.")
                        if stage == "embedding"
                        else _("Describing failed: {error}").format(error=error)
                    )
                else:
                    self.compare_job_messages.pop(key, None)
                dpg.set_value(
                    "status",
                    f"{self.relative_path(path)}: "
                    + (
                        self.compare_job_messages[key]
                        if error
                        else _("description and embedding ready.")
                    ),
                )
                self.last_query = ""
                if self.mode == "search" and not self.comparing:
                    self.on_query_change()
            self.load_comparison_catalog()
            if self.preview_hash == digest:
                self.load_preview_descriptions()
            if self.comparing:
                for side in range(2):
                    self.update_comparison_choices(side)
                self.render_comparison_fields()
                dpg.configure_item("compare_copy", enabled=self.can_copy_comparison())
                dpg.configure_item(
                    "compare_copy_all", enabled=self.can_copy_all_comparisons()
                )
            else:
                for side in range(2):
                    self.update_comparison_generation(side)
            if self.collection is not None:
                self.collection, self.collection_hidden = collection_files(
                    self.con, self.cfg["describe"]
                )
                if self.mode == "list" and not self.comparing:
                    scroll = dpg.get_y_scroll("list_content")
                    self.render_collection()
                    self.restore_scroll = {
                        **(self.restore_scroll or {}),
                        "list_content": scroll,
                    }
            if stage != "described":
                self.start_next_comparison_generation()

    def load_comparison_catalog(self) -> None:
        rows = self.con.execute(
            "SELECT DISTINCT model, prompt_version, effort_level FROM descriptions "
            "WHERE status = 'described' ORDER BY model, prompt_version, effort_level"
        ).fetchall()
        self.compare_catalog = {
            variant_label(model, prompt, effort): (model, prompt, effort)
            for model, prompt, effort in rows
        }
        for side, filters in enumerate(self.compare_filters):
            for index, (key, _label) in enumerate(COMPARE_PARAMETERS):
                values = sorted(
                    {
                        row[index]
                        if row[index] is not None
                        else self.compare_default_effort
                        for row in rows
                    }
                )
                items = list(dict.fromkeys([self.compare_any, *values, filters[key]]))
                dpg.configure_item(f"compare_filter_{side}_{key}", items=items)
                dpg.set_value(f"compare_filter_{side}_{key}", filters[key])

    def comparison_matches(self, side: int) -> list[str]:
        filters = self.compare_filters[side]
        return [
            label
            for label, parameters in self.compare_catalog.items()
            if all(
                filters[key] == self.compare_any
                or filters[key]
                == (value if value is not None else self.compare_default_effort)
                for (key, _label), value in zip(COMPARE_PARAMETERS, parameters)
            )
        ]

    def update_comparison_choices(self, side: int) -> None:
        labels = self.comparison_matches(side)
        selected = self.compare_variants[side]
        available = [label for label in labels if label in self.variants]
        if selected not in labels:
            candidates = available or labels
            other = self.compare_variants[1 - side]
            selected = next(
                (label for label in candidates if label != other),
                candidates[0] if candidates else "",
            )
            self.compare_variants[side] = selected
        dpg.configure_item(
            f"compare_variant_{side}", items=labels, show=len(labels) > 1
        )
        dpg.set_value(f"compare_variant_{side}", selected)
        dpg.set_value(
            f"compare_match_count_{side}",
            _("Description: {variant}").format(variant=selected)
            if len(labels) == 1
            else _("Variants for this meme: {available} / {total}").format(
                available=len(available), total=len(labels)
            ),
        )
        dpg.set_value(
            f"compare_missing_{side}",
            _("This meme has no description in the selected variant.")
            if labels
            else _("No descriptions match the selected parameters."),
        )
        dpg.configure_item(
            f"compare_missing_{side}", show=selected not in self.variants
        )
        self.update_comparison_generation(side)

    def on_compare_filter(
        self, _sender: object, value: str, user_data: tuple[int, str]
    ) -> None:
        side, key = user_data
        self.compare_filters[side][key] = value
        self.refresh_comparison_paths()
        dpg.focus_item("compare_back")

    def refresh_comparison_paths(self, preferred_path: str | None = None) -> None:
        """Intersect both description filters without losing the source sequence."""
        preferred_path = preferred_path or self.preview_path or self.compare_last_path
        if self.preview_path is not None and preferred_path == self.preview_path:
            self.compare_last_path = self.preview_path
        conditions = []
        parameters = []
        for filters in self.compare_filters:
            terms = ["d.hash = f.hash", "d.status = 'described'"]
            for key, _label in COMPARE_PARAMETERS:
                value = filters[key]
                if value == self.compare_any:
                    continue
                if key == "effort_level" and value == self.compare_default_effort:
                    terms.append("d.effort_level IS NULL")
                else:
                    terms.append(f"d.{key} = ?")
                    parameters.append(value)
            conditions.append(
                "EXISTS (SELECT 1 FROM descriptions d WHERE "
                + " AND ".join(terms)
                + ")"
            )
        matching = {
            row[0]
            for row in self.con.execute(
                f"SELECT f.path FROM ({indexed_files_sql(self.con)}) f WHERE "
                + " AND ".join(conditions),
                parameters,
            )
        }
        self.compare_paths = [
            path for path in self.compare_source_paths if path in matching
        ]
        self.compare_index = (
            self.compare_paths.index(preferred_path)
            if preferred_path in self.compare_paths
            else 0
        )
        if self.compare_paths:
            path = self.compare_paths[self.compare_index]
            if path != self.preview_path:
                if self.show(path):
                    return
                self.clear_preview(keep_comparison=True)
        else:
            self.clear_preview(keep_comparison=True)
        self.render_comparison()

    def open_comparison(self, *_args: object, from_preview: bool = True) -> None:
        if self.comparing:
            return
        resume = not from_preview and bool(self.compare_source_paths)
        if resume:
            paths = self.compare_source_paths
        elif self.related_origin is not None:
            rows = related_files(
                self.con, self.preview_hash, identical=self.related_identical
            )
            paths, _hidden = current_result_paths([(row[0], row[1]) for row in rows])
        elif self.mode == "search":
            paths = self.paths
        else:
            paths = [
                file.path
                for file in filter_collection(
                    self.collection or [],
                    dpg.get_value("query"),
                    self.collection_filter,
                )
            ]
        if not paths and self.preview_path is None:
            if self.collection is None:
                self.collection, self.collection_hidden = collection_files(
                    self.con, self.cfg["describe"]
                )
            paths = [file.path for file in self.collection]
        self.compare_origin = (
            self.preview_path,
            dpg.get_value("desc_variant"),
            dpg.get_y_scroll("preview_pane"),
            dpg.get_value("status"),
        )
        if not resume:
            self.compare_source_paths = list(
                dict.fromkeys(
                    [*paths, *([self.preview_path] if self.preview_path else [])]
                )
            )
        selected_path = self.compare_last_path if resume else self.preview_path
        if not any(self.compare_variants):
            self.compare_variants[0] = dpg.get_value("desc_variant")
        self.load_comparison_catalog()
        self.comparing = True
        dpg.set_value("view_mode", self.mode_labels["compare"])
        self.apply_layout()
        self.refresh_comparison_paths(selected_path)
        self.restore_scroll = {
            **(self.restore_scroll or {}),
            "compare_fields": self.compare_scroll if resume else 0,
        }
        dpg.focus_item("compare_back")

    def close_comparison(self, *_args: object, restore: bool = True) -> None:
        if not self.comparing:
            return
        origin = self.compare_origin
        if self.preview_path is not None:
            self.compare_last_path = self.preview_path
        self.compare_scroll = dpg.get_y_scroll("compare_fields")
        self.comparing = False
        self.compare_origin = None
        dpg.set_value("view_mode", self.mode_labels[self.mode])
        self.clear_comparison_image()
        self.apply_layout()
        if restore and origin is not None:
            path, variant, scroll, status = origin
            if path is not None and self.show(path):
                if variant in self.variants:
                    dpg.set_value("desc_variant", variant)
                    self.render_description(self.variants[variant])
                self.restore_scroll = {"preview_pane": scroll}
                dpg.set_value("status", status)
            else:
                self.clear_preview()
                if path is None:
                    dpg.set_value("status", status)
        dpg.focus_item("query")

    def step_comparison(self, delta: int) -> None:
        self.select_comparison(self.compare_index + delta)

    def select_comparison(self, idx: int) -> None:
        if not self.comparing or not self.compare_paths:
            return
        idx = min(len(self.compare_paths) - 1, max(0, idx))
        if idx == self.compare_index:
            return
        self.compare_index = idx
        self.compare_last_path = self.compare_paths[idx]
        if not self.show(self.compare_paths[idx]):
            self.clear_preview(keep_comparison=True)
            self.render_comparison()
        dpg.set_y_scroll("compare_fields", 0)

    def update_comparison_navigation(self) -> None:
        dpg.configure_item(
            "compare_previous",
            enabled=bool(self.compare_paths) and self.compare_index > 0,
        )
        dpg.configure_item(
            "compare_next", enabled=self.compare_index < len(self.compare_paths) - 1
        )
        dpg.set_value(
            "compare_position",
            f"{self.compare_index + 1 if self.compare_paths else 0} / {len(self.compare_paths)}",
        )

    def on_compare_variant(self, _sender: object, value: str, side: int) -> None:
        if value not in self.comparison_matches(side):
            return
        self.compare_variants[side] = value
        self.render_comparison_fields()
        dpg.configure_item(f"compare_missing_{side}", show=value not in self.variants)
        dpg.set_value(
            f"compare_missing_{side}",
            _("This meme has no description in the selected variant."),
        )
        self.update_comparison_generation(side)
        dpg.configure_item("compare_copy", enabled=self.can_copy_comparison())
        dpg.configure_item("compare_copy_all", enabled=self.can_copy_all_comparisons())
        dpg.focus_item("compare_back")

    def can_copy_comparison(self) -> bool:
        return (
            self.comparing
            and self.preview_path is not None
            and self.preview_hash is not None
            and all(
                label in self.compare_catalog and self.variants.get(label) is not None
                for label in self.compare_variants
            )
        )

    def copy_comparison(self, *_args: object) -> None:
        if not self.can_copy_comparison():
            dpg.set_value("status", _("Select two available descriptions to copy."))
            return
        data = self.comparison_data(
            self.preview_path,
            self.preview_hash,
            self.img_size,
            [self.variants[label] for label in self.compare_variants],
        )
        dpg.set_clipboard_text(json.dumps(data, ensure_ascii=False, indent=2))
        dpg.set_value("status", _("Copied the description comparison as JSON."))

    def comparison_data(
        self,
        path: str,
        digest: str,
        size: tuple[int, int],
        descriptions: list[str | None],
    ) -> dict:
        data = {
            "meme": {
                "path": self.relative_path(path),
                "sha256": digest,
                "width": size[0],
                "height": size[1],
            }
        }
        for side, label, raw in zip(
            ("left", "right"), self.compare_variants, descriptions
        ):
            try:
                description = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                description = raw
            data[side] = {
                **dict(
                    zip(
                        (key for key, _label in COMPARE_PARAMETERS),
                        self.compare_catalog[label],
                    )
                ),
                "description": description,
            }
        return data

    def can_copy_all_comparisons(self) -> bool:
        return (
            self.comparing
            and bool(self.compare_paths)
            and all(
                label in self.compare_catalog and label in self.comparison_matches(side)
                for side, label in enumerate(self.compare_variants)
            )
        )

    def copy_all_comparisons(self, *_args: object) -> None:
        if not self.can_copy_all_comparisons():
            dpg.set_value(
                "status", _("Select description variants and a non-empty meme list.")
            )
            return
        paths = self.compare_paths.copy()
        requested = set(paths)
        selected = [self.compare_catalog[label] for label in self.compare_variants]
        rows = {
            path: (digest, left, right)
            for path, digest, left, right in self.con.execute(
                f"SELECT f.path, f.hash, l.description_json, r.description_json "
                f"FROM ({indexed_files_sql(self.con)}) f "
                "JOIN descriptions l ON l.hash = f.hash "
                "AND l.model = ? AND l.prompt_version = ? AND l.effort_level IS ? "
                "AND l.status = 'described' AND l.description_json IS NOT NULL "
                "JOIN descriptions r ON r.hash = f.hash "
                "AND r.model = ? AND r.prompt_version = ? AND r.effort_level IS ? "
                "AND r.status = 'described' AND r.description_json IS NOT NULL",
                (*selected[0], *selected[1]),
            )
            if path in requested
        }
        comparisons = []
        for path in paths:
            if path not in rows:
                continue
            digest, left, right = rows[path]
            try:
                with open(path, "rb") as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                        continue
                    file.seek(0)
                    still = self.preview_image(path, digest)
                    with Image.open(file if still == path else still) as image:
                        size = image.size
            except (OSError, ValueError, RuntimeError):
                continue
            comparisons.append(self.comparison_data(path, digest, size, [left, right]))
        if not comparisons:
            dpg.set_value("status", _("No memes with both selected variants to copy."))
            return
        dpg.set_clipboard_text(
            json.dumps({"comparisons": comparisons}, ensure_ascii=False, indent=2)
        )
        dpg.set_value(
            "status",
            _(
                "Copied comparisons as JSON. Pairs: {pairs}; skipped memes: {skipped}."
            ).format(pairs=len(comparisons), skipped=len(paths) - len(comparisons)),
        )

    def render_comparison(self) -> None:
        self.clear_comparison_image()
        if self.current_tex is not None:
            dpg.add_image_button(
                self.current_tex,
                tag="compare_image",
                parent="compare_image_row",
                before="compare_image_info",
                callback=self.open_comparison_zoom,
            )
        dpg.set_value(
            "compare_path",
            dpg.get_value("preview_path")
            if self.current_tex is not None
            else _("No memes match both sides.")
            if not self.compare_paths
            else _("Cannot display the selected meme."),
        )
        dpg.configure_item("compare_image_hint", show=self.current_tex is not None)
        for side in range(2):
            self.update_comparison_choices(side)
        dpg.configure_item("compare_copy", enabled=self.can_copy_comparison())
        dpg.configure_item("compare_copy_all", enabled=self.can_copy_all_comparisons())
        self.compare_size = (0, 0)
        self.update_comparison_navigation()
        self.resize_comparison()
        self.restore_scroll = {**(self.restore_scroll or {}), "compare_fields": 0}

    def render_comparison_fields(self) -> None:
        scroll = dpg.get_y_scroll("compare_fields")
        dpg.delete_item("compare_fields", children_only=True)
        width = (
            dpg.get_available_content_region("compare_fields")[0]
            or dpg.get_item_rect_size("main")[0] - 16
        )
        wrap = max(1, int(width / 2) - 24)
        values = [
            dict(description_fields(self.variants[label]))
            if label in self.variants
            else {}
            for label in self.compare_variants
        ]
        keys = dict.fromkeys(key for fields in values for key in fields)
        with dpg.table(
            parent="compare_fields",
            header_row=False,
            width=-1,
            policy=dpg.mvTable_SizingStretchSame,
            borders_innerV=True,
            borders_innerH=True,
        ):
            dpg.add_table_column()
            dpg.add_table_column()
            for key in keys:
                with dpg.table_row():
                    for side, fields in enumerate(values):
                        with dpg.group():
                            if self.compare_variants[side] in self.variants:
                                dpg.add_text(key or _("Description"), color=KEY_COLOR)
                                dpg.add_text(
                                    fields.get(key, _("(missing field)")), wrap=wrap
                                )
                            else:
                                dpg.add_text("")
        self.restore_scroll = {
            **(self.restore_scroll or {}),
            "compare_fields": scroll,
        }

    def resize_comparison(self) -> None:
        main_width, height = dpg.get_item_rect_size("main")
        width = dpg.get_available_content_region("compare_fields")[0] or main_width - 16
        size = (width, height)
        if size == self.compare_size or not all(size):
            return
        self.compare_size = size
        width, height = size
        w, h = self.img_size
        if w and h and dpg.does_item_exist("compare_image"):
            scale = min((width - 32) / w, min(140, height / 4) / h, 1.0)
            image_width = max(1, int(w * scale))
            dpg.configure_item(
                "compare_image", width=image_width, height=max(1, int(h * scale))
            )
            for tag in ("compare_path", "compare_image_hint"):
                dpg.configure_item(tag, wrap=max(1, width - image_width - 48))
        for side in range(2):
            dpg.configure_item(f"compare_missing_{side}", wrap=max(1, width // 2 - 24))
            dpg.configure_item(
                f"compare_match_count_{side}", wrap=max(1, width // 2 - 24)
            )
            for key, _label in COMPARE_PARAMETERS:
                dpg.configure_item(
                    f"compare_filter_label_{side}_{key}", wrap=max(1, width // 6 - 16)
                )
        self.render_comparison_fields()

    def open_comparison_zoom(self, *_args: object) -> None:
        if not self.comparing or self.current_tex is None:
            return
        self.close_comparison_zoom()
        width, height = dpg.get_item_rect_size("main")
        window_w, window_h = max(1, width - 32), max(1, height - 32)
        w, h = self.img_size
        scale = min(max(1, window_w - 32) / w, max(1, window_h - 90) / h, 1.0)
        dpg.add_image(
            self.current_tex,
            tag="compare_zoom_image",
            parent="compare_zoom",
            before="compare_zoom_close",
            width=max(1, int(w * scale)),
            height=max(1, int(h * scale)),
        )
        dpg.configure_item(
            "compare_zoom", width=window_w, height=window_h, pos=(16, 16), show=True
        )

    def close_comparison_zoom(self, *_args: object) -> None:
        dpg.hide_item("compare_zoom")
        if dpg.does_item_exist("compare_zoom_image"):
            dpg.delete_item("compare_zoom_image")

    def clear_comparison_image(self) -> None:
        self.close_comparison_zoom()
        if dpg.does_item_exist("compare_image"):
            dpg.delete_item("compare_image")

    # -------------------------------------------------------------- preview

    def on_variant_change(self, _sender: object, app_data: str) -> None:
        self.render_description(self.variants.get(app_data))
        # The combo keeps nav focus after picking; hand the keyboard back.
        dpg.focus_item("query")

    def on_tab(self, *_args: object) -> None:
        if self.comparing:
            return
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

    def preview_image(self, path: str, digest: str) -> str:
        """The path itself for images; a cached first frame for videos."""
        if not common.is_video(path):
            return path
        cached = Path(self.frame_dir.name) / f"{digest}.png"
        if not cached.is_file():
            with tempfile.TemporaryDirectory(dir=self.frame_dir.name) as tmp:
                frame = common.extract_video_frames(Path(path), [0], Path(tmp))[0]
                frame.replace(cached)
        return str(cached)

    def description_frames(self, path: str, digest: str) -> list[tuple[str, float]]:
        """The frames describe sends to the model, extracted once per session."""
        if digest not in self.frame_sets:
            workdir = Path(self.frame_dir.name) / f"{digest}-frames"
            workdir.mkdir(exist_ok=True)
            images = describe_stage.prepare_description_images(Path(path), workdir)
            self.frame_sets[digest] = [(str(i.path), i.timestamp) for i in images]
        return self.frame_sets[digest]

    def step_frame(self, delta: int) -> None:
        if self.preview_path is None or self.preview_hash is None:
            return
        try:
            frames = self.description_frames(self.preview_path, self.preview_hash)
        except Exception as error:  # noqa: BLE001 - Pillow/FFmpeg failures vary
            dpg.set_value(
                "status", _("cannot extract frames: {error}").format(error=error)
            )
            return
        self.frame_index = (self.frame_index + delta) % len(frames)
        frame, timestamp = frames[self.frame_index]
        if not self.set_preview_texture(frame):
            dpg.set_value("status", _("cannot load frame: {frame}").format(frame=frame))
            return
        dpg.set_value(
            "frame_label",
            _("frame {n}/{total} · {seconds:.3f} s").format(
                n=self.frame_index + 1, total=len(frames), seconds=timestamp or 0
            ),
        )

    def set_preview_texture(self, image_path: str) -> bool:
        loaded = dpg.load_image(image_path)
        if loaded is None:
            return False
        w, h, _ch, data = loaded
        new_tex = dpg.add_static_texture(
            width=w, height=h, default_value=data, parent="tex_reg"
        )
        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")
        dpg.add_image(
            new_tex, tag="preview_img", parent="preview_pane", before="preview_path"
        )
        self.clear_comparison_image()
        if self.current_tex is not None:
            dpg.delete_item(self.current_tex)
        self.current_tex = new_tex
        self.img_size = (w, h)
        self.fit_image()
        if self.comparing:
            self.render_comparison()
        return True

    def show(self, path: str) -> bool:
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
            dpg.set_value(
                "status", _("cannot load: {path}: {error}").format(path=path, error=e)
            )
            return False
        if (digest,) not in indexed:
            dpg.set_value(
                "status",
                _("the file is gone or its content changed: {path}").format(path=path),
            )
            return False
        # load_image reports an unreadable file by returning None, not raising.
        try:
            loaded = dpg.load_image(self.preview_image(path, digest))
        except (OSError, RuntimeError) as error:
            print(f"preview frame failed: {path}: {error}", file=sys.stderr)
            loaded = None
        if loaded is None:
            # Keep descriptions and navigation available for unsupported images.
            dpg.set_value(
                "status",
                _("cannot load: {path} — showing a placeholder").format(path=path),
            )
            w, h = 420, 210
            data = [0.15, 0.15, 0.15, 1.0] * (w * h)
        else:
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
        image_info = f"{w} × {h} px" if loaded is not None else _("No image preview")
        if common.is_video(path):
            image_info += " · " + _("video")
        dpg.set_value("preview_path", f"{image_info} · {self.relative_path(path)}")
        dpg.show_item("preview_path")
        self.frame_index = 0
        dpg.set_value("frame_label", _("first frame"))
        dpg.configure_item("frame_nav", show=is_animation(path))
        dpg.configure_item(
            "copy_file",
            label=_("Copy video") if common.is_video(path) else _("Copy image"),
        )
        dpg.show_item("file_actions")
        group = related_files(self.con, digest, identical=False)
        available, _hidden = current_result_paths([(row[0], row[1]) for row in group])
        dpg.configure_item("preview_actions", show=len(available) > 1)
        self.img_size = (w, h)
        self.fit_image()

        self.load_preview_descriptions(preserve_selection=False)
        dpg.configure_item("compare_open", show=True)

        self.clear_comparison_image()
        if self.current_tex is not None:
            dpg.delete_item(self.current_tex)
        self.current_tex = new_tex
        if self.comparing:
            self.render_comparison()
        return True

    def load_preview_descriptions(self, *, preserve_selection: bool = True) -> None:
        previous = dpg.get_value("desc_variant") if preserve_selection else None
        rows = self.con.execute(
            """
            SELECT d.model, d.prompt_version, d.effort_level, d.description_json
            FROM descriptions d
            WHERE d.hash = ? AND d.status = 'described'
            ORDER BY d.created_at DESC
            """,
            (self.preview_hash,),
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
            previous
            if previous in self.variants
            else active_label
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

    def clear_preview(self, *, keep_comparison: bool = False) -> None:
        if self.comparing and not keep_comparison:
            self.close_comparison(restore=False)
        self.clear_comparison_image()
        self.preview_path = None
        self.preview_hash = None
        dpg.hide_item("preview_path")
        dpg.hide_item("frame_nav")
        dpg.hide_item("file_actions")
        dpg.hide_item("preview_actions")
        dpg.hide_item("compare_open")
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
        if self.comparing:
            self.resize_comparison()
            return
        pane_w = dpg.get_item_rect_size("preview_pane")[0]
        if not pane_w or pane_w == self.pane_w:
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
            self.poll_comparison_generation()
            self.maybe_search()
            self.resize_panes()
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
    i18n.add_language_argument(parser)


def run(args: argparse.Namespace, cfg: dict) -> None:
    language = getattr(args, "language", None) or cfg.get("gui", {}).get("language")
    try:
        i18n.set_language(language)
    except ValueError as error:
        sys.exit(f"memy gui: {error}")
    App(cfg).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage 5: search GUI (Dear PyGui)")
    add_arguments(parser)
    args = parser.parse_args(argv)
    run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
