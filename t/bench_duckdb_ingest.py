#!/usr/bin/env python3

#  Ingestion microbench for elixir/data_duckdb.py (r3a §3 path (b)):
#  build an Arrow IPC file of ~5M refs rows with pyarrow, time
#  INSERT INTO refs_stage SELECT ... FROM the IPC file, then time
#  recluster() of those rows.  Not a test — run directly:
#
#      ~/.venv/bin/python t/bench_duckdb_ingest.py [NROWS] [DBDIR]
#
#  DuckDB 1.5.5 core has no read_ipc()/read_arrow() table function (that
#  lives in the downloadable 'arrow' extension), so the parent registers
#  the IPC file as a pyarrow dataset and DuckDB scans the batches — same
#  file transport, pyarrow replaces the extension.
#
#  This file is part of Elixir, a source code cross-referencer.
#
#  SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as pads

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from elixir import data_duckdb as dd

NROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 5_000_000

# 2-tag-linux shapes: 66871 blobs; identids spread so the sort in
# recluster() sees shuffled data, like real ingestion order (blobid).
NIDENT = 2_000_000
NBLOB = 66871
FAMS = ('C', 'D', 'K', 'M', 'B')

def build_ipc(path, n):
    t0 = time.perf_counter()
    identid = pa.array(((i * 2654435761) % NIDENT for i in range(n)), type=pa.uint32())
    blobid = pa.array((i % NBLOB for i in range(n)), type=pa.uint32())
    refline = pa.array((i % 997 + 1 for i in range(n)), type=pa.uint32())
    family = pa.array((FAMS[i % 5] for i in range(n)), type=pa.string())
    table = pa.Table.from_arrays([identid, blobid, refline, family],
                                 names=['identid', 'blobid', 'refline', 'family'])
    with pa.OSFile(str(path), 'wb') as f:
        with pa.ipc.new_file(f, table.schema) as w:
            for batch in table.to_batches(max_chunksize=250_000):
                w.write_batch(batch)
    return time.perf_counter() - t0, table.nbytes

def main():
    with tempfile.TemporaryDirectory(prefix='ddb-bench-') as d:
        d = Path(d)
        ipc = d / 'refs.arrow'
        gen_s, nbytes = build_ipc(ipc, NROWS)
        db = dd.connect_rw(d / 'bench.duckdb')  # threads=2, 512MB, like deployment

        t0 = time.perf_counter()
        db.register('refs_ipc', pads.dataset(str(ipc), format='ipc'))
        db.execute("INSERT INTO refs_stage "
                   "SELECT identid, blobid, refline, family::reffam FROM refs_ipc")
        ins_s = time.perf_counter() - t0
        n = db.execute('SELECT count(*) FROM refs_stage').fetchone()[0]

        t0 = time.perf_counter()
        dd.recluster(db)
        recl_s = time.perf_counter() - t0
        assert db.execute('SELECT count(*) FROM refs_stage').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM refs').fetchone()[0] == n
        db.close()

        print(f'rows={n} ipc_mib={nbytes / 2**20:.1f}')
        print(f'ipc_build={gen_s:.2f}s ingest={ins_s:.2f}s '
              f'({n / ins_s / 1e6:.1f}M rows/s) recluster={recl_s:.2f}s '
              f'({n / recl_s / 1e6:.1f}M rows/s)')

if __name__ == '__main__':
    main()
