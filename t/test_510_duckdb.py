#!/usr/bin/env python3

#  Tests for elixir/data_duckdb.py: schema/staging discipline, lock model,
#  determinism helpers (canonical_dump, compare_multiset) and SQL
#  invariant checks.  r3a design: schema §4, determinism §6.
#
#  This file is part of Elixir, a source code cross-referencer.
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
#  SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import io
import shutil
import subprocess
import sys

import duckdb
import pytest

from elixir import data_duckdb as dd

defTypeR = {
    'c': 'config', 'd': 'define', 'e': 'enum', 'E': 'enumerator',
    'f': 'function', 'l': 'label', 'M': 'macro', 'm': 'member',
    'p': 'prototype', 's': 'struct', 't': 'typedef', 'u': 'union',
    'v': 'variable', 'x': 'externvar'}


def synthetic_dataset():
    """Small deterministic dataset covering every def family (C/D/K/M),
    every ref family (C/D/K/M/B), a macro, doc comments and two versions.
    Ids are dense counters, like update.py assigns them."""
    blobs = [(i, hashlib.sha1(f'blob{i}'.encode()).digest(), name)
             for i, name in enumerate(('a.c', 'b.c', 'd.dts', 'l.lds', 's.S', 'doc.rst'))]
    versions = [(0, 'v5.4'), (1, 'v6.0')]
    version_objects = [
        (0, 0, 'a.c'), (0, 1, 'b.c'), (0, 2, 'dev.dts'), (0, 3, 'kernel.lds'),
        (1, 0, 'a.c'), (1, 1, 'b.c'), (1, 4, 'entry.S'), (1, 5, 'doc.rst')]

    defs, refs, docs = [], [], []
    idents = []
    names = ('u32', 'main', 'FOO', 'bar', 'end')
    fams_of = {'u32': ('C', 'K'), 'main': ('C',), 'FOO': ('K',), 'bar': ('D',), 'end': ('M',)}
    for identid, name in enumerate(names):
        def_fams = macro_fams = 0
        for n, fam in enumerate(fams_of[name]):
            fam_bit = dd.FAM_BITS[fam]
            def_fams |= fam_bit
            # one def per family, FOO is a macro in K
            deftype = 'macro' if name == 'FOO' else defTypeR[
                {'C': 'f', 'K': 'd', 'D': 's', 'M': 'l'}[fam]]
            if deftype == 'macro':
                macro_fams |= fam_bit
            defs.append((identid, n, 100 + n, deftype, fam))
        idents.append((identid, name, def_fams, macro_fams))
        # occurrences in every ref family, spread over blobs and lines
        for n, fam in enumerate('CDKMB'):
            refs.append((identid, (n + identid) % 6, 10 * n + identid, fam))
    # docs rows now carry the occurrence family: C/D/K/M for /**
    # comments, B for DT bindings doc comments (comps_docs)
    docs = [(0, 0, 3, 'C'), (2, 0, 7, 'K'), (3, 2, 42, 'D'), (0, 5, 8, 'B')]
    return {'blobs': blobs, 'versions': versions, 'version_objects': version_objects,
            'idents': idents, 'defs': defs, 'refs': refs, 'docs': docs}


# Fixed ingestion order: dimension tables first, facts staged then
# reclustering per batch.  Same input + same order = same database.
INGEST_ORDER = ('blobs', 'versions', 'idents', 'version_objects', 'defs', 'refs', 'docs')

def build_db(path, batches=1):
    ds = synthetic_dataset()
    conn = dd.connect_rw(path)
    for table in INGEST_ORDER:
        rows = ds[table]
        if table == 'refs':
            for b in range(batches):
                dd.insert(conn, table, rows[b::batches])
                dd.recluster(conn)
        else:
            dd.insert(conn, table, rows)
            if table in dd.FACT_TABLES:
                dd.recluster(conn)
    conn.close()
    return path

def dump_hash(path):
    conn = dd.connect_ro(path)
    buf = io.BytesIO()
    dd.canonical_dump(conn, buf)
    conn.close()
    return hashlib.sha256(buf.getvalue()).hexdigest()

@pytest.fixture(scope='module')
def good_db(tmp_path_factory):
    return build_db(tmp_path_factory.mktemp('good') / 'data.duckdb')

