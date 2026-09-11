#!/bin/sh
# Equivalence-harness gates (R3B §6).
#
#   t/equiv/gate.sh quick — pre-merge, one command:
#       pytest t/ + musl Tier A replay (~1.5k requests, < 2 min)
#   t/equiv/gate.sh full — nightly:
#       quick + musl Tier B (sampled-exhaustive, ~154k requests)
#       (~10-15 min with --workers)
#
# Both replays run the current tree against the frozen captures at
# EQUIV_CAPTURES (default ../elixir-data-acceptance/equiv), serving the
# DuckDB bootstrap proj-dir (default ../equiv-bootstrap/musl-ddb).
# The pre-cutover captures (captured from the BDB old side) are kept
# beside them with a .bdb-era suffix, and the linux2tag-B captures +
# linux2tag-old bootstrap remain on disk as migration evidence — the
# tree no longer ships the engine that served them.
#
# This file is part of Elixir, a source code cross-referencer.
# SPDX-License-Identifier: AGPL-3.0-or-later

set -e

here=$(dirname "$0")
root=$(cd "$here/../.." && pwd)
PY=${PYTHON:-~/.venv/bin/python}
CAPTURES=${EQUIV_CAPTURES:-$(dirname "$root")/elixir-data-acceptance/equiv}
PROJ_DIR=${EQUIV_PROJ_DIR:-$(dirname "$root")/equiv-bootstrap/musl-ddb}
export PYTHONUNBUFFERED=1

case "$1" in
quick)
    "$PY" -m pytest "$root/t"
    "$PY" "$here/replay.py" \
        --manifest "$here/manifests/musl-A.jsonl" \
        --captures "$CAPTURES/musl-A" \
        --proj-dir "$PROJ_DIR"
    ;;
full)
    "$0" quick
    # Tier B manifests are deterministic (seed 42); musl-B is generated
    # on demand (209k+ requests do not belong in git).
    if [ ! -f "$CAPTURES/manifests/musl-B.jsonl" ]; then
        mkdir -p "$CAPTURES/manifests"
        "$PY" "$here/gen_manifest.py" --project musl --tier B \
            --out "$CAPTURES/manifests/musl-B.jsonl"
    fi
    "$PY" "$here/replay.py" \
        --manifest "$CAPTURES/manifests/musl-B.jsonl" \
        --captures "$CAPTURES/musl-B" \
        --proj-dir "$PROJ_DIR" \
        --workers 8
    ;;
*)
    echo "usage: $0 {quick|full}" >&2
    exit 2
    ;;
esac
