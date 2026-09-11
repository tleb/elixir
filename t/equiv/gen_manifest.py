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
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Manifest generator of the t/equiv harness (R3B §2, §4).

Tiers: A (quick, ~1.5k), B (nightly sample; linux2tag ~15-25k per R3B
§4 strata), C (musl fully exhaustive, the one-off sign-off).

Generation needs only the project repository (tags, ls-tree sizes) and
the frozen canonical dump of the database (idents, families, blob
ids) — never a live data dir. With --seed 42 (the default) the output
is byte-stable, so manifests are committed and their sha256 recorded
in every capture's meta.json.

Sampling is coverage, not estimation: strata hit code paths (family
logic, version boundaries, truncation, charset), any single diff
fails the gate.

Compats note: comps keys are stored parse.quote()d (the B-family
lookup quotes the ident again), so a comps key doubles as the URL
segment verbatim, and unquoting it gives the /acp q= prefix form.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
from array import array
from urllib.parse import quote, unquote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from elixir.lib import getFileFamily  # noqa: E402

# Data sources of the frozen old side, relative to the elixir repo
PROJECTS = {
    'musl': {
        'project': 'musl',
        'repo': '../elixir-data/musl/repo',
        'dump': '../elixir-data/baselines/musl-baseline0.dump',
    },
    'linux2tag': {
        'project': 'linux',
        'repo': '../elixir-data-acceptance/linux/repo',
        'dump': '../elixir-data-acceptance/linux/before.dump',
    },
}

FAM_BITS = {'C': 1, 'K': 2, 'D': 4, 'M': 8}
_blob_id_re = re.compile(rb'\d+')


class Strata:
    """Everything the generator needs from the frozen database, parsed
    from one canonical dump (sorted `dbname\\thexkey\\thexvalue` lines)"""

    def __init__(self, dump_path):
        self.vers = {}        # tag -> {blob_id: path}, PathList order
        self.def_fams = {}    # ident(bytes) -> family bitmask
        self.def_blobs = {}   # ident(bytes) -> array('I') of blob ids
        self.ref_blobs = {}   # ident(bytes) -> array('I') of blob ids
        self.ref_count = {}   # ident(bytes) -> reference-line count
        self.docs = set()     # idents(bytes) with doc comments
        self.comps = []       # compatible keys(bytes), dump (byte) order
        self._parse(dump_path)
        # blob-id sets per tag, for existence tests
        self.tag_blobs = {t: set(paths) for t, paths in self.vers.items()}

    def _parse(self, path):
        handlers = {
            b'vers': self._vers, b'defs': self._defs, b'refs': self._refs,
            b'docs': self._docs, b'comps': self._comps,
        }
        handler = None
        cur = None
        with open(path, 'rb') as f:
            for line in f:
                name, tab, rest = line.partition(b'\t')
                if not tab:
                    continue
                if name != cur:
                    cur = name
                    handler = handlers.get(name)
                if handler is None:
                    continue
                keyhex, _, valhex = rest[:-1].partition(b'\t')
                handler(bytes.fromhex(keyhex.decode()), bytes.fromhex(valhex.decode()))

    def _vers(self, tag, val):
        d = {}
        for entry in val.split(b'\n')[:-1]:
            i, _, p = entry.partition(b' ')
            d[int(i)] = p.decode()
        self.vers[tag.decode()] = d

    def _defs(self, ident, val):
        entries, _, fams = val.partition(b'#')
        mask = 0
        for fam in fams.split(b','):
            if fam:
                mask |= FAM_BITS.get(fam.decode(), 0)
        self.def_fams[ident] = mask
        ids = array('I')
        for e in entries.split(b',') if entries else ():
            ids.append(int(_blob_id_re.match(e).group()))
        self.def_blobs[ident] = ids

    def _refs(self, ident, val):
        ids = array('I')
        n = 0
        for e in val.split(b'\n')[:-1]:
            bid, _, rest = e.partition(b':')
            ids.append(int(bid))
            n += rest.rsplit(b':', 1)[0].count(b',') + 1
        self.ref_blobs[ident] = ids
        self.ref_count[ident] = n

    def _docs(self, ident, _val):
        self.docs.add(ident)

    def _comps(self, key, _val):
        self.comps.append(key)

    # -- derived views ------------------------------------------------

    def idents_sorted(self):
        return sorted(set(self.def_fams) | set(self.ref_count))

    def def_idents_sorted(self):
        return sorted(self.def_fams)

    def hot_idents(self, n):
        by_refs = sorted(self.ref_count, key=lambda k: (-self.ref_count[k], k))
        return by_refs[:n]

    def rare_idents(self, n, rng):
        rare = [k for k in sorted(self.ref_count) if self.ref_count[k] == 1]
        return rng.sample(rare, min(n, len(rare)))

    def byte_edges(self):
        keys = self.def_idents_sorted()
        edges = [keys[0], keys[-1]]
        below_a = [k for k in keys if k < b'a']
        if below_a:
            edges.append(below_a[-1])  # the key just below 'a'
        return edges

    def kdm_only(self, cap):
        out = [k for k in self.def_idents_sorted()
               if self.def_fams[k] and not self.def_fams[k] & FAM_BITS['C']]
        return out[:cap]

    def in_tag(self, ident):
        """set of tags the ident occurs in (defs or refs)"""
        tags = set()
        for arr in (self.def_blobs.get(ident, ()), self.ref_blobs.get(ident, ())):
            for t, tb in self.tag_blobs.items():
                if not tb.isdisjoint(arr):
                    tags.add(t)
        return tags