def copy_db(src, dst_dir, name='data.duckdb'):
    """Copy a cleanly-closed database (single file, WAL checkpointed)."""
    dst = dst_dir / name
    shutil.copy(src, dst)
    return dst


##################################################################################
# By-name index: the write-side DDL creates it (read-only connections
# cannot), init() is idempotent, and it changes no logical content

def test_init_creates_idents_name_index(tmp_path):
    path = tmp_path / 'data.duckdb'
    conn = dd.connect_rw(path)
    conn.execute("INSERT INTO idents VALUES (0, 'foo', NULL, NULL)")
    conn.close()

    conn = dd.connect_rw(path)  # idempotent
    rows = conn.execute(
        "SELECT index_name, table_name FROM duckdb_indexes()"
        " WHERE table_name = 'idents'").fetchall()
    conn.close()
    assert ('idx_idents_name', 'idents') in rows

    conn = dd.connect_ro(path)
    assert conn.execute(
        "SELECT identid FROM idents WHERE name = 'foo'"
    ).fetchall() == [(0,)]
    conn.close()

def test_index_changes_no_logical_content(good_db, tmp_path):
    before = dump_hash(good_db)
    conn = dd.connect_rw(good_db)  # would create the index if missing
    conn.close()
    assert dump_hash(good_db) == before

##################################################################################
# Determinism: two independent builds from the same input

def test_two_builds_same_dump_hash(tmp_path):
    a = build_db(tmp_path / 'a.duckdb')
    b = build_db(tmp_path / 'b.duckdb')
    assert dump_hash(a) == dump_hash(b)

def test_dump_to_path_matches_stream(tmp_path):
    db = build_db(tmp_path / 'data.duckdb')
    out = tmp_path / 'dump.txt'
    conn = dd.connect_ro(db)
    dd.canonical_dump(conn, out)
    buf = io.BytesIO()
    dd.canonical_dump(conn, buf)
    conn.close()
    assert out.read_bytes() == buf.getvalue()
    assert out.is_file()  # written into cwd nowhere
    assert dump_hash(db) == hashlib.sha256(out.read_bytes()).hexdigest()

def test_two_builds_multiset_equal(tmp_path):
    a = build_db(tmp_path / 'a.duckdb')
    b = build_db(tmp_path / 'b.duckdb')
    assert dd.compare_multiset(a, b) == {}

def test_staging_batches_do_not_change_dump(tmp_path):
    # one big stage batch vs three batches with a recluster after each
    a = build_db(tmp_path / 'a.duckdb', batches=1)
    b = build_db(tmp_path / 'b.duckdb', batches=3)
    assert dump_hash(a) == dump_hash(b)
    assert dd.compare_multiset(a, b) == {}

def test_dump_is_not_vacuous(tmp_path):
    # the gate must discriminate: one extra refs row changes hash + report
    a = build_db(tmp_path / 'a.duckdb')
    b = build_db(tmp_path / 'b.duckdb')
    conn = dd.connect_rw(b)
    conn.execute("INSERT INTO refs_stage VALUES (0, 0, 999, 'B')")
    dd.recluster(conn)
    conn.close()
    assert dump_hash(a) != dump_hash(b)
    assert dd.compare_multiset(a, b) == {'refs': (0, 1)}


##################################################################################
# Invariant checks

def test_invariants_clean_on_good_db(good_db):
    conn = dd.connect_ro(good_db)
    assert dd.check_invariants(conn) == []
    conn.close()

def test_invariants_valid_mid_ingestion(good_db, tmp_path):
    # staged-but-unreclustered rows are part of the database: checks run
    # against the *_all views, so this must still be clean
    db = copy_db(good_db, tmp_path)
    conn = dd.connect_rw(db)
    conn.execute("INSERT INTO refs_stage VALUES (0, 1, 55, 'C')")
    assert dd.check_invariants(conn) == []
    conn.close()

