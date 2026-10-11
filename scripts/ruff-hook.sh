#!/usr/bin/env bash
set -u

cd /home/marcin/Projekty/memy || exit 1

check_output=$(ruff check --fix . 2>&1)
check_status=$?

format_output=$(ruff format . 2>&1)
format_status=$?

if (( check_status != 0 || format_status != 0 )); then
    printf 'Popraw pozostałe błędy Ruffa:\n%s\n%s\n' \
        "$check_output" "$format_output" >&2
    exit 2
fi
