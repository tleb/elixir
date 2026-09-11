#!/usr/bin/env python3

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

"""DuckDB storage layer for Elixir — replaces the Berkeley DB layer of
data.py for new-side builds (r3a design, schema §4, determinism §6).

Schema facts, in one place:

 - blobs/versions/idents are small dimension tables, PRIMARY KEY'd (ART
   index); their INTEGER ids are dense counters assigned by the caller
   (update.py) in deterministic walk order — no sequences, no nextval.
 - defs/refs/docs/version_objects are fact tables with no constraints and
   no indexes: their access path is physical clustering (zonemaps over a
   table sorted by identid), not lookups.
 - Fact tables are written through a staging twin (<t>_stage); readers see
   <t>_all = main UNION ALL stage.  recluster() rebuilds main from _all
   ordered by the cluster key and truncates the stage.  Inserting into a
   main fact table directly is a bug: it grows the unclustered region and
   silently degrades every ident lookup (r3a risk R1).

Connection / lock model (single file per project):

 - Exactly one process may hold a database read-write; any number of
   processes may hold it read-only.  Conflicts raise duckdb.IOException:
   a second writer process, or any read-only process while a writer is
   connected, fails cleanly at connect() time.
 - In the SAME process, duckdb caches the database instance: a second
   connect() to the same path shares the existing instance instead of
   failing (so rw + ro in one process errors with a configuration
   conflict, not a lock error).  The deployment rule stays simple: the
   updater opens rw on a private copy, readers open ro on the live file,
   never both from one process on one path.

Determinism: the logical content of a database is a deterministic
function of the input walk (counter ids, no sequence state).  Physical
bytes are NOT stable, so run-to-run and golden gates use canonical_dump()
(byte-stable by construction) and compare_multiset() (row-level multiset
equality via EXCEPT ALL).
"""

import os
import sys
import tempfile

import duckdb

# Tables in the fixed order used by canonical_dump() and compare_multiset().
TABLES = ('blobs', 'versions', 'version_objects', 'idents', 'defs', 'refs', 'docs')

# Fact tables: staged, cluster-rebuilt by recluster(), no constraints.
FACT_TABLES = ('version_objects', 'defs', 'refs', 'docs')

# Bitmask for idents.def_fams / macro_fams, per def family.
FAM_BITS = {'C': 1, 'D': 2, 'K': 4, 'M': 8}

# Cluster key (and canonical total order) per fact table.
CLUSTER_ORDER = {
    'version_objects': 'versionid, blobid, filepath',
    'defs': 'identid, blobid, defline, family, deftype',
    'refs': 'identid, blobid, refline, family',
    'docs': 'identid, blobid, line, family',
}

# Column order used by insert(); kept explicit to catch schema drift.
COLUMNS = {
    'blobs': ('blobid', 'blobhash', 'filename'),
    'versions': ('versionid', 'tag'),
    'version_objects': ('versionid', 'blobid', 'filepath'),
    'idents': ('identid', 'name', 'def_fams', 'macro_fams'),
    'defs': ('identid', 'blobid', 'defline', 'deftype', 'family'),
    'refs': ('identid', 'blobid', 'refline', 'family'),
    'docs': ('identid', 'blobid', 'line', 'family'),
}

# Referenced columns checked by check_invariants(): child.col -> parent table.
FOREIGN_KEYS = {
    'version_objects': (('versionid', 'versions'), ('blobid', 'blobs')),
    'defs': (('identid', 'idents'), ('blobid', 'blobs')),
    'refs': (('identid', 'idents'), ('blobid', 'blobs')),
    'docs': (('identid', 'idents'), ('blobid', 'blobs')),
}

# Per-table dump projections, ordered by the table's key columns. blobhash
# is hex-encoded so the dump stays line-oriented text.
_DUMP = {
    'blobs': 'SELECT blobid, to_hex(blobhash) AS blobhash, filename FROM blobs ORDER BY blobid',
    'versions': 'SELECT versionid, tag FROM versions ORDER BY versionid',
    'version_objects': 'SELECT versionid, blobid, filepath FROM version_objects ORDER BY versionid, blobid, filepath',
    'idents': 'SELECT identid, name, def_fams, macro_fams FROM idents ORDER BY identid',
    'defs': 'SELECT identid, blobid, defline, deftype, family FROM defs ORDER BY identid, blobid, defline, family, deftype',
    'refs': 'SELECT identid, blobid, refline, family FROM refs ORDER BY identid, blobid, refline, family',
    'docs': 'SELECT identid, blobid, line, family FROM docs ORDER BY identid, blobid, line, family',
}

