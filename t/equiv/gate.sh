#!/bin/sh
# Equivalence-harness gates (R3B §6).
#
#   t/equiv/gate.sh quick — pre-merge, one command:
#       pytest t/ + musl Tier A replay (~1.5k requests, < 2 min)
#   t/equiv/gate.sh full — nightly:
#       quick + musl Tier B (sampled-exhaustive) + linux2tag Tier B
#       (~30-60 min); replays against the frozen captures, so the old
#       side needs no BDB code in the tree.
#
# During the migration, point EQUIV_PROJ_DIR at the side under test
# (default: the frozen old-side bootstrap).
#
# This file is part of Elixir, a source code cross-referencer.
# SPDX-License-Identifier: AGPL-3.0-or-later

set -e

here=$(dirname "$0")
root=$(cd "$here/../.." && pwd)
PY=${PYTHON:-~/.venv/bin/python}
CAPTURES=${EQUIV_CAPTURES:-$(dirname "$root")/elixir-data-acceptance/equiv}
PROJ_DIR=${EQUIV_PROJ_DIR:-$(dirname "$root")/equiv-bootstrap/musl-old}
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
    # on demand (209k+ requests do not belong in git), linux2tag-B is
    # committed.
    if [ ! -f "$CAPTURES/manifests/musl-B.jsonl" ]; then
        mkdir -p "$CAPTURES/manifests"
        "$PY" "$here/gen_manifest.py" --project musl --tier B \
            --out "$CAPTURES/manifests/musl-B.jsonl"
    fi
    "$PY" "$here/replay.py" \
        --manifest "$CAPTURES/manifests/musl-B.jsonl" \
        --captures "$CAPTURES/musl-B" \
        --proj-dir "$PROJ_DIR"
    "$PY" "$here/replay.py" \
        --manifest "$here/manifests/linux2tag-B.jsonl" \
        --captures "$CAPTURES/linux2tag-B" \
        --proj-dir "$(dirname "$root")/equiv-bootstrap/linux2tag-old"
    ;;
*)
    echo "usage: $0 {quick|full}" >&2
    exit 2
    ;;
esac