def lstree(repo_dir, tag):
    """(mode, type, size, path) per entry, ls-tree -l -r order"""
    out = subprocess.run(['git', '-C', repo_dir, 'ls-tree', '-l', '-r', tag],
                         check=True, stdout=subprocess.PIPE).stdout
    entries = []
    for line in out.split(b'\n')[:-1]:
        meta, _, path = line.partition(b'\t')
        mode, type, _hash, size = meta.split()  # the size column is padded
        entries.append((mode.decode(), type.decode(),
                        int(size) if size != b'-' else None,
                        path.decode()))
    return entries


def git_tags(repo_dir, project):
    from elixir.repo import list_tags
    return [t.decode() for t in list_tags(repo_dir.encode(), project)]


# Requests that never terminate under the pinned pygments (2.18.0):
# DevicetreeLexer's 'statements' property lookahead
#   (?=(?:\s*,\s*[a-zA-Z_][\w-]*|(?:\s*(?:/[*][^*/]*?[*]/\s*)*))*\s*[=;])
# backtracks catastrophically on 'prop = <&phandle' followed by interleaved
# /* */ comment lines; web.py format_code renders .dts source pages through
# it, so the page never completes at any wall clock. There is no response
# to pin, so the request is dropped from manifests entirely (a divergence
# entry cannot express it). Screened exhaustively over the whole linux2tag
# Tier B manifest (T-E2, te2-screen-*.py batch subprocesses): exactly this
# one request hangs. Raw, tree and API URLs for the same path terminate
# exactly and stay in. Re-screen if pygments or the web lexer choice moves.
PATHOLOGICAL_REQUESTS = {
    '/linux/v4.2/source/arch/arm/boot/dts/at91-ariag25.dts',
}


class Manifest:
    def __init__(self):
        self.entries = []
        self._seen = set()

    def add(self, note, m, p, q=None, h=None, b=None):
        if p in PATHOLOGICAL_REQUESTS:
            return
        key = (m, p, q, b)
        if key in self._seen:  # same request from another stratum: skip
            return
        self._seen.add(key)
        e = {'m': m, 'p': p}
        if q is not None:
            e['q'] = q
        if h:
            e['h'] = h
        if b is not None:
            e['b'] = b
        e['note'] = note
        self.entries.append(e)

    def get(self, p, note, q=None, h=None):
        self.add(note, 'GET', p, q=q, h=h)

    def post(self, p, note, body):
        self.add(note, 'POST', p,
                 h={'Content-Type': 'application/x-www-form-urlencoded'},
                 b=body)

    def finalize(self):
        for i, e in enumerate(self.entries):
            e['i'] = i
        return self.entries

    def histogram(self):
        counts = {}
        for e in self.entries:
            stratum = e['note'].split('/')[0]
            counts[stratum] = counts.get(stratum, 0) + 1
        return dict(sorted(counts.items()))


JSON_ACCEPT = {'Accept': 'application/json'}


