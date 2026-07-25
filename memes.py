"""Aggregate CLI over every pipeline stage: `memes.py <stage> [flags]`.

`index` runs the whole indexing pipeline (scan -> describe -> embed); the
individual stages are also available for running one in isolation. Each stage
module stays runnable on its own (`uv run describe.py ...`) — this file just
hangs their `add_arguments` / `run` pair off a subparser.
"""

import argparse
from pathlib import Path

import common
import describe
import embed
import scan
import search

STAGES = [
    ("scan", scan, "scan images into the DB and regroup near-duplicates"),
    ("describe", describe, "describe memes with a VLM"),
    ("embed", embed, "embed descriptions into the vector + FTS indexes"),
    ("search", search, "search memes (hybrid vec + FTS, RRF fusion)"),
]


def run_index(args: argparse.Namespace, cfg: dict) -> None:
    """The whole indexing pipeline, sharing one connection across stages.

    Every stage is idempotent and resumable, so this is also the incremental
    update path: re-run it after adding memes and only new work happens.
    """
    con = common.connect(cfg["db_path"], init_schema=True)
    print("== scan ==")
    scan.ingest(con, Path(cfg["image_dir"]).expanduser())
    scan.regroup(con, cfg["phash_threshold"])
    print("== describe ==")
    describe.describe_memes(
        con, cfg["describe"], retry_failed=args.retry_failed, limit=args.limit
    )
    print("== embed ==")
    embed.embed_descriptions(con, cfg["describe"], cfg["embed"], limit=args.limit)
    con.close()


def run_gui(args: argparse.Namespace, cfg: dict) -> None:
    # Imported lazily so the headless stages never pay for (or trip over) dearpygui.
    import gui

    gui.run(args, cfg)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memes", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)

    index_parser = sub.add_parser(
        "index",
        help="run the full indexing pipeline: scan -> describe -> embed",
        description="Run scan, describe and embed in order. Incremental and resumable.",
    )
    common.add_config_argument(index_parser)
    index_parser.add_argument(
        "--limit", type=int, default=0, help="cap describe and embed at N memes each"
    )
    index_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-pick rows with status=failed_describe",
    )
    index_parser.set_defaults(run=run_index)

    for name, module, help_text in STAGES:
        stage_parser = sub.add_parser(name, help=help_text, description=help_text)
        module.add_arguments(stage_parser)
        stage_parser.set_defaults(run=module.run)

    gui_parser = sub.add_parser("gui", help="Dear PyGui frontend over search")
    common.add_config_argument(gui_parser)
    gui_parser.set_defaults(run=run_gui)

    args = parser.parse_args(argv)
    args.run(args, common.load_config(args.config))


if __name__ == "__main__":
    main()