@pytest.mark.parametrize('table', ['blobs', 'idents', 'versions'])
def test_id_denseness_violation(good_db, tmp_path, table):
    db = copy_db(good_db, tmp_path)
    conn = dd.connect_rw(db)
    idcol = dd.COLUMNS[table][0]
    gap = conn.execute(f'SELECT max({idcol}) + 2 FROM {table}').fetchone()[0]
    dd.insert(conn, table, [{'blobs': (gap, b'x' * 20, 'gap.c'),
                             'idents': (gap, 'gap', None, None),
                             'versions': (gap, 'v9.9')}[table]])
    v = dd.check_invariants(conn)
    assert len(v) == 1 and v[0].startswith(f'{table}: ids not dense')
    conn.close()

@pytest.mark.parametrize('table,col,parent', [
    ('refs', 'identid', 'idents'), ('refs', 'blobid', 'blobs'),
    ('defs', 'identid', 'idents'), ('defs', 'blobid', 'blobs'),
    ('docs', 'identid', 'idents'), ('docs', 'blobid', 'blobs'),
    ('version_objects', 'versionid', 'versions'), ('version_objects', 'blobid', 'blobs'),
])
def test_orphan_violation(good_db, tmp_path, table, col, parent):
    db = copy_db(good_db, tmp_path)
    conn = dd.connect_rw(db)
    idcol = dd.COLUMNS[parent][0]
    orphan = conn.execute(f'SELECT max({idcol}) + 7 FROM {parent}').fetchone()[0]
    # orphan id in the corrupted column, valid ids elsewhere
    row = {
        ('refs', 'identid'): (orphan, 0, 1, 'C'),
        ('refs', 'blobid'): (0, orphan, 1, 'C'),
        ('defs', 'identid'): (orphan, 0, 1, 'function', 'C'),
        ('defs', 'blobid'): (0, orphan, 1, 'function', 'C'),
        ('docs', 'identid'): (orphan, 0, 1, 'C'),
        ('docs', 'blobid'): (0, orphan, 1, 'C'),
        ('version_objects', 'versionid'): (orphan, 0, 'x.c'),
        ('version_objects', 'blobid'): (0, orphan, 'x.c'),
    }[(table, col)]
    dd.insert(conn, table, [row])
    v = dd.check_invariants(conn)
    expected = [f'{table}: 1 rows with orphan {col} (no {parent} row)']
    # an orphan refs.identid also trips the refs-need-defs gate
    if table == 'refs' and col == 'identid':
        expected.append('refs: 1 rows reference an ident with no defs')
    assert sorted(v) == sorted(expected)
    conn.close()

def test_refs_without_defs_violation(good_db, tmp_path):
    db = copy_db(good_db, tmp_path)
    conn = dd.connect_rw(db)
    # a legit new ident (dense, no defs) referenced by a new staged ref
    new_id = conn.execute('SELECT max(identid) + 1 FROM idents').fetchone()[0]
    dd.insert(conn, 'idents', [(new_id, 'never_defined', None, None)])
    dd.insert(conn, 'refs', [(new_id, 0, 77, 'C')])
    v = dd.check_invariants(conn)
    assert v == ['refs: 1 rows reference an ident with no defs']
    conn.close()

def test_def_fams_inconsistency_violation(good_db, tmp_path):
    db = copy_db(good_db, tmp_path)
    conn = dd.connect_rw(db)
    conn.execute("UPDATE idents SET def_fams = def_fams | 8 WHERE name = 'u32'")
    v = dd.check_invariants(conn)
    assert len(v) == 1 and v[0].startswith('idents: 1 rows with def_fams')
    conn.close()


##################################################################################
# Lock model: one rw process XOR any number of ro processes

def subprocess_connect(path, read_only):
    code = (f'import duckdb\n'
            f'c = duckdb.connect({str(path)!r}, read_only={read_only})\n'
            f'print("CONNECTED", c.execute("SELECT count(*) FROM blobs").fetchone()[0])\n')
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)

def test_second_writer_process_fails_cleanly(tmp_path):
    db = tmp_path / 'data.duckdb'
    build_db(db)
    writer = dd.connect_rw(db)
    writer.execute('SELECT 42').fetchone()  # healthy
    r = subprocess_connect(db, read_only=False)
    assert 'CONNECTED' not in r.stdout and 'IOException' in r.stderr
    assert r.returncode != 0
    # the surviving writer is unharmed
    assert writer.execute('SELECT count(*) FROM blobs').fetchone()[0] == 6
    writer.close()

