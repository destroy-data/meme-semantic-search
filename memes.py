"""Aggregate CLI over every pipeline stage: `memes.py <stage> [flags]`.

`all` runs scan, describe and embed in order. Other commands run only their
selected stage. Stage modules also stay runnable on their own.
"""

import argparse

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


def run_all(args: argparse.Namespace, cfg: dict) -> None:
    """Run each stage with the same flags it accepts when run alone."""
    print("== scan ==")
    scan.run(args, cfg)
    print("== describe ==")
    describe.run(args, cfg)
    print("== embed ==")
    embed.run(args, cfg)


def run_gui(args: argparse.Namespace, cfg: dict) -> None:
    # Imported lazily so the headless stages never pay for (or trip over) dearpygui.
    import gui

    gui.run(args, cfg)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memes", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)

    all_parser = sub.add_parser(
        "all",
        help="run scan, describe and embed in order",
        description="Run scan, describe and embed in order. Incremental and resumable.",
    )
    common.add_config_argument(all_parser)
    all_parser.add_argument(
        "--limit", type=int, default=0, help="cap describe and embed at N memes each"
    )
    all_parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-pick rows with status=failed_describe",
    )
    all_parser.add_argument(
        "--regroup-only",
        action="store_true",
        help="skip ingest, only recompute perceptual duplicate groups",
    )
    all_parser.add_argument(
        "--prune-missing",
        action="store_true",
        help="also remove missing or replaced paths outside image_dir; keep descriptions",
    )
    selection = all_parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--path",
        nargs="+",
        metavar="FILE",
        help="describe these files only; replaces existing descriptions for the same config",
    )
    selection.add_argument(
        "--undescribed-only",
        action="store_true",
        help="only describe memes without any successful description",
    )
    all_parser.add_argument(
        "--any-effort",
        action="store_true",
        help="skip descriptions under the same model and prompt at any effort level",
    )
    all_parser.add_argument(
        "--model", help="override [describe].model for describing and embedding"
    )
    all_parser.add_argument("--effort", help="override [describe].effort_level")
    all_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="print each description as it comes back",
    )
    all_parser.set_defaults(run=run_all)

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
