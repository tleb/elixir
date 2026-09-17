#  Tests for the update stall watchdog (WorkerPool in
#  elixir/update.py): a work unit that hangs past ELIXIR_STALL_TIMEOUT
#  must log one [stall] line naming the next item, then recover by
#  resubmitting it on a rebuilt pool — or fail loudly (UpdateError)
#  after three stalls. Plus the run's identity banner.
#
#  The hang task is a module-level function so the spawned workers can
#  unpickle it by reference; every file it touches lives in the test's
#  tmp_path, and the pool is stopped in a finally so no worker outlives
#  the test.
#
# This file is part of Elixir, a source code cross-referencer.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import importlib.metadata
import os
import subprocess
import sys
import threading
import time

import pytest

from elixir import update


def hang_until_released(item):
    '''A work unit with the incident's shape: it spins while its
    lockfile exists, then writes its scratch file and returns — so a
    run wedges only as long as the lockfile stays'''
    chunk_no, payload, lock, out_dir = item
    while os.path.exists(lock):
        time.sleep(0.05)
    path = os.path.join(out_dir, 'scratch-%02d' % chunk_no)
    with open(path, 'w') as f:
        f.write(payload)
    return chunk_no, payload, path


class Wedge:
    '''One watchdog test's world: the lockfile, the scratch dir, the
    captured log lines and the pool under test'''

    def __init__(self, tmp_path, monkeypatch, processes=2, timeout=0.5):
        self.lock = tmp_path / 'lock'
        self.lock.touch()
        self.out_dir = tmp_path / 'scratch'
        self.out_dir.mkdir()
        self.lines = []
        monkeypatch.setattr(update, 'log', self.lines.append)
        monkeypatch.setattr(update, 'stack_dump_wait', 0.05)
        self.pool = update.WorkerPool(processes, timeout)
        self.items = [(ci, 'payload-%d' % ci, str(self.lock), str(self.out_dir))
                      for ci in range(4)]

    def stalls(self):
        return [line for line in self.lines if line.startswith('[stall]')]

    def run(self, ctx):
        '''Consume in a thread; the main thread releases the wedge once
        the first stall line lands. Returns (pairs, raised-or-None).'''
        outcome = {}

        def consume():
            try:
                outcome['pairs'] = list(
                    self.pool.imap_consume(hang_until_released, self.items, ctx))
            except BaseException as e: # recorded, re-checked below
                outcome['error'] = e

        thread = threading.Thread(target=consume)
        thread.start()
        deadline = time.monotonic() + 60
        while not self.stalls() and thread.is_alive():
            if time.monotonic() > deadline:
                pytest.fail('no stall line was logged: %r' % self.lines)
            time.sleep(0.05)
        self.release()
        thread.join(120)
        assert not thread.is_alive()
        return outcome.get('pairs'), outcome.get('error')

    def release(self):
        if self.lock.exists():
            self.lock.unlink()

    def stop(self):
        self.release()
        self.pool.stop()


def test_stall_logged_then_recovers(tmp_path, monkeypatch):
    wedge = Wedge(tmp_path, monkeypatch)
    try:
        pairs, error = wedge.run('proj v1 refs')
    finally:
        wedge.stop()

    assert error is None
    stalls = wedge.stalls()
    assert stalls, wedge.lines
    first = stalls[0]
    assert 'proj v1 refs' in first
    assert 'retry 1/3' in first
    assert '0/4 chunks back' in first
    assert 'next item: ' in first and 'payload-0' in first

    # The resubmitted run completed, in submission order, with every
    # result paired with its own item
    assert [item[0] for item, _result in pairs] == [0, 1, 2, 3]
    assert [result[:2] for _item, result in pairs] == \
        [(ci, 'payload-%d' % ci) for ci in range(4)]
    for ci in range(4):
        path = wedge.out_dir / ('scratch-%02d' % ci)
        assert path.read_text() == 'payload-%d' % ci


def test_three_stalls_raise_update_error(tmp_path, monkeypatch):
    wedge = Wedge(tmp_path, monkeypatch)
    try:
        outcome = {}

        def consume():
            try:
                outcome['pairs'] = list(wedge.pool.imap_consume(
                    hang_until_released, wedge.items, 'proj v1 refs'))
            except BaseException as e: # recorded, re-checked below
                outcome['error'] = e

        thread = threading.Thread(target=consume)
        thread.start()
        deadline = time.monotonic() + 60
        while len(wedge.stalls()) < 3 and thread.is_alive():
            if time.monotonic() > deadline:
                pytest.fail('only %d stall lines: %r'
                            % (len(wedge.stalls()), wedge.lines))
            time.sleep(0.05)
        thread.join(120)
        assert not thread.is_alive()
        # the lockfile was never released: no result may have come back
        assert 'pairs' not in outcome
    finally:
        wedge.stop()

    stalls = wedge.stalls()
    assert len(stalls) == 3, wedge.lines
    for i, line in enumerate(stalls, 1):
        assert 'retry %d/3' % i in line

    assert isinstance(outcome['error'], update.UpdateError)
    message = str(outcome['error'])
    assert 'proj v1 refs' in message
    assert stalls[-1] in message # the error carries the last stall line


def build_tiny_repo(root):
    '''One project with a one-file one-tag repository to index'''
    repo_dir = root / 'wproj' / 'repo'
    data_dir = root / 'wproj' / 'data'
    repo_dir.mkdir(parents=True)
    data_dir.mkdir()
    git = ['git', '-C', str(repo_dir), '-c', 'user.name=test',
           '-c', 'user.email=test']

    def run(*args):
        subprocess.run(git + list(args), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    run('init', '.')
    (repo_dir / 'a.c').write_text('int a(void) { return 0; }\n')
    run('add', '.')
    run('commit', '-m', 'Initial commit')
    run('tag', 'v1')
    return str(repo_dir), str(data_dir)


def test_startup_banner(tmp_path, monkeypatch, capsys):
    repo_dir, data_dir = build_tiny_repo(tmp_path)
    monkeypatch.setattr(update, 'num_threads', 2)
    monkeypatch.setenv('ELIXIR_STALL_TIMEOUT', '7')
    monkeypatch.setenv('ELIXIR_DDB_MEM', '1GB')

    update.run(repo_dir, data_dir)

    banner = capsys.readouterr().out.splitlines()[0]
    assert 'elixir %s' % importlib.metadata.version('elixir') in banner
    assert sys.version.split()[0] in banner
    assert '2 workers' in banner # the count actually used, not the host's
    assert 'stall timeout 7s' in banner
    assert 'ELIXIR_DDB_MEM=1GB' in banner
    assert 'ELIXIR_STALL_TIMEOUT=7' in banner
