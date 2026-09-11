#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2025 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#  and contributors.
#
#  Elixir is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Elixir is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record every manifest response through the in-process client (R3B §2).

Writes <out>/responses.jsonl.{zst,gz} plus meta.json with the full
provenance: elixir commit, ELIXIR_VERSION pin, project repo HEAD +
tags hash, the data-dir canonical dump md5 (recomputed read-only with
data_duckdb.canonical_dump), dependency versions, wall time.

Parallel capture: --workers N spawns N processes, each with its own
app and read-only database connection (concurrent readers are fine,
measured). Records always land in the file in manifest order.

Run under the tree whose behavior you want to freeze; the old side is
the pinned worktree of the pre-migration commit (R3B §2).
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from t.equiv import common  # noqa: E402


def worker(chunk, proj_dir, part_path):
    """One process: own client, own read-only database connection"""
    client = common.make_client(proj_dir)
    w, _kind = common.open_writer(part_path)
    try:
        for entry in chunk:
            w.write((json.dumps(common.fetch(client, entry), separators=(',', ':')) + '\n').encode())
    finally:
        w.close()


def run_capture(manifest_path, out, proj_dir, workers=1, dump_hash=True, force=False,
                project=None):
    entries = common.read_manifest(manifest_path)
    os.makedirs(out, exist_ok=True)
    rec_path = os.path.join(out, 'responses.jsonl')
    have = [p for p in (rec_path + '.zst', rec_path + '.gz') if os.path.exists(p)]
    if have and not force:
        raise SystemExit(f'{have[0]} exists (use --force to overwrite)')

    t0 = time.monotonic()
    meta = {
        'manifest': os.path.relpath(manifest_path, common.REPO_ROOT),
        'manifest_sha256': common.manifest_sha256(manifest_path),
        'requests': len(entries),
        'project': project or _project_of(manifest_path),
        'proj_dir': os.path.abspath(proj_dir),
        'ELIXIR_VERSION': os.environ.get('ELIXIR_VERSION', common.ELIXIR_VERSION_PIN),
        'elixir': common.elixir_provenance(),
        'compression': common.compression_kind() or 'gzip',
        'workers': workers,
    }

    # provenance of the project side: repo HEAD + tags, data-dir dump hash
    project = meta['project']
    repo_dir = os.path.join(proj_dir, project, 'repo')
    data_dir = os.path.join(proj_dir, project, 'data')
    meta['repo'] = common.repo_provenance(repo_dir)
    if dump_hash:
        print('computing data-dir dump md5 (read-only) ...', file=sys.stderr)
        t1 = time.monotonic()
        meta['data_dump_md5'] = common.data_dir_dump_md5(data_dir)
        print(f'  md5 {meta["data_dump_md5"]} in {time.monotonic()-t1:.1f}s',
              file=sys.stderr)

    # capture, in manifest order, possibly through N worker processes
    if workers <= 1:
        worker(entries, proj_dir, rec_path + '.part')
        os.replace(_as_compressed(rec_path + '.part'), _as_compressed(rec_path))
        kind = common.compression_kind() or 'gzip'
    else:
        import multiprocessing as mp
        ctx = mp.get_context('fork')  # cheap; the client is built per child
        chunks = [entries[i::workers] for i in range(workers)]
        procs = []
        for n, chunk in enumerate(chunks):
            p = ctx.Process(target=worker, args=(chunk, proj_dir, f'{rec_path}.part{n}'))
            p.start()
            procs.append(p)
        for p in procs:
            p.join()
        rc = [p.exitcode for p in procs]
        if any(r != 0 for r in rc):
            raise SystemExit(f'workers failed: {rc}')
        # merge: chunks were round-robin (i::workers), interleave back
        merged = [None] * len(entries)
        for n, chunk in enumerate(chunks):
            part = _as_compressed(f'{rec_path}.part{n}')
            for e, rec in zip(chunk, common.iter_records_file(part), strict=True):
                merged[e['i']] = rec
            os.remove(part)
        kind = common.compression_kind() or 'gzip'
        w, _ = common.open_writer(rec_path)
        for rec in merged:
            w.write((json.dumps(rec, separators=(',', ':')) + '\n').encode())
        w.close()

    meta['wall_seconds'] = round(time.monotonic() - t0, 1)
    meta['versions'] = common.versions_info()
    common.write_meta(out, meta)

    # summary: status histogram
    hist = {}
    for rec in common.iter_records(out):
        hist[rec['s']] = hist.get(rec['s'], 0) + 1
    print(f'captured {len(entries)} requests -> {out} '
          f'({meta["wall_seconds"]}s, {kind})')
    for s in sorted(hist):
        print(f'  {s}: {hist[s]}')
    return meta


def _project_of(manifest_path):
    name = os.path.basename(manifest_path).rsplit('-', 1)[0]
    if name == 'linux2tag':
        return 'linux'
    return name


def _as_compressed(path):
    kind = common.compression_kind() or 'gzip'
    return path + ('.zst' if kind == 'zstd' else '.gz')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out', help='capture dir '
                    '(default ../elixir-data-acceptance/equiv/<manifest stem>)')
    ap.add_argument('--proj-dir', help='LXR_PROJ_DIR of the side to capture '
                    '(default ../equiv-bootstrap/<project>-ddb)')
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--no-dump-hash', action='store_true',
                    help='skip the data-dir dump md5 (debug iteration only)')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    stem = os.path.splitext(os.path.basename(args.manifest))[0]
    out = args.out or os.path.join(common.DEFAULT_CAPTURES_ROOT, stem)
    project = _project_of(args.manifest)
    proj_dir = args.proj_dir or common.SIDES[project]

    run_capture(args.manifest, out, proj_dir, workers=args.workers,
                dump_hash=not args.no_dump_hash, force=args.force)


if __name__ == '__main__':
    main()
