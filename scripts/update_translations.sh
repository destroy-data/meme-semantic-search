#!/usr/bin/env bash
# Re-extract GUI messages into locale/memy.pot and merge them into every
# locale/<lang>.po. Needs GNU gettext (xgettext, msgmerge). The program reads
# the .po files directly; no .mo files are compiled.
#
# New language: msginit -l <lang> -i src/memy/locale/memy.pot -o src/memy/locale/<lang>.po
# then rename the output to <lang>.po if msginit picked a longer name.
set -euo pipefail
cd "$(dirname "$0")/.."
locale=src/memy/locale
xgettext --language=Python --from-code=UTF-8 --add-comments=TRANSLATORS: \
    --keyword=_ --keyword=N_ --keyword=ngettext:1,2 --keyword=pgettext:1c,2 \
    --package-name=memy --sort-by-file --output="$locale/memy.pot" \
    src/memy/*.py
for po in "$locale"/*.po; do
    msgmerge --quiet --update --backup=none "$po" "$locale/memy.pot"
    msgfmt --check --statistics --output-file=/dev/null "$po"
done
