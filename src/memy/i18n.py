"""User-interface translations: gettext catalogs loaded from .po files in memory.

English is the source language: messages in the code are English and need no
catalog. Other languages live in `locale/<lang>.po` next to this module. They
are parsed at startup and compiled to the .mo format in memory, so no compiled
files exist on disk and nothing can go stale. `scripts/update_translations.sh`
re-extracts messages and merges them into the catalogs.
"""

import argparse
import ast
import gettext
import io
import struct
from pathlib import Path

LOCALE_DIR = Path(__file__).parent / "locale"
DEFAULT_LANGUAGE = "en"

_translations: gettext.NullTranslations = gettext.NullTranslations()


def _(message: str) -> str:
    return _translations.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translations.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _translations.pgettext(context, message)


def N_(message: str) -> str:
    """Mark a message for extraction; translate it later, where it is shown."""
    return message


def available_languages() -> list[str]:
    return [DEFAULT_LANGUAGE, *sorted(p.stem for p in LOCALE_DIR.glob("*.po"))]


def set_language(language: str | None) -> None:
    """Switch every later lookup to `language`; None or "en" means English."""
    global _translations
    if not language or language == DEFAULT_LANGUAGE:
        _translations = gettext.NullTranslations()
        return
    po = LOCALE_DIR / f"{language}.po"
    if not po.is_file():
        raise ValueError(
            f"no translation for language {language!r}; "
            f"available: {', '.join(available_languages())}"
        )
    catalog = parse_po(po.read_text(encoding="utf-8"))
    _translations = gettext.GNUTranslations(io.BytesIO(build_mo(catalog)))


def add_language_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--language",
        help="interface language (default: [gui].language from the config, "
        f"else {DEFAULT_LANGUAGE}); available: {', '.join(available_languages())}",
    )


def parse_po(text: str) -> dict[str, str]:
    """Return {msgid: msgstr} in the key format .mo files use.

    Context is joined to the msgid with \\x04 and plural forms with \\0, the
    same layout msgfmt writes. Fuzzy and untranslated entries are skipped, so
    gettext falls back to the English message for them.
    """
    catalog: dict[str, str] = {}
    entry: dict[str, str] = {}
    fuzzy = False
    field: str | None = None

    def finish() -> None:
        nonlocal entry, fuzzy, field
        if "msgid" in entry:
            plural = "msgid_plural" in entry
            forms = sorted(k for k in entry if k.startswith("msgstr["))
            values = [entry[k] for k in forms] if plural else [entry.get("msgstr", "")]
            header = entry["msgid"] == ""
            if values and all(values) and (header or not fuzzy):
                key = entry["msgid"]
                if plural:
                    key += "\0" + entry["msgid_plural"]
                if "msgctxt" in entry:
                    key = entry["msgctxt"] + "\x04" + key
                catalog[key] = "\0".join(values)
        entry, fuzzy, field = {}, False, None

    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                if "msgid" in entry:
                    finish()
                fuzzy = True
            continue
        if line.startswith('"'):
            if field is None:
                raise ValueError(f"line {number}: string outside an entry")
            entry[field] += _unquote(line, number)
            continue
        keyword, _sep, rest = line.partition(" ")
        if keyword in ("msgctxt", "msgid") and "msgid" in entry:
            finish()
        if keyword not in ("msgctxt", "msgid", "msgid_plural", "msgstr") and not (
            keyword.startswith("msgstr[") and keyword.endswith("]")
        ):
            raise ValueError(f"line {number}: unknown keyword {keyword!r}")
        field = keyword
        entry[field] = _unquote(rest.strip(), number)
    finish()
    if "" not in catalog:
        raise ValueError('catalog has no header entry (msgid "")')
    return catalog


def _unquote(token: str, number: int) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        raise ValueError(f"line {number}: expected a quoted string")
    return ast.literal_eval(token)


def build_mo(catalog: dict[str, str]) -> bytes:
    """Serialize a catalog in the GNU .mo format (little-endian, revision 0)."""
    keys = sorted(catalog, key=lambda k: k.encode())
    ids = strs = b""
    entries = []
    for key in keys:
        msgid, msgstr = key.encode(), catalog[key].encode()
        entries.append((len(msgid), len(ids), len(msgstr), len(strs)))
        ids += msgid + b"\0"
        strs += msgstr + b"\0"
    header_size = 7 * 4
    ids_start = header_size + 16 * len(keys)
    strs_start = ids_start + len(ids)
    key_table = b"".join(struct.pack("<II", n, ids_start + o) for n, o, _, _ in entries)
    value_table = b"".join(
        struct.pack("<II", n, strs_start + o) for _, _, n, o in entries
    )
    header = struct.pack(
        "<7I",
        0x950412DE,  # magic
        0,  # revision
        len(keys),
        header_size,  # offset of the msgid table
        header_size + 8 * len(keys),  # offset of the msgstr table
        0,  # hash table size: none, gettext reads every entry anyway
        0,
    )
    return header + key_table + value_table + ids + strs