def test_ro_process_fails_while_writer_open(tmp_path):
    db = tmp_path / 'data.duckdb'
    build_db(db)
    writer = dd.connect_rw(db)
    r = subprocess_connect(db, read_only=True)
    assert 'CONNECTED' not in r.stdout and 'IOException' in r.stderr
    assert r.returncode != 0
    writer.close()

def test_ro_processes_coexist(tmp_path):
    db = tmp_path / 'data.duckdb'
    build_db(db)
    conns = [dd.connect_ro(db) for _ in range(3)]
    for c in conns:
        assert c.execute('SELECT count(*) FROM refs').fetchone()[0] > 0
    r = subprocess_connect(db, read_only=True)  # fourth, cross-process
    assert r.stdout.startswith('CONNECTED') and r.returncode == 0
    for c in conns:
        c.close()

def test_ro_allowed_after_writer_closes(tmp_path):
    db = tmp_path / 'data.duckdb'
    conn = dd.connect_rw(db)
    conn.close()
    r = subprocess_connect(db, read_only=True)
    assert r.stdout.startswith('CONNECTED') and r.returncode == 0

def test_ro_on_missing_file_raises(tmp_path):
    with pytest.raises(duckdb.IOException):
        dd.connect_ro(tmp_path / 'nope.duckdb')


##################################################################################
# Staging and clustering discipline

def test_insert_stages_facts_leaves_main_untouched(tmp_path):
    db = tmp_path / 'data.duckdb'
    conn = dd.connect_rw(db)
    dd.insert(conn, 'blobs', [(0, b'a' * 20, 'a.c')])
    dd.insert(conn, 'idents', [(0, 'u32', 1, 0)])
    dd.insert(conn, 'defs', [(0, 0, 1, 'function', 'C')])
    dd.insert(conn, 'refs', [(0, 0, 5, 'C')])
    assert conn.execute('SELECT count(*) FROM refs').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM defs').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM refs_all').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM defs_all').fetchone()[0] == 1
    conn.close()

def test_recluster_merges_and_clusters(tmp_path):
    db = tmp_path / 'data.duckdb'
    conn = dd.connect_rw(db)
    dd.insert(conn, 'blobs', [(i, bytes([i]) * 20, f'{i}.c') for i in range(4)])
    dd.insert(conn, 'idents', [(i, f'i{i}', None, None) for i in range(4)])
    rows = [(3 - i, i % 4, i, 'C') for i in range(40)]  # identid descending
    dd.insert(conn, 'refs', rows)
    n_all = conn.execute('SELECT count(*) FROM refs_all').fetchone()[0]
    dd.recluster(conn)
    assert conn.execute('SELECT count(*) FROM refs_stage').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM refs').fetchone()[0] == 40
    assert conn.execute('SELECT count(*) FROM refs_all').fetchone()[0] == n_all
    # physical clustering: storage order (rowid) is non-decreasing identid
    n = conn.execute("""
        SELECT count(*) FROM (
            SELECT identid, lag(identid) OVER (ORDER BY rowid) AS prev FROM refs)
        WHERE prev IS NOT NULL AND identid < prev""").fetchone()[0]
    assert n == 0
    conn.close()

def test_insert_rejects_unknown_table(tmp_path):
    conn = dd.connect_rw(tmp_path / 'data.duckdb')
    with pytest.raises(ValueError):
        dd.insert(conn, 'bogus', [(1,)])
    conn.close()


##################################################################################
# canonical_dump mechanics

def test_dump_orders_by_key_columns(good_db):
    conn = dd.connect_ro(good_db)
    buf = io.BytesIO()
    dd.canonical_dump(conn, buf)
    conn.close()
    lines = buf.getvalue().decode().splitlines()
    # blobs first (hex hashes uppercase, no leading spaces), ordered by blobid
    assert lines[0].startswith('0 ')
    # docs is the last table in the dump: its rows close the output,
    # ordered by identid, blobid, line, family — the family column is
    # part of the key order now
    assert lines[-4:] == ['0 0 3 C', '0 5 8 B', '2 0 7 K', '3 2 42 D']
    # refs and docs rows share the 4-field shape: 25 refs + 4 docs
    refs = [ln.split() for ln in lines if len(ln.split()) == 4 and ln.split()[3] in 'CDKMB']
    assert len(refs) == 25 + 4
