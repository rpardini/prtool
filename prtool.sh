#!/bin/sh
# Run prtool under uv from anywhere; the repo is taken from the current directory.
here="$(dirname "$(readlink -f "$0")")"
exec uv run --quiet --project "$here" "$here/prtool.py" "$@"
