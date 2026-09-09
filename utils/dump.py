#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#  and contributors
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

# Canonical dump of a data directory: one line per record,
# "<dbname>\t<hex key>\t<hex value>", sorted by (dbname, key). Hex
# encoding makes the output binary-safe and independent from the
# DB-access layer's value types, so two dumps can be compared with
# plain diff. Indexing determinism is validated exactly this way:
# index a project twice from scratch, dump both data directories,
# diff the dumps.

import sys, pathlib
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
from elixir import lib
from elixir.data import DB

def dump(data_dir, dtscomp):
    db = DB(data_dir, readonly=True, dtscomp=dtscomp)
    out = sys.stdout
    try:
        dbs = [
            ('vars', db.vars),
            ('blob', db.blob),
            ('hash', db.hash),
            ('file', db.file),
            ('vers', db.vers),
            ('defs', db.defs),
            ('defs_cache_C', db.defs_cache['C']),
            ('defs_cache_K', db.defs_cache['K']),
            ('defs_cache_D', db.defs_cache['D']),
            ('defs_cache_M', db.defs_cache['M']),
            ('refs', db.refs),
            ('docs', db.docs),
        ]
        if dtscomp:
            dbs += [
                ('comps', db.comps),
                ('comps_docs', db.comps_docs),
            ]
        for name, d in dbs:
            # Raw bytes, not the type-decoding BsdDB.get(): the dump
            # must not depend on the value classes of data.py
            for key in sorted(d.get_keys()):
                out.write(f'{name}\t{key.hex()}\t{d.db.get(key).hex()}\n')
    finally:
        db.close()

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Dump a data directory in canonical form '
                    '(one line per record, hex-encoded, sorted)')
    parser.add_argument('--dtscomp', type=int, default=0,
                        help='DTS compatibles support level, as in update.py')
    args = parser.parse_args()

    dump(lib.getDataDir(), args.dtscomp)
