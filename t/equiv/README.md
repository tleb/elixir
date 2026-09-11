# Equivalence harness (R3B)

Byte-exact before/after HTTP comparison for the DuckDB migration.

Layout (R3B §2):

    common.py       env pinning, falcon TestClient + RAW_URI extras,
                    record/meta writers, zstd-or-gzip compression
    normalize.py    the exactly-two normalizations (Date header,
                    error-page timestamp)
    gen_manifest.py tiered manifest generator (seed 42, byte-stable)
    capture.py      record responses (+ N parallel workers)
    replay.py       compare against captures, report, exit 1 on any diff
    gate.sh         quick (pre-merge) and full (nightly) entries
    manifests/      committed manifests (musl-A ~1.5k, linux2tag-B ~17k)
    known-divergences.json
                    expected-status overrides for intentional behavior
                    changes — reviewed like code, never for drift

Captures land outside the repo, under ../elixir-data-acceptance/equiv/
(default), keyed by manifest stem. The go-forward captures (musl-A,
musl-B) were frozen from the post-cutover commit, serving the DuckDB
bootstrap ../equiv-bootstrap/musl-ddb; every gate replays the current
tree against them. The pre-cutover capture sets (captured from the
BDB old side) are kept beside them with a .bdb-era suffix, and the
linux2tag-B captures + linux2tag-old bootstrap remain on disk — both
are the migration's frozen evidence; the tree no longer ships the
engine that served them. `replay --bless` re-freezes captures, and
exists only for manifest-generator changes.

Bootstrap proj-dirs (symlinks to the frozen assets) live under
../equiv-bootstrap/; see R3B §5 for the pairing rules (repo HEAD +
tags sha256 recorded in meta.json, data-dir dump md5 recomputed
read-only at capture time).