_DDL = [
    "CREATE TYPE IF NOT EXISTS deffam AS ENUM ('C','D','K','M')",
    "CREATE TYPE IF NOT EXISTS reffam AS ENUM ('C','D','K','M','B')",
    """
    CREATE TABLE IF NOT EXISTS blobs (
        blobid   INTEGER PRIMARY KEY,
        blobhash BLOB NOT NULL,
        filename VARCHAR NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS versions (
        versionid INTEGER PRIMARY KEY,
        tag       VARCHAR NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS version_objects (
        versionid INTEGER NOT NULL,
        blobid    INTEGER NOT NULL,
        filepath  VARCHAR NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS idents (
        identid    INTEGER PRIMARY KEY,
        name       VARCHAR NOT NULL,
        def_fams   SMALLINT,
        macro_fams SMALLINT
    )""",
    """
    CREATE TABLE IF NOT EXISTS defs (
        identid INTEGER NOT NULL,
        blobid  INTEGER NOT NULL,
        defline INTEGER NOT NULL,
        deftype VARCHAR,
        family  deffam NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS refs (
        identid INTEGER NOT NULL,
        blobid  INTEGER NOT NULL,
        refline INTEGER NOT NULL,
        family  reffam NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS docs (
        identid INTEGER NOT NULL,
        blobid  INTEGER NOT NULL,
        line    INTEGER NOT NULL,
        family  reffam NOT NULL
    )""",
    # By-name ident lookups (query.py hot path, autocomplete prefix
    # scans).  ART index on a dimension table — affordable next to the
    # PK ones (r3a §4); read-only connections use it, they just cannot
    # create it, which is why it lives in the write-side DDL.
    'CREATE INDEX IF NOT EXISTS idx_idents_name ON idents(name)',
]

# Staging twins of the fact tables: same columns, no constraints (the
# discipline is enforced by insert()/recluster(), not by the schema).
_STAGE_DDL = {
    'version_objects': 'CREATE TABLE IF NOT EXISTS version_objects_stage (versionid INTEGER, blobid INTEGER, filepath VARCHAR)',
    'defs': 'CREATE TABLE IF NOT EXISTS defs_stage (identid INTEGER, blobid INTEGER, defline INTEGER, deftype VARCHAR, family deffam)',
    'refs': 'CREATE TABLE IF NOT EXISTS refs_stage (identid INTEGER, blobid INTEGER, refline INTEGER, family reffam)',
    'docs': 'CREATE TABLE IF NOT EXISTS docs_stage (identid INTEGER, blobid INTEGER, line INTEGER, family reffam)',
}

##################################################################################

def connect_rw(path, threads=2, memory_limit='512MB'):
    """Open (creating and initializing if needed) `path` read-write and
    return a connection.  Fails with duckdb.IOException if another
    process holds the file (rw or ro)."""
    conn = duckdb.connect(str(path))
    conn.execute(f"SET threads={int(threads)}")
    conn.execute(f"SET memory_limit='{memory_limit}'")
    init(conn)
    return conn

def connect_ro(path):
    """Open an existing database read-only.  Fails with duckdb.IOException
    if the file is missing or held read-write by another process."""
    return duckdb.connect(str(path), read_only=True)

def init(conn):
    """Create the schema (idempotent) on a read-write connection: tables,
    staging twins and *_all views."""
    stmts = list(_DDL)
    for t in FACT_TABLES:
        cols = ', '.join(COLUMNS[t])
        stmts.append(_STAGE_DDL[t])
        stmts.append(f'CREATE VIEW IF NOT EXISTS {t}_all AS '
                     f'SELECT {cols} FROM {t} UNION ALL SELECT {cols} FROM {t}_stage')
    for stmt in stmts:
        conn.execute(stmt)

def insert(conn, table, rows):
    """Insert `rows` (tuples in COLUMNS order) enforcing the staging
    discipline: fact tables go to <t>_stage until recluster(), dimension
    tables are inserted directly.  Returns the number of rows inserted."""
    if table not in TABLES:
        raise ValueError(f'unknown table {table!r}')
    rows = list(rows)
    target = f'{table}_stage' if table in FACT_TABLES else table
    cols = ', '.join(COLUMNS[table])
    marks = ', '.join('?' * len(COLUMNS[table]))
    conn.executemany(f'INSERT INTO {target} ({cols}) VALUES ({marks})', rows)
    return len(rows)

def recluster(conn):
    """Rebuild each fact table's main copy from <t>_all in CLUSTER_ORDER
    (clustered by identid), then truncate the stage.  Atomic per call
    (single transaction).  This is the only path that merges staged rows
    into main."""
    conn.execute('BEGIN TRANSACTION')
    try:
        for t in FACT_TABLES:
            conn.execute(f'CREATE TABLE {t}__recluster AS '
                         f'SELECT * FROM {t}_all ORDER BY {CLUSTER_ORDER[t]}')
            conn.execute(f'DROP VIEW {t}_all')
            conn.execute(f'DROP TABLE {t}')
            conn.execute(f'ALTER TABLE {t}__recluster RENAME TO {t}')
            cols = ', '.join(COLUMNS[t])
            conn.execute(f'CREATE VIEW {t}_all AS '
                         f'SELECT {cols} FROM {t} UNION ALL SELECT {cols} FROM {t}_stage')
            conn.execute(f'TRUNCATE {t}_stage')
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise

