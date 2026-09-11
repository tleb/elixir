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

"""Compare a capture against the current tree (R3B §3).

Byte-exact after exactly two normalizations; 0 diffs on any record is
the only pass. Report: per-stratum counts, the first 20 diffs (unified
diffs for text bodies, sha256 for binary). Exit 1 on any diff.

Refuses to run if the dependency versions drifted from meta.json
(Pygments/Jinja/falcon output is part of the surface), or if the
manifest changed since the capture (--bless re-freezes, and exists
only for manifest-generator changes — never to accept drift).

Intentional behavior changes go through known-divergences.json (a
manifest-sha-keyed list of expected-status overrides), reviewed like
code (R3B §3).
"""

import argparse
import base64
import difflib
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from t.equiv import common  # noqa: E402

KNOWN_DIVERGENCES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'known-divergences.json')


def load_divergences(manifest_sha):
    if not os.path.exists(KNOWN_DIVERGENCES):
        return {}
    with open(KNOWN_DIVERGENCES, encoding='utf-8') as f:
        entries = json.load(f)
    return {e['i']: e for e in entries if e.get('manifest_sha256') == manifest_sha}


def describe_diff(entry, expected, got):
    """One human-readable diff record"""
    d = {'i': entry['i'], 'note': entry.get('note', ''), 'p': entry['p'],
         'q': entry.get('q')}
    if expected['s'] != got['s']:
        d['status'] = [expected['s'], got['s']]
    if expected['h'] != got['h']:
        keys = set(expected['h']) | set(got['h'])
        d['headers'] = {k: [expected['h'].get(k), got['h'].get(k)]
                        for k in sorted(keys)
                        if expected['h'].get(k) != got['h'].get(k)}
    eb = base64.b64decode(expected['b'])
    gb = base64.b64decode(got['b'])
    if eb != gb:
        try:
            et, gt = eb.decode('utf-8'), gb.decode('utf-8')
            diff = list(difflib.unified_diff(
                et.splitlines(), gt.splitlines(),
                fromfile=f'expected i={entry["i"]}', tofile=f'got',
                lineterm='', n=1))
            d['body_diff'] = '\n'.join(diff[:60])
            if len(diff) > 60:
                d['body_diff'] += f'\n... ({len(diff) - 60} more diff lines)'
        except UnicodeDecodeError:
            d['body_sha256'] = [hashlib.sha256(eb).hexdigest(),
                                hashlib.sha256(gb).hexdigest()]
        d['body_len'] = [len(eb), len(gb)]
    return d


def run_replay(manifest_path, captures, proj_dir, max_diffs=20, report_path=None):
    if not os.path.isdir(captures) or not os.path.exists(os.path.join(captures, 'meta.json')):
        raise SystemExit(f'no frozen captures in {captures} — capture them from the '
                         'pinned old side first (the linux2tag ones are T-E2\'s, '
                         'after its old-side data rebuild)')
    meta = common.read_meta(captures)

    drift = common.check_versions_match(meta)
    if drift:
        raise SystemExit('refusing to replay: dependency versions differ from '
                         f'capture meta: {drift}')
    sha = common.manifest_sha256(manifest_path)
    if sha != meta['manifest_sha256']:
        raise SystemExit(f'manifest sha256 drift: capture has '
                         f'{meta["manifest_sha256"][:12]}, current {sha[:12]}. '
                         'If the generator changed on purpose, re-freeze with '
                         'capture --force (never to accept implementation drift).')

    entries = {e['i']: e for e in common.read_manifest(manifest_path)}
    divergences = load_divergences(sha)

    client = common.make_client(proj_dir)
    strata = {}
    diffs = []
    n = 0
    t0 = time.monotonic()
    for rec in common.iter_records(captures):
        entry = entries[rec['i']]
        got = common.fetch(client, entry)
        div = divergences.get(entry['i'])
        if div and 'expected_status' in div:
            got = {**got, 's': div['expected_status']}
        stratum = entry.get('note', '?').split('/')[0]
        ok = strata.setdefault(stratum, [0, 0])
        if got == rec:
            ok[0] += 1
        else:
            ok[1] += 1
            if len(diffs) < max_diffs:
                diffs.append(describe_diff(entry, rec, got))
        n += 1

    wall = time.monotonic() - t0
    total_diff = sum(d for _o, d in strata.values())
    report = {
        'manifest': os.path.relpath(manifest_path, common.REPO_ROOT),
        'manifest_sha256': sha,
        'captures': captures,
        'proj_dir': os.path.abspath(proj_dir),
        'requests': n,
        'diffs': total_diff,
        'wall_seconds': round(wall, 1),
        'strata': {k: {'ok': o, 'diff': d} for k, (o, d) in sorted(strata.items())},
        'first_diffs': diffs,
    }
    if report_path:
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=1, sort_keys=True)
            f.write('\n')

    print(f'replayed {n} requests in {wall:.1f}s: '
          f'{n - total_diff} ok, {total_diff} diff')
    for k, (o, d) in sorted(strata.items()):
        print(f'  {k:22s} {o:6d} ok, {d} diff')
    for d in diffs:
        print(json.dumps(d, indent=1)[:2000])
    if total_diff:
        print(f'FAIL: {total_diff} diffs', file=sys.stderr)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--captures', required=True)
    ap.add_argument('--proj-dir', required=True,
                    help='LXR_PROJ_DIR of the side under test')
    ap.add_argument('--max-diffs', type=int, default=20)
    ap.add_argument('--report', help='report.json path '
                    '(default <captures>/report.json)')
    ap.add_argument('--bless', action='store_true',
                    help='re-freeze the captures instead of comparing '
                    '(manifest-generator changes ONLY)')
    args = ap.parse_args()

    if args.bless:
        print('bless: re-freezing captures — legitimate only when the '
              'manifest generator changed, never to accept drift',
              file=sys.stderr)
        from t.equiv import capture as capture_mod
        meta = common.read_meta(args.captures)
        capture_mod.run_capture(args.manifest, args.captures,
                                meta['proj_dir'],
                                workers=meta.get('workers', 1), force=True)
        return 0

    report = run_replay(args.manifest, args.captures, args.proj_dir,
                        max_diffs=args.max_diffs,
                        report_path=args.report
                        or os.path.join(args.captures, 'report.json'))
    return 1 if report['diffs'] else 0


if __name__ == '__main__':
    sys.exit(main())
