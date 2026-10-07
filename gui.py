"""Stage 5: Dear PyGui GUI over stage 4 search."""

import argparse
import json
import os
import shutil
import subprocess
import time

import dearpygui.dearpygui as dpg

import common
import search as search_stage


def variant_label(model: str, prompt_version: str, effort_level: str | None) -> str:
    suffix = f" / {effort_level}" if effort_level else ""
    return f"{model} @ {prompt_version}{suffix}"


KEY_COLOR = (255, 215, 80, 255)
HINT_COLOR = (140, 140, 140, 255)

DEBOUNCE_S = 0.3  # idle time after the last keystroke before we search
PAGE_STEP = 10  # results skipped by PageUp/PageDown
TOP_N_DEFAULT = 50
TOP_N_STEP = 10
TOP_N_RANGE = (10, 100)
ROW_H_FALLBACK = 21  # used until a rendered row can be measured

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
    except Exception as e:
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
        self.img_size: tuple[int, int] = (0, 0)
        self.variants: dict[str, str | None] = {}
        self.top_n = TOP_N_DEFAULT
        self.row_h = ROW_H_FALLBACK
        self.pending_copy: tuple[str, str] | None = None  # (kind, path)
        self.search_at: float | None = None  # debounce deadline, None = idle
        self.last_query = ""
        self.pane_w = 0
        self.show_desc = True
        self.fullscreen = False

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        dpg.create_context()

        with dpg.font_registry():
            font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
            if font_path:
                f = dpg.add_font(font_path, 16)
                dpg.bind_font(f)

        dpg.add_texture_registry(tag="tex_reg")
        self.bind_keys()

        with dpg.window(tag="main", no_title_bar=True, no_resize=True, no_move=True):
            dpg.add_input_text(
                tag="query",
                hint="opisz mem…",
                width=-1,
                callback=self.on_query_change,
            )

            with dpg.group(horizontal=True):
                with dpg.child_window(
                    tag="list_pane", width=380, autosize_y=True, border=False
                ):
                    pass
                with dpg.child_window(
                    tag="preview_pane",
                    autosize_x=True,
                    autosize_y=True,
                    border=False,
                ):
                    dpg.add_text("wpisz zapytanie", tag="preview_hint")
                    dpg.add_combo(
                        tag="desc_variant",
                        items=[],
                        callback=self.on_variant_change,
                        show=False,
                        width=-1,
                    )
                    dpg.add_group(tag="preview_desc", show=False)

            with dpg.group(horizontal=True):
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

    def bind_keys(self) -> None:
        """All shortcuts are global handlers: the query box owns the keyboard
        permanently (see keep_query_focused), so anything bound to a bare letter
        would be swallowed by typing. Letters therefore always need a modifier;
        arrows, PgUp/PgDn, Esc and F-keys are free because a single-line input
        does nothing with them. Plain Home/End are deliberately left to the text
        cursor — Ctrl+Home/End do the list jumps instead.
        """
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=lambda: self.step(1))
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=lambda: self.step(-1))
            dpg.add_key_press_handler(
                dpg.mvKey_Next, callback=lambda: self.step(PAGE_STEP)
            )
            dpg.add_key_press_handler(
                dpg.mvKey_Prior, callback=lambda: self.step(-PAGE_STEP)
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

    def maybe_search(self) -> None:
        if self.search_at is None or time.monotonic() < self.search_at:
            return
        self.search_at = None
        self.do_search()

    def do_search(self) -> None:
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
            paths = search_stage.search(self.con, self.cfg["embed"], query, self.top_n)
        except Exception as e:
            dpg.set_value("status", f"błąd: {e}")
            return

        self.last_query = query
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
            dpg.set_value("status", f"{len(paths)} wyników (z {self.top_n})")
        else:
            self.current_idx = -1
            dpg.set_value("status", "brak wyników")
            self.clear_preview()

    def bump_top_n(self, delta: int) -> None:
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

    def select_index(self, idx: int) -> None:
        if not (0 <= idx < len(self.paths)) or idx == self.current_idx:
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
        if dpg.is_item_shown("help"):
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
        if 0 <= self.current_idx < len(self.paths):
            return self.paths[self.current_idx]
        dpg.set_value("status", "nie ma czego skopiować")
        return None

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
        path = self.current_path()
        if path:
            dpg.set_value("status", open_externally(path))

    # ---------------------------------------------------------- view toggles

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
        dpg.configure_item("list_pane", show=not self.fullscreen)
        has_desc = bool(self.variants) and self.show_desc
        dpg.configure_item("desc_variant", show=has_desc)
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
        avail_h = pane_h if self.fullscreen or not self.show_desc else pane_h * 0.6
        scale = min(pane_w / w, avail_h / h, 1.0)
        dpg.configure_item(
            "preview_img", width=max(1, int(w * scale)), height=max(1, int(h * scale))
        )

    def show(self, path: str) -> None:
        try:
            w, h, _ch, data = dpg.load_image(path)
        except Exception as e:
            dpg.set_value("status", f"nie mogę wczytać: {e}")
            return

        new_tex = dpg.add_static_texture(
            width=w, height=h, default_value=data, parent="tex_reg"
        )

        if dpg.does_item_exist("preview_hint"):
            dpg.delete_item("preview_hint")
        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")

        dpg.add_image(
            new_tex,
            tag="preview_img",
            parent="preview_pane",
            before="desc_variant",
        )
        self.img_size = (w, h)
        self.fit_image()

        rows = self.con.execute(
            """
            SELECT d.model, d.prompt_version, d.effort_level, d.description_json
            FROM memes m
            JOIN descriptions d ON d.hash = m.hash
            WHERE m.path = ? AND d.status = 'described'
            ORDER BY d.created_at DESC
            """,
            (path,),
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
        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")
        if not dpg.does_item_exist("preview_hint"):
            dpg.add_text(
                "wpisz zapytanie",
                tag="preview_hint",
                parent="preview_pane",
                before="desc_variant",
            )
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

    def keep_query_focused(self) -> None:
        """Type-anywhere: the query box owns the keyboard unless another widget
        is actively being edited.

        Checks `active` (= being edited), not `focused`: pressing Enter drops the
        keyboard capture but leaves the box "focused", so a focused-based guard
        would never hand the keys back after a search.
        """
        if dpg.is_item_active("query"):
            return
        # The combo's popup is a separate ImGui window we can't inspect, so back
        # off while it merely holds focus too - stealing would close it.
        if dpg.is_item_focused("desc_variant") or dpg.is_item_active("desc_variant"):
            return
        dpg.focus_item("query")

    def handle_resize(self) -> None:
        """Refit the image and re-wrap the description when the pane changes
        width (window resize, fullscreen toggle) - neither adapts on its own."""
        pane_w = dpg.get_item_rect_size("preview_pane")[0]
        if not pane_w or abs(pane_w - self.pane_w) < 20:
            return
        self.pane_w = pane_w
        self.fit_image()
        if self.variants:
            self.render_description(self.current_raw)

    def run(self) -> None:
        self.build()
        while dpg.is_dearpygui_running():
            self.keep_query_focused()
            self.maybe_search()
            self.handle_resize()
            dpg.render_dearpygui_frame()
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
