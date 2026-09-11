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

"""Shared runner of the t/equiv harness (R3B §2): env pinning, the
in-process falcon TestClient (with the RAW_URI extras that reproduce
the raw-path behavior of the real WSGI server), the record format and
its compressed writers, and the meta.json provenance.

Manifest line (index-stable, the `note` tags the stratum for reports):
  {"i":0,"m":"GET","p":"/musl/v1.2.5/ident/u32","q":null,"h":{},"note":"hot-ident"}
`p` is the path exactly as sent on the wire (quoted); `q` the raw query
string; `b` (optional) a request body, for POST forms.

Record line:
  {"i":0,"s":200,"h":{...minus Date...},"b":"<b64 normalized body>"}
"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from t.equiv import normalize  # noqa: E402  (needs the sys.path fix above)

# Kills the git-hash-in-footer variance: both sides render with the same
# version string regardless of the tree they run from (R3B §2)
ELIXIR_VERSION_PIN = 'equiv-harness'

# Capture/provenance locations relative to the repo (R3B §2, §5)
DEFAULT_CAPTURES_ROOT = os.path.join(os.path.dirname(REPO_ROOT), 'elixir-data-acceptance', 'equiv')
DEFAULT_BOOTSTRAP_ROOT = os.path.join(os.path.dirname(REPO_ROOT), 'equiv-bootstrap')

# proj-dir of the frozen old side, per project name (linux's data dir is
# rebuilt by T-E2, not this task)
OLD_SIDES = {
    'musl': os.path.join(DEFAULT_BOOTSTRAP_ROOT, 'musl-old'),
    'linux': os.path.join(DEFAULT_BOOTSTRAP_ROOT, 'linux2tag-old'),
}


def pin_env(proj_dir):
    """Pin LXR_PROJ_DIR and ELIXIR_VERSION before elixir.web is imported;
    the app reads the project dir per request, the version at import"""
    os.environ['LXR_PROJ_DIR'] = proj_dir
    os.environ.setdefault('ELIXIR_VERSION', ELIXIR_VERSION_PIN)


def make_client(proj_dir):
    """The in-process client: no real HTTP server, its environ is fully
    controlled and extras RAW_URI drives RawPathComponent exactly like
    the production WSGI server. Both sides of every comparison go
    through this identical client, so client quirks cancel."""
    pin_env(proj_dir)
    from falcon import testing
    from elixir.web import get_application
    return testing.TestClient(get_application())


def extras_for(entry):
    """RAW_URI as the real server would set it: full raw target"""
    p, q = entry['p'], entry.get('q')
    return {'RAW_URI': f'{p}?{q}' if q else p}


def fetch(client, entry):
    """Run one manifest entry, return the normalized record dict"""
    kwargs = {
        'path': entry['p'],
        'extras': extras_for(entry),
        # Intentional 500s (the /acp f=B quirk) log a traceback through
        # wsgi.errors; swallow it, the record is the observation
        'wsgierrors': io.StringIO(),
    }
    if entry.get('q') is not None:
        kwargs['query_string'] = entry['q']
    if entry.get('h'):
        kwargs['headers'] = entry['h']
    if entry.get('b') is not None:
        kwargs['body'] = entry['b']

    method = entry.get('m', 'GET').lower()
    simulate = getattr(client, f'simulate_{method}')
    result = simulate(**kwargs)

    return {
        'i': entry['i'],
        's': result.status_code,
        'h': normalize.normalize_headers(dict(result.headers)),
        'b': base64.b64encode(normalize.normalize_body(result.content)).decode('ascii'),
    }


def read_manifest(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def manifest_sha256(path):
    return sha256_file(path)


# ---------------------------------------------------------------------------
# Compression: zstd if the module or the CLI exists, else gzip (R3B §2)

def _zstd_impl():
    try:
        import zstandard  # noqa: F401
        return 'module'
    except ImportError:
        return 'cli' if shutil.which('zstd') else None


class _CliZstdWriter:
    """zstd through the CLI: one subprocess per file, level 3"""

    def __init__(self, path):
        self.f = open(path, 'wb')
        self.p = subprocess.Popen(['zstd', '-3', '-q', '-'],
                                  stdin=subprocess.PIPE, stdout=self.f)

    def write(self, data):
        self.p.stdin.write(data)

    def close(self):
        self.p.stdin.close()
        rc = self.p.wait()
        self.f.close()
        if rc != 0:
            raise RuntimeError(f'zstd exited {rc}')


class _CliZstdReader:
    def __init__(self, path):
        self.p = subprocess.Popen(['zstd', '-dc', path], stdout=subprocess.PIPE)

    def read(self, *a):
        return self.p.stdout.read(*a)

    def close(self):
        self.p.stdout.close()
        rc = self.p.wait()
        if rc != 0:
            raise RuntimeError(f'zstd exited {rc}')

    def __iter__(self):
        for line in self.p.stdout:
            yield line
        self.close()


class _ModuleZstdWriter:
    def __init__(self, path):
        import zstandard
        self.f = open(path, 'wb')
        self.c = zstandard.ZstdCompressor().stream_writer(self.f)

    def write(self, data):
        self.c.write(data)

    def close(self):
        self.c.close()
        self.f.close()


class _ModuleZstdReader:
    def __init__(self, path):
        import zstandard
        self.f = open(path, 'rb')
        self.c = zstandard.ZstdDecompressor().stream_reader(self.f, read_across_frames=True)

    def read(self, *a):
        return self.c.read(*a)

    def close(self):
        self.c.close()
        self.f.close()

    def __iter__(self):
        for line in self.c:
            yield line
        self.close()


def compression_kind():
    """'zstd' when available (module or CLI), else 'gzip'"""
    return 'zstd' if _zstd_impl() else 'gzip'


def open_writer(path):
    """Compressed line writer for records; the suffix picks the format"""
    if _zstd_impl() == 'module':
        return _ModuleZstdWriter(path + '.zst'), 'zstd'
    if _zstd_impl() == 'cli':
        return _CliZstdWriter(path + '.zst'), 'zstd'
    import gzip
    return gzip.open(path + '.gz', 'wb'), 'gzip'


def records_file(outdir):
    for suffix in ('zst', 'gz'):
        p = os.path.join(outdir, f'responses.jsonl.{suffix}')
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f'no responses.jsonl.* in {outdir}')


def iter_records_file(path):
    """Stream records from one compressed responses file (suffix-driven)"""
    if path.endswith('.zst'):
        if _zstd_impl() == 'module':
            lines = _ModuleZstdReader(path)
        else:
            lines = _CliZstdReader(path)
    else:
        import gzip
        lines = gzip.open(path, 'rt', encoding='utf-8')
    try:
        for line in lines:
            if line.strip():
                yield json.loads(line)
    finally:
        if hasattr(lines, 'close'):
            lines.close()


def iter_records(outdir):
    """Stream the records of a capture dir"""
    yield from iter_records_file(records_file(outdir))


def versions_info():
    import platform
    import falcon
    import jinja2
    import pygments

    info = {
        'python': platform.python_version(),
        'falcon': falcon.__version__,
        'jinja2': jinja2.__version__,
        'pygments': pygments.__version__,
    }
    try:
        import berkeleydb
        info['berkeleydb'] = berkeleydb.__version__
    except ImportError:
        try:
            import duckdb
            info['duckdb'] = duckdb.__version__
        except ImportError:
            pass
    return info


def data_dir_dump_md5(data_dir):
    """BDB data-dir provenance: canonical utils/dump.py | md5sum,
    read-only (R3B §5). None for non-BDB (DuckDB) sides."""
    if not os.path.exists(os.path.join(data_dir, 'versions.db')):
        return None
    env = dict(os.environ, LXR_DATA_DIR=data_dir)
    dump = subprocess.run([sys.executable, os.path.join(REPO_ROOT, 'utils', 'dump.py')],
                          env=env, cwd=REPO_ROOT,
                          stdout=subprocess.PIPE, check=True)
    return hashlib.md5(dump.stdout).hexdigest()


def repo_provenance(repo_dir):
    def git(*args):
        return subprocess.run(['git', '-C', repo_dir] + list(args),
                              check=True, stdout=subprocess.PIPE).stdout
    head = git('rev-parse', 'HEAD').decode().strip()
    tags_sha = hashlib.sha256(git('tag', '-l')).hexdigest()
    return {'head': head, 'tags_sha256': tags_sha}


def elixir_provenance():
    import subprocess
    def git(*args):
        return subprocess.run(['git', '-C', REPO_ROOT] + list(args),
                              check=True, stdout=subprocess.PIPE).stdout.decode().strip()
    return {
        'commit': git('rev-parse', 'HEAD'),
        'branch': git('rev-parse', '--abbrev-ref', 'HEAD'),
        'dirty': bool(git('status', '--porcelain')),
    }


def write_meta(outdir, meta):
    with open(os.path.join(outdir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write('\n')


def read_meta(outdir):
    with open(os.path.join(outdir, 'meta.json'), encoding='utf-8') as f:
        return json.load(f)


def check_versions_match(meta):
    """Replay refuses to run if dep versions differ from meta: the
    Pygments/Jinja/falcon output is part of the surface, both sides
    must come from the same venv (R3B §2)"""
    current = versions_info()
    recorded = meta.get('versions', {})
    drift = {k: (recorded.get(k), current.get(k))
             for k in ('python', 'falcon', 'jinja2', 'pygments', 'berkeleydb', 'duckdb')
             if k in recorded and recorded.get(k) != current.get(k)}
    return drift
