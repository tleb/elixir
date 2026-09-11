import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t.equiv import capture as capture_mod
from t.equiv import common, normalize
from t.equiv import replay as replay_mod


def test_normalize_drops_date_header():
    h = normalize.normalize_headers({'Date': 'Wed, 10 Sep 2025 00:00:00 GMT',
                                     'Content-Type': 'text/html'})
    assert 'date' not in h
    assert h == {'content-type': 'text/html'}


def test_normalize_scrubs_both_timestamp_forms():
    page = (b'Request date: 2025-09-10 12:34:56.789012\n'
            b'Path: /x\n'
            b'Request%20date%3A%202025-09-10%2012%3A34%3A56.789012%0APath%3A%20/x')
    out = normalize.normalize_body(page)
    assert out.count(b'<TS>') == 2
    assert b'789012' not in out
    # optional-fraction edge: str(datetime) drops .0 microseconds
    out2 = normalize.normalize_body(b'Request date: 2025-09-10 12:34:56\nx')
    assert b'Request date: <TS>' in out2


def _mini_manifest(project='testproj', version='v5.4'):
    return [
        {'i': 0, 'm': 'GET', 'p': f'/{project}/{version}/source', 'q': None, 'h': {}, 'note': 'tree'},
        {'i': 1, 'm': 'GET', 'p': f'/{project}/{version}/source/issue102.c', 'q': None, 'h': {}, 'note': 'source'},
        {'i': 2, 'm': 'GET', 'p': f'/{project}/{version}/ident/gsb_buffer', 'q': None, 'h': {}, 'note': 'ident'},
        {'i': 3, 'm': 'GET', 'p': '/acp', 'q': 'q=gsb&p=testproj&f=C', 'h': {}, 'note': 'acp'},
        {'i': 4, 'm': 'GET', 'p': f'/api/ident/{project}/gsb_buffer', 'q': f'version={version}', 'h': {}, 'note': 'api'},
        # an error page: carries the run-to-run timestamp
        {'i': 5, 'm': 'GET', 'p': f'/{project}/{version}/source/nosuchfile.c', 'q': None, 'h': {}, 'note': 'err404'},
    ]


@pytest.fixture()
def pinned_env(testenv):
    """Run the harness against the session testproj; restore LXR_* after"""
    saved = {k: __import__('os').environ.get(k) for k in ('LXR_PROJ_DIR', 'ELIXIR_VERSION')}
    common.pin_env(testenv.proj_dir)
    yield testenv
    import os
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# Capture records the tree's git provenance (commit, branch, dirty);
# skip cleanly where the tree is .git-less (e.g. the Docker image)
# instead of failing. Real checkouts keep full provenance.
@pytest.mark.skipif(not os.path.exists(os.path.join(common.REPO_ROOT, '.git')),
                    reason=f'{common.REPO_ROOT} is not a git worktree '
                           '(no provenance to record)')
def test_capture_replay_roundtrip_and_sensitivity(tmp_path, pinned_env):
    """Gate 1/2/3 of T-E1, at pytest scale: determinism of the pipeline,
    zero-diff replay against the same side, and one-byte sensitivity"""
    manifest = tmp_path / 'mini.jsonl'
    with open(manifest, 'w') as f:
        for e in _mini_manifest():
            f.write(json.dumps(e) + '\n')
    out = tmp_path / 'captures'

    # the captured error page must have its timestamp normalized away
    capture_mod.run_capture(str(manifest), str(out), pinned_env.proj_dir,
                            dump_hash=False, force=True, project='testproj')
    import base64
    records = list(common.iter_records(str(out)))
    assert [r['s'] for r in records] == [200, 200, 200, 200, 200, 404]
    err_body = base64.b64decode(records[5]['b'])
    assert b'Request date: <TS>' in err_body

    # gate 2: replay against the same side -> 0 diffs
    report = replay_mod.run_replay(str(manifest), str(out), pinned_env.proj_dir)
    assert report['diffs'] == 0

    # gate 1: a second capture is byte-identical after normalization
    out2 = tmp_path / 'captures2'
    capture_mod.run_capture(str(manifest), str(out2), pinned_env.proj_dir,
                            dump_hash=False, force=True, project='testproj')
    assert list(common.iter_records(str(out))) == list(common.iter_records(str(out2)))

    # gate 3: flip ONE byte in one captured body -> replay flags exactly it
    recs = list(common.iter_records_file(common.records_file(str(out))))
    body = bytearray(base64.b64decode(recs[1]['b']))
    body[len(body) // 2] ^= 1
    recs[1]['b'] = base64.b64encode(bytes(body)).decode()
    rf = common.records_file(str(out))
    stem = rf[:-4] if rf.endswith('.zst') or rf.endswith('.gz') else rf
    w, _ = common.open_writer(stem)
    for r in recs:
        w.write((json.dumps(r, separators=(',', ':')) + '\n').encode())
    w.close()

    report = replay_mod.run_replay(str(manifest), str(out), pinned_env.proj_dir)
    assert report['diffs'] == 1
    assert report['strata']['source']['diff'] == 1
    d = report['first_diffs'][0]
    assert d['i'] == 1
    assert 'body_diff' in d or 'body_sha256' in d