def canonical_dump(conn, out=None):
    """Write a byte-stable dump of all tables to `out` (binary stream, or a
    path; default stdout), in TABLES order, each table as CSV rows ordered
    by its key columns, blobhash hex-encoded.  Hash the output to compare
    two databases; two builds of the same input produce identical bytes."""
    stream = out if hasattr(out, 'write') else (open(out, 'wb') if out is not None else sys.stdout.buffer)
    owned = stream is not out and out is not None
    try:
        for t in TABLES:
            fd, tmp = tempfile.mkstemp(suffix='.csv')
            os.close(fd)
            try:
                conn.execute(f"COPY ({_DUMP[t]}) TO '{tmp}' (FORMAT CSV, DELIMITER ' ', HEADER FALSE)")
                with open(tmp, 'rb') as f:
                    stream.write(f.read())
            finally:
                os.unlink(tmp)
    finally:
        if owned:
            stream.close()

def compare_multiset(db_a, db_b):
    """Compare two database files row-by-row (multiset equality, via EXCEPT
    ALL in both directions) over the main tables, staged rows ignored.
    Returns {} when identical, else {table: (rows only in a, rows only in
    b)} for each differing table.  db_a and db_b must be distinct paths
    (DuckDB refuses attaching an already-open file)."""
    conn = connect_ro(db_a)
    try:
        esc = str(db_b).replace("'", "''")
        conn.execute(f"ATTACH '{esc}' AS other (READ_ONLY)")
        diffs = {}
        for t in TABLES:
            n_a = conn.execute(f'SELECT count(*) FROM '
                               f'((SELECT * FROM {t}) EXCEPT ALL (SELECT * FROM other.{t}))').fetchone()[0]
            n_b = conn.execute(f'SELECT count(*) FROM '
                               f'((SELECT * FROM other.{t}) EXCEPT ALL (SELECT * FROM {t}))').fetchone()[0]
            if n_a or n_b:
                diffs[t] = (n_a, n_b)
        return diffs
    finally:
        conn.close()

def check_invariants(conn):
    """Check structural invariants in SQL (see module docstring and r3a §6)
    against the *_all views, i.e. valid mid-ingestion.  Returns a list of
    violation strings, empty when clean."""
    v = []

    # Id denseness: with a PRIMARY KEY, count == max+1 proves the ids are
    # exactly 0..max (dense counters, nothing burned).
    for t in ('blobs', 'versions', 'idents'):
        idcol = COLUMNS[t][0]
        n, hi = conn.execute(f'SELECT count(*), max({idcol}) FROM {t}').fetchone()
        if n != (hi + 1 if hi is not None else 0):
            v.append(f'{t}: ids not dense (count={n}, max={hi})')

    for t, fks in FOREIGN_KEYS.items():
        for col, parent in fks:
            idcol = COLUMNS[parent][0]
            n = conn.execute(f'SELECT count(*) FROM {t}_all c WHERE NOT EXISTS '
                             f'(SELECT 1 FROM {parent} p WHERE p.{idcol} = c.{col})').fetchone()[0]
            if n:
                v.append(f'{t}: {n} rows with orphan {col} (no {parent} row)')

    # The refs gate, exactly as update.py inserts them: an occurrence is
    # kept only for idents with at least one non-compatible def (the
    # port of the old db.defs keys; compatibles lived in db.comps).
    n = conn.execute("SELECT count(*) FROM refs_all r WHERE NOT EXISTS "
                     "(SELECT 1 FROM defs_all d WHERE d.identid = r.identid "
                     "AND d.deftype <> 'compatible')").fetchone()[0]
    if n:
        v.append(f'refs: {n} rows reference an ident with no defs')

    bits = ' '.join(f"WHEN '{f}' THEN {b}" for f, b in FAM_BITS.items())
    n = conn.execute(f"""
        WITH d AS (
            SELECT identid,
                   bit_or(CASE family {bits} END) AS def_fams,
                   coalesce(bit_or(CASE WHEN deftype = 'macro'
                               THEN CASE family {bits} END END), 0) AS macro_fams
            FROM defs_all GROUP BY identid)
        SELECT count(*) FROM idents i LEFT JOIN d USING (identid)
        WHERE i.def_fams IS DISTINCT FROM d.def_fams
           OR i.macro_fams IS DISTINCT FROM d.macro_fams""").fetchone()[0]
    if n:
        v.append(f'idents: {n} rows with def_fams/macro_fams inconsistent with defs')

    return v
