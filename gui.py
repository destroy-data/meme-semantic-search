"""Stage 5: Dear PyGui GUI over stage 4 search."""

import argparse
import json
import os

import dearpygui.dearpygui as dpg

import common
import search as search_stage


def variant_label(model: str, prompt_version: str, effort_level: str | None) -> str:
    suffix = f" / {effort_level}" if effort_level else ""
    return f"{model} @ {prompt_version}{suffix}"


KEY_COLOR = (255, 215, 80, 255)


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
        self.variants: dict[str, str | None] = {}

    def build(self) -> None:
        dpg.create_context()

        with dpg.font_registry():
            font_path = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)
            if font_path:
                f = dpg.add_font(font_path, 16)
                dpg.bind_font(f)

        dpg.add_texture_registry(tag="tex_reg")

        # Down/Up arrows step through results. Skip when the query box has focus
        # so arrow keys still move the text caret while typing.
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Down, callback=self.on_next)
            dpg.add_key_press_handler(dpg.mvKey_Up, callback=self.on_prev)

        with dpg.window(tag="main", no_title_bar=True, no_resize=True, no_move=True):
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    tag="query",
                    hint="opisz mem…",
                    width=-260,
                    on_enter=True,
                    callback=self.on_search,
                )
                dpg.add_input_int(
                    tag="n",
                    default_value=20,
                    min_value=1,
                    max_value=100,
                    min_clamped=True,
                    max_clamped=True,
                    width=100,
                )
                dpg.add_button(label="Szukaj", callback=self.on_search, width=100)

            with dpg.group(horizontal=True):
                with dpg.child_window(width=380, autosize_y=True, border=False):
                    dpg.add_listbox(
                        tag="results",
                        items=[],
                        num_items=30,
                        callback=self.on_select,
                        width=-1,
                    )
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

            dpg.add_text("", tag="status")

        dpg.create_viewport(title="mem search", width=1280, height=800)
        dpg.setup_dearpygui()
        dpg.set_primary_window("main", True)
        dpg.show_viewport()

    def on_search(self, sender: object = None, *_: object) -> None:
        query = dpg.get_value("query").strip()
        n = dpg.get_value("n")
        print(f"[dbg] on_search sender={sender!r} query={query!r} n={n}", flush=True)
        if not query:
            dpg.set_value("status", "wpisz coś w pole zapytania")
            return
        dpg.set_value("status", f"szukam „{query}”…")
        try:
            paths = search_stage.search(self.con, self.cfg["embed"], query, n)
        except Exception as e:
            print(f"[dbg] search error: {e}", flush=True)
            dpg.set_value("status", f"błąd: {e}")
            return
        print(f"[dbg] got {len(paths)} paths", flush=True)

        self.paths = paths
        items = [f"{i + 1:>3}. {os.path.basename(p)}" for i, p in enumerate(paths)]
        dpg.configure_item("results", items=items)
        if paths:
            self.current_idx = 0
            dpg.set_value("results", items[0])
            self.show(paths[0])
            dpg.set_value("status", f"{len(paths)} wyników")
        else:
            self.current_idx = -1
            dpg.set_value("status", "brak wyników")
            self.clear_preview()

    def on_variant_change(self, _sender: object, app_data: str) -> None:
        self.render_description(self.variants.get(app_data))

    def render_description(self, raw: str | None) -> None:
        dpg.delete_item("preview_desc", children_only=True)
        pane_w = dpg.get_item_rect_size("preview_pane")[0] or 800
        wrap_w = max(200, pane_w - 20)
        for key, value in description_fields(raw):
            if key is not None:
                dpg.add_text(f"{key}:", parent="preview_desc", color=KEY_COLOR)
                dpg.add_text(value, parent="preview_desc", wrap=wrap_w, indent=16)
            else:
                dpg.add_text(value, parent="preview_desc", wrap=wrap_w)
        dpg.configure_item("preview_desc", show=True)

    def on_select(self, _sender: object, app_data: str) -> None:
        # Listbox callback gives us the visible label; we prefixed with "  N. ".
        try:
            idx = int(app_data.split(".", 1)[0].strip()) - 1
        except (ValueError, AttributeError):
            return
        if 0 <= idx < len(self.paths):
            self.current_idx = idx
            self.show(self.paths[idx])

    def select_index(self, idx: int) -> None:
        if not (0 <= idx < len(self.paths)):
            return
        self.current_idx = idx
        dpg.set_value("results", f"{idx + 1:>3}. {os.path.basename(self.paths[idx])}")
        self.show(self.paths[idx])

    def on_next(self, *_: object) -> None:
        # Let arrow keys drive the text caret while the query box is focused.
        if dpg.is_item_focused("query"):
            return
        self.select_index(self.current_idx + 1)

    def on_prev(self, *_: object) -> None:
        if dpg.is_item_focused("query"):
            return
        self.select_index(self.current_idx - 1)

    def show(self, path: str) -> None:
        try:
            w, h, _ch, data = dpg.load_image(path)
        except Exception as e:
            dpg.set_value("status", f"nie mogę wczytać: {e}")
            return

        new_tex = dpg.add_static_texture(
            width=w, height=h, default_value=data, parent="tex_reg"
        )

        pane_w, pane_h = dpg.get_item_rect_size("preview_pane") or (800, 700)
        scale = min(pane_w / w, pane_h / h, 1.0) if w and h else 1.0
        disp_w, disp_h = max(1, int(w * scale)), max(1, int(h * scale))

        if dpg.does_item_exist("preview_hint"):
            dpg.delete_item("preview_hint")
        if dpg.does_item_exist("preview_img"):
            dpg.delete_item("preview_img")

        dpg.add_image(
            new_tex,
            tag="preview_img",
            parent="preview_pane",
            width=disp_w,
            height=disp_h,
            before="desc_variant",
        )

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
            "desc_variant", items=labels, default_value=selected, show=bool(labels)
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
                "brak wyników",
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
        if self.current_tex is not None:
            dpg.delete_item(self.current_tex)
            self.current_tex = None

    def run(self) -> None:
        self.build()
        dpg.start_dearpygui()
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