def qseg(s):
    """URL segment, quoted as the wire form"""
    return quote(s, safe='')


def source_url(project, v, path):
    return f'/{project}/{qseg(v)}/source{path}'


def ident_url(project, v, ident, family=None):
    fam = f'/{family}' if family else ''
    return f'/{project}/{qseg(v)}{fam}/ident/{qseg(ident)}'


def comp_url(project, v, quoted_key):
    """B-family ident URL: the segment is the stored (already quoted) key"""
    return f'/{project}/{qseg(v)}/B/ident/{quoted_key}'


def api_url(project, ident):
    return f'/api/ident/{project}/{qseg(ident)}'


def acp_q(q, p, f):
    return f'q={quote(q, safe="")}&p={p}&f={f}'


# ---------------------------------------------------------------------------
# Route-class and error-path coverage shared by every project (R3B §2)

def route_coverage(M, project, tags, files_by_family, ident_hot, comp_keys):
    """R1-R9 + E* + the pinned quirks. files_by_family: family -> [paths];
    ident_hot: one known ident (str); comp_keys: a few stored comps keys"""
    v_first, v_last = tags[0], tags[-1]
    some_file = (files_by_family.get('C') or files_by_family['none'])[0]
    some_dir = sorted({os.path.dirname('/' + f) for f in files_by_family['C']})[1]

    # R1 index (500 when the default 'linux' project is absent)
    M.get('/', 'r1-index')
    # R8 incomplete URLs
    for p in (f'/{project}', f'/{project}/{v_last}', f'/{project}/{v_last}/',
              f'/{project}/latest'):
        M.get(p, 'r8-redirect')
    # R3 trailing slash
    M.get(source_url(project, v_last, some_dir) + '/', 'r3-slash')
    # R9 unknown path resource
    M.get(f'/{project}/{v_last}/nosuchcmd', 'r9-unknown')
    M.get(f'/{project}/{v_last}/nosuchcmd/deeper', 'r9-unknown')

    # R5 ident form redirect
    M.get(f'/{project}/{v_last}/ident', 'r5-form', q=f'i={qseg(ident_hot)}&f=C')
    M.get(f'/{project}/{v_last}/ident', 'r5-form', q=f'i={qseg(ident_hot)}')
    M.get(f'/{project}/{v_last}/ident', 'r5-form', q='i=&f=C')
    M.get(f'/{project}/{v_last}/ident', 'r5-form')
    M.post(f'/{project}/{v_last}/ident', 'r5-form', f'i={qseg(ident_hot)}&f=C')
    M.post(f'/{project}/{v_last}/ident', 'r5-form', f'i={qseg(ident_hot)}&f=B')
    M.post(f'/{project}/{v_last}/ident', 'r5-form', 'i=&f=C')
    M.post(ident_url(project, v_last, ident_hot), 'r5-post-405', f'i={qseg(ident_hot)}')

    # R2 quirks: raw + missing file -> 400 (HTML variant -> 404)
    M.get(source_url(project, v_last, '/nosuchfile.c'), 'e-raw-missing-400', q='raw=1')
    M.get(source_url(project, v_last, '/nosuchfile.c'), 'e-file-missing-404')
    # charset of paths: allowed punctuation passes (404), unicode fails (400)
    M.get(source_url(project, v_last, '/x+y=z.c'), 'e-path-charset-ok')
    M.get(source_url(project, v_last, '/%C3%A9.c'), 'e-path-unicode-400')
    M.get(source_url(project, v_last, '/%22quoted%22.c'), 'e-path-charset-400')
    # bogus tag (git error -> empty type -> 404 error page)
    M.get(source_url(project, 'v9.9.9', ''), 'e-bogus-tag')
    M.get(source_url(project, 'v9.9.9', '/Makefile'), 'e-bogus-tag')
    # latest / latest-rc redirects
    M.get(source_url(project, 'latest', '/Makefile'), 'r2-latest-redirect')
    M.get(source_url(project, 'latest-rc', '/Makefile'), 'r2-latest-redirect')
    M.get(ident_url(project, 'latest', ident_hot), 'r4-latest-redirect')

    # E*: invalid project/version/ident
    M.get(f'/{project}%21/{v_last}/source', 'e-bad-project-400')
    M.get(f'/nosuchproj/{v_last}/source', 'e-unknown-project-404')
    M.get(f'/{project}/%21/source', 'e-bad-version-400')
    M.get(ident_url(project, v_last, '%C3%A9!!'), 'e-ident-unicode-400')
    M.get(ident_url(project, v_last, 'bad ident'), 'e-ident-space-400')
    # JSON error branch (Vary: Accept)
    M.get(source_url(project, v_last, '/nosuchfile.c'), 'e-json-branch', h=JSON_ACCEPT)
    M.get(f'/nosuchproj/{v_last}/source', 'e-json-branch', h=JSON_ACCEPT)
    M.get(ident_url(project, v_last, '%C3%A9!!'), 'e-json-branch', h=JSON_ACCEPT)

    # R6 autocomplete edges and errors
    M.get('/acp', 'r6-acp', q=acp_q('zzz', project, 'C'))
    M.get('/acp', 'r6-acp', q=acp_q('~', project, 'C'))
    M.get('/acp', 'r6-acp', q=acp_q('%FF', project, 'C'))
    M.get('/acp', 'r6-acp', q=acp_q(ident_hot[:2], project, 'J'))  # invalid -> C
    M.get('/acp', 'r6-acp', q=f'q={ident_hot[:1]}&p={project}')
    M.get('/acp', 'r6-acp', q='q=x&p=nosuchproj&f=C')  # 404
    M.get('/acp', 'r6-acp', q=f'p={project}&f=C')       # 400 missing q
    M.get('/acp', 'r6-acp', q='q=x&p=&f=C')            # 400 invalid project
    M.get('/acp', 'r6-acp', q=acp_q('%C3%A9', project, 'C'))  # 400 invalid ident

    # R7 API edges
    M.get(api_url(project, ident_hot), 'r7-api', q=f'version={v_last}')
    M.get(api_url(project, ident_hot), 'r7-api', q=f'version={v_last}&family=B')
    M.get(api_url(project, ident_hot), 'r7-api', q='version=latest')
    M.get(api_url(project, ident_hot), 'r7-api-missing-version-400')
    M.get(api_url(project, ident_hot), 'r7-api', q=f'version={v_first}')
    M.get(api_url(project, ident_hot), 'r7-api', q='version=v9.9.9')
    M.get('/api/ident/nosuchproj/x', 'r7-api-unknown-project-404', q=f'version={v_last}')
    M.get(api_url(project, '%C3%A9!!'), 'r7-api-ident-400', q=f'version={v_last}')

    # B family (compatibles) — /acp f=B is the pinned 500 where comps
    # is absent (non-DTS projects)
    if comp_keys:
        key = comp_keys[0].decode()
        M.get(comp_url(project, v_last, key), 'ident/compat')
        M.get('/acp', 'r6-acp', q=acp_q(unquote(key)[:4], project, 'B'))
    else:
        M.get(ident_url(project, v_last, ident_hot, 'B'), 'e-comps-missing')
    M.get('/acp', 'r6-acp-quirk-500', q=acp_q('x', project, 'B'))


# ---------------------------------------------------------------------------
# musl tiers

def gen_musl(M, s, repo, tier, rng):
    project = 'musl'
    tags = git_tags(repo, project)
    assert set(tags) <= set(s.vers), 'repo tags missing from the dump'
    v_first, v_last = tags[0], tags[-1]
    v_mid = tags[len(tags) // 2]
    vers_cycle = [v_first, v_mid, v_last, 'latest']

    # ---- files, stratified from the last tag's PathList ---------------
    last_files = sorted(s.vers[v_last].values())
    by_fam = {'C': [], 'K': [], 'D': [], 'M': [], 'none': []}
    for p in last_files:
        by_fam[getFileFamily(os.path.basename(p)) or 'none'].append(p)
    tree = lstree(repo, v_last)
    sizes = {p: sz for mode, typ, sz, p in tree if typ == 'blob'}
    empties = [p for mode, typ, sz, p in tree if typ == 'blob' and sz == 0]
    largest = sorted((p for mode, typ, sz, p in tree if typ == 'blob'),
                     key=lambda p: -sizes[p])[:5]

    ident_hot = s.hot_idents(60)
    route_coverage(M, project, tags, by_fam, ident_hot[0].decode(), [])

    # R2 trees across versions
    top_dirs = sorted({os.path.dirname(p) for p in last_files if os.path.dirname(p)})
    for v in (v_first, v_mid, v_last):
        M.get(source_url(project, v, ''), 'tree/root')
        for d in top_dirs[:6]:
            M.get(source_url(project, v, d), 'tree/top')
    M.get(source_url(project, v_last, top_dirs[-1]), 'tree/top')
    M.get(source_url(project, v_last, '/src/prng'), 'tree/deep')

    # R2 sources: family strata + boundaries, spread over versions
    def cycle_v(i):
        return vers_cycle[i % len(vers_cycle)]

    sample_c = rng.sample(by_fam['C'], min(45, len(by_fam['C'])))
    strata_files = (sample_c + by_fam['M'][:10] + by_fam['none'][:10]
                    + [last_files[0], last_files[-1]] + largest + empties[:5])
    for i, p in enumerate(strata_files):
        M.get(source_url(project, cycle_v(i), '/' + p), 'source/family')
    # the same file across eras, including a version where it is absent
    era_probe = sample_c[0]
    for v in (v_first, v_mid, v_last):
        if era_probe in s.vers[v].values():
            M.get(source_url(project, v, '/' + era_probe), 'source/eras')

    # R2 raw variants
    for p in ([sample_c[0], sample_c[1], by_fam['M'][0], largest[0], empties[0]]
              + [f for f in sample_c if f.endswith('.s')][:2]):
        M.get(source_url(project, v_last, '/' + p), 'source/raw', q='raw=1')
    M.get(source_url(project, v_last, '/' + sample_c[0]), 'source/raw0', q='raw=0')

    # ---- idents -------------------------------------------------------
    rare = s.rare_idents(80, rng)
    edges = s.byte_edges()
    rand_idents = rng.sample(s.def_idents_sorted(), 400)
    no_refs = [k for k in s.def_idents_sorted() if k not in s.ref_count][:40]

    selected = ident_hot + rare + edges + rand_idents + no_refs
    for i, ident in enumerate(selected):
        note = ('ident/hot' if ident in ident_hot else
                'ident/rare' if ident in rare else
                'ident/edge' if ident in edges else 'ident/varied')
        M.get(ident_url(project, cycle_v(i), ident.decode()), note)
    # family variants over a few hot idents (incl. the >100-symbols ones)
    for ident in ident_hot[:8]:
        for fam in ('A', 'C', 'D', 'K', 'M', 'B'):
            M.get(ident_url(project, v_last, ident.decode(), fam), 'ident/family')
    M.get(ident_url(project, v_last, 'nosuchident0'), 'ident/none')
    M.get(ident_url(project, 'v9.9.9', ident_hot[0].decode()), 'ident/bogus-version')

    # R6 autocomplete strata
    prefixes1 = sorted({k.decode()[:1] for k in s.def_fams})
    for pref in prefixes1:
        M.get('/acp', 'acp/prefix1', q=acp_q(pref, project, 'C'))
    for pref in rng.sample(sorted({k.decode()[:2] for k in s.def_fams}), 60):
        M.get('/acp', 'acp/prefix2', q=acp_q(pref, project, 'C'))
    for ident in rng.sample(s.def_idents_sorted(), 25):  # full-name prefix
        M.get('/acp', 'acp/full', q=acp_q(ident.decode(), project, 'C'))
    for ident in s.hot_idents(12):  # >10 truncation cases
        M.get('/acp', 'acp/trunc', q=acp_q(ident.decode()[:2], project, 'C'))

    # R7 API strata
    for i, ident in enumerate(selected[:600]):
        M.get(api_url(project, ident.decode()), 'api/ident', q=f'version={cycle_v(i)}')
    for ident in ident_hot[:6]:
        for fam in ('A', 'B', 'K', 'M'):
            M.get(api_url(project, ident.decode()), 'api/ident-family',
                  q=f'version={v_last}&family={fam}')

    if tier in ('B', 'C'):
        gen_musl_beyond_A(M, s, tags, tier, rng)


def gen_musl_beyond_A(M, s, tags, tier, rng):
    """Tier B: sampled-exhaustive (all idents x 8 sentinel versions via
    API, all trees x all versions, 10% stratified source pages); Tier C:
    fully exhaustive (all idents x all versions, all files). R3B §6."""
    project = 'musl'
    sentinels = [tags[0], tags[len(tags) * 1 // 8], tags[len(tags) * 2 // 8],
                 tags[len(tags) * 3 // 8], tags[len(tags) // 2],
                 tags[len(tags) * 5 // 8], tags[len(tags) * 3 // 4], tags[-1]]
    api_versions = tags if tier == 'C' else sentinels

    for ident in s.idents_sorted():
        for v in api_versions:
            M.get(api_url(project, ident.decode()), 'api/all', q=f'version={v}')

    # trees across every indexed version
    for v in tags:
        for d in sorted({os.path.dirname(p) for p in s.vers[v].values()}):
            M.get(source_url(project, v, d), 'tree/all')

    # stratified source pages: 10% (tier B) / 100% (tier C)
    for v in api_versions:
        files = sorted(s.vers[v].values())
        if tier != 'C':
            files = rng.sample(files, max(1, len(files) // 10))
        for p in files:
            M.get(source_url(project, v, '/' + p), 'source/all')


# ---------------------------------------------------------------------------
# linux2tag tier B (R3B §4 sampling at linux scale)

def gen_linux2tag(M, s, repo, tier, rng):
    assert tier == 'B', 'linux2tag only has a Tier B manifest (R3B §4)'
    project = 'linux'
    tags = git_tags(repo, project)
    assert set(tags) <= set(s.vers), 'repo tags missing from the dump'
    v_old, v_new = tags[0], tags[-1]

    tree_old = lstree(repo, v_old)
    tree_new = lstree(repo, v_new)
    paths_old = {p for _m, _t, _s, p in tree_old if _t == 'blob'}
    paths_new = {p for _m, _t, _s, p in tree_new if _t == 'blob'}
    sizes_new = {p: sz for m, t, sz, p in tree_new if t == 'blob'}
    symlinks_new = [p for m, t, _s, p in tree_new if m == '120000']
    empties_new = [p for m, t, sz, p in tree_new if t == 'blob' and sz == 0]

    by_fam_new = {'C': [], 'K': [], 'D': [], 'M': [], 'none': []}
    for p in sorted(paths_new):
        by_fam_new[getFileFamily(os.path.basename(p)) or 'none'].append(p)

    ident_hot = s.hot_idents(20)
    route_coverage(M, project, tags, by_fam_new, ident_hot[0].decode(),
                   s.comps[:5] or [])

    # ---- trees: every directory of the new tag, top dirs of the old ---
    for d in sorted({os.path.dirname(p) for p in paths_new}):
        M.get(source_url(project, v_new, d), 'tree/all')
    for d in sorted({p.split('/')[1] for p in paths_old if '/' in p[1:]}):
        if d:
            M.get(source_url(project, v_old, '/' + d), 'tree/old-top')

    # ---- files: family x size-decile strata + boundaries ---------------
    def decile_buckets(paths):
        out = [[] for _ in range(10)]
        for i, p in enumerate(paths):
            out[min(i * 10 // len(paths), 9)].append(p)
        return out
    for fam, paths in by_fam_new.items():
        if not paths:
            continue
        ordered = sorted(paths, key=lambda p: sizes_new.get(p, 0))
        per_bucket = 100 if fam in ('C', 'none') else 60
        for di, bucket in enumerate(decile_buckets(ordered)):
            for p in rng.sample(bucket, min(per_bucket, len(bucket))):
                M.get(source_url(project, v_new, '/' + p), f'file/{fam}-d{di}')
    # named lexer branches
    def take_named(pred, n):
        pool = [p for p in sorted(paths_new) if pred(p)]
        return rng.sample(pool, min(n, len(pool)))
    named = [p for p in sorted(paths_new) if p.endswith('.S')][:10]
    named += take_named(lambda p: os.path.basename(p).startswith('Kconfig'), 5)
    named += take_named(lambda p: p.endswith('.dts'), 5)
    named += take_named(lambda p: p.endswith('.dtsi'), 5)
    named += take_named(lambda p: os.path.basename(p).startswith('Makefile'), 5)
    named += take_named(lambda p: p.endswith('.rst'), 5)
    for p in named[:40]:
        M.get(source_url(project, v_new, '/' + p), 'file/lexer-branch')
    # boundaries: first/last path in ls-tree order per top dir
    for top in sorted({p.split('/')[1] for p in paths_new if p.count('/') >= 1}):
        under = sorted(p for p in paths_new if p.startswith(top + '/'))
        if under:
            M.get(source_url(project, v_new, '/' + under[0]), 'file/boundary')
            M.get(source_url(project, v_new, '/' + under[-1]), 'file/boundary')
    for p in sorted(paths_new, key=lambda p: -sizes_new.get(p, 0))[:5]:
        M.get(source_url(project, v_new, '/' + p), 'file/largest')
    for p in rng.sample(empties_new, min(20, len(empties_new))):
        M.get(source_url(project, v_new, '/' + p), 'file/empty')
    for p in rng.sample(symlinks_new, min(20, len(symlinks_new))):
        M.get(source_url(project, v_new, '/' + p), 'file/symlink')
    # files only in one of the two tags
    for p in rng.sample(sorted(paths_old - paths_new), min(300, len(paths_old - paths_new))):
        M.get(source_url(project, v_old, '/' + p), 'file/old-only')
    for p in rng.sample(sorted(paths_new - paths_old), min(300, len(paths_new - paths_old))):
        M.get(source_url(project, v_new, '/' + p), 'file/new-only')
    # a few old-tag pages of files present in both
    both = sorted(paths_old & paths_new)
    for p in rng.sample(both, min(500, len(both))):
        M.get(source_url(project, v_old, '/' + p), 'file/old-era')
    # raw variants
    for p in [both[0], both[len(both) // 2], both[-1]]:
        M.get(source_url(project, v_new, '/' + p), 'file/raw', q='raw=1')

    # ---- idents (~12k target with complements) -------------------------
    ref_sorted = sorted((k for k in s.ref_count if s.ref_count[k] > 0),
                        key=lambda k: s.ref_count[k])
    deciles = [[] for _ in range(10)]
    for i, k in enumerate(ref_sorted):
        deciles[min(i * 10 // len(ref_sorted), 9)].append(k)
    picked = []
    seen = set()

    def take(idents, note, cap=None):
        out = []
        for k in idents:
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
            if cap and len(out) >= cap:
                break
        picked.extend((k, note) for k in out)
        return out

    take(s.hot_idents(300), 'hot')                    # 300 hottest
    for di, bucket in enumerate(deciles):             # 300 across deciles
        take(rng.sample(bucket, min(30, len(bucket))), f'decile{di}')
    take(s.rare_idents(300, rng), 'rare')             # 300 random rare
    take(s.kdm_only(1500), 'kdm-only')                # family-compat paths
    take(rng.sample(sorted(s.comps), min(500, len(s.comps))), 'compat')
    take(s.byte_edges(), 'edge')                      # byte-order edges
    comp_commas = [k for k in s.comps if b',' in k]
    take(rng.sample(comp_commas, min(100, len(comp_commas))), 'compat-comma')

    # version-diff: exists in one tag, not the other
    old_blobs = s.tag_blobs[v_old]
    new_blobs = s.tag_blobs[v_new]
    only_old = []
    only_new = []
    for k in s.def_idents_sorted():
        hit_old = (not old_blobs.isdisjoint(s.def_blobs.get(k, ()))
                   or not old_blobs.isdisjoint(s.ref_blobs.get(k, ())))
        hit_new = (not new_blobs.isdisjoint(s.def_blobs.get(k, ()))
                   or not new_blobs.isdisjoint(s.ref_blobs.get(k, ())))
        if hit_old and not hit_new:
            only_old.append(k)
        elif hit_new and not hit_old:
            only_new.append(k)
    take(rng.sample(only_old, min(200, len(only_old))), 'version-diff-old')
    take(rng.sample(only_new, min(200, len(only_new))), 'version-diff-new')
    # defs-but-no-refs, refs-but-no-docs
    no_refs = [k for k in s.def_idents_sorted() if k not in s.ref_count]
    take(rng.sample(no_refs, min(200, len(no_refs))), 'defs-no-refs')
    no_docs = [k for k in sorted(s.ref_count) if k not in s.docs]
    take(rng.sample(no_docs, min(200, len(no_docs))), 'refs-no-docs')

    # ---- complements: idents defined in the sampled files --------------
    sample_paths = {e['p'].removeprefix(f'/{project}/{v_new}/source/')
                    for e in M.entries if e['note'].startswith('file/')}
    sample_ids = {i for i, p in s.vers[v_new].items() if p in sample_paths}
    comp_count = 0
    for ident in s.def_idents_sorted():
        if comp_count >= 3500:
            break
        if not sample_ids.isdisjoint(s.def_blobs.get(ident, ())):
            picked.append((ident, 'file-complement'))
            comp_count += 1

    # emit the ident requests
    for i, (ident, note) in enumerate(picked):
        if note == 'version-diff-old':
            v = v_old
        elif note == 'version-diff-new':
            v = v_new
        else:
            tags_in = s.in_tag(ident)
            v = (v_old, v_new)[i % 2] if tags_in else v_new
        if note in ('hot', 'edge') and i % 20 == 0:
            M.get(ident_url(project, v, ident.decode()), f'ident/{note}-html')
            M.get(ident_url(project, v_old if v == v_new else v_new, ident.decode()),
                  f'ident/{note}-html')
        else:
            M.get(api_url(project, ident.decode()), f'api/{note}', q=f'version={v}')
    for i, k in enumerate(rng.sample(s.comps, min(200, len(s.comps)))):
        M.get(comp_url(project, (v_old, v_new)[i % 2], k.decode()), 'ident/compat-html')
    for ident in s.hot_idents(100):
        for fam in ('A', 'B', 'D', 'K', 'M'):
            M.get(ident_url(project, v_new, ident.decode(), fam), 'ident/family')

    # ---- autocomplete strata -------------------------------------------
    for pref in sorted({k.decode()[:1] for k in s.def_fams})[:26]:
        M.get('/acp', 'acp/prefix1', q=acp_q(pref, project, 'C'))
        M.get('/acp', 'acp/prefix1', q=acp_q(pref, project, 'A'))
    for pref in rng.sample(sorted({k.decode()[:2] for k in s.def_fams}), 100):
        M.get('/acp', 'acp/prefix2', q=acp_q(pref, project, 'C'))
    for pref in rng.sample(sorted({k.decode()[:2] for k in s.comps}), 50):
        M.get('/acp', 'acp/prefix2', q=acp_q(pref, project, 'B'))
    for ident in s.hot_idents(30):
        M.get('/acp', 'acp/trunc', q=acp_q(ident.decode()[:2], project, 'C'))
    for k in rng.sample(s.comps, 30):
        M.get('/acp', 'acp/full', q=acp_q(unquote(k.decode()), project, 'B'))
    M.get('/acp', 'acp/edge', q=acp_q('a', project, 'C'))
    M.get('/acp', 'acp/edge', q=acp_q('\x7f', project, 'C'))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--project', required=True, choices=sorted(PROJECTS))
    ap.add_argument('--tier', required=True, choices=['A', 'B', 'C'])
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--repo', help='override the project repository')
    ap.add_argument('--dump', help='override the canonical dump')
    ap.add_argument('--out', help='output path '
                    '(default t/equiv/manifests/<project>-<tier>.jsonl)')
    args = ap.parse_args()

    conf = PROJECTS[args.project]
    repo = os.path.join(REPO_ROOT, args.repo or conf['repo'])
    dump = os.path.join(REPO_ROOT, args.dump or conf['dump'])
    out = args.out or os.path.join(REPO_ROOT, 't', 'equiv', 'manifests',
                                   f'{args.project}-{args.tier}.jsonl')

    print(f'parsing {dump} ...', file=sys.stderr)
    s = Strata(dump)
    print(f'  tags={len(s.vers)} defs={len(s.def_fams)} refs={len(s.ref_count)} '
          f'docs={len(s.docs)} comps={len(s.comps)}', file=sys.stderr)

    M = Manifest()
    rng = random.Random(args.seed)
    if args.project == 'musl':
        gen_musl(M, s, repo, args.tier, rng)
    else:
        gen_linux2tag(M, s, repo, args.tier, rng)

    entries = M.finalize()
    with open(out, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, separators=(',', ':'), ensure_ascii=True) + '\n')
    print(f'{out}: {len(entries)} requests')
    for stratum, n in M.histogram().items():
        print(f'  {stratum:22s} {n}')


if __name__ == '__main__':
    main()
