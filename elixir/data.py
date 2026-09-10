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

import berkeleydb
import re
from . import lib
import os
import os.path
import errno

deflist_regex = re.compile(rb'(\d*)(\w)(\d*)(\w),?')
deflist_macro_regex = re.compile(r'\dM\d+(\w)')
deflist_id_regex = re.compile(rb'\d+')

##################################################################################

defTypeR = {
    'c': 'config',
    'd': 'define',
    'e': 'enum',
    'E': 'enumerator',
    'f': 'function',
    'l': 'label',
    'M': 'macro',
    'm': 'member',
    'p': 'prototype',
    's': 'struct',
    't': 'typedef',
    'u': 'union',
    'v': 'variable',
    'x': 'externvar'}

defTypeD = {v: k for k, v in defTypeR.items()}

##################################################################################

maxId = 999999999

class DefList:
    '''Stores associations between a blob ID, a type (e.g., "function"),
        a line number and a file family.
        Also stores in which families the ident exists for faster tests.'''
    def __init__(self, data=b'#'):
        self.data, self.families = data.split(b'#')

    def iter(self, dummy=False):
        # Get all element in a list of sublists and sort them
        entries = deflist_regex.findall(self.data)
        entries.sort(key=lambda x:int(x[0]))
        for id, type, line, family in entries:
            id = int(id)
            type = defTypeR [type.decode()]
            line = int(line)
            family = family.decode()
            yield id, type, line, family
        if dummy:
            yield maxId, None, None, None

    def append(self, id, type, line, family):
        if type not in defTypeD:
            return
        p = str(id) + defTypeD[type] + str(line) + family
        # Insert at the canonical position instead of concatenating:
        # blobs defining a shared ident are indexed by parallel
        # threads, so arrival order follows thread scheduling. Sorted
        # insertion by blob ID, stable for equal IDs (the sort key of
        # iter()), makes the record bytes depend only on the set of
        # definitions. One blob belongs to exactly one work unit, so
        # equal-ID entries keep their call order.
        entries = self.data.split(b',') if self.data else []
        pos = len(entries)
        for i, entry in enumerate(entries):
            if int(deflist_id_regex.match(entry).group()) > id:
                pos = i
                break
        entries.insert(pos, p.encode())
        self.data = b','.join(entries)
        self.add_family(family)

    def pack(self):
        return self.data + b'#' + self.families

    def add_family(self, family):
        # Canonical order as in append(): defs of a shared ident are
        # appended by parallel threads, so a family's arrival order
        # follows thread scheduling. Sorted insertion makes the record
        # bytes depend only on the set of families.
        family = family.encode()
        fams = self.families.split(b',') if self.families else []
        if family not in fams:
            fams.append(family)
            fams.sort()
        self.families = b','.join(fams)

    def get_families(self):
        return self.families.decode().split(',')

    def get_macros(self):
        return deflist_macro_regex.findall(self.data.decode()) or ''

class PathList:
    '''Stores associations between a blob ID and a file path.
        Inserted by update.py sorted by blob ID.'''
    def __init__(self, data=b''):
        self.data = data

    def iter(self, dummy=False):
        for p in self.data.split(b'\n')[:-1]:
            id, path = p.split(b' ',maxsplit=1)
            id = int(id)
            path = path.decode()
            yield id, path
        if dummy:
            yield maxId, None

    def append(self, id, path):
        p = str(id).encode() + b' ' + path + b'\n'
        self.data += p

    def pack(self):
        return self.data

class RefList:
    '''Stores a mapping from blob ID to list of lines
        and the corresponding family.'''
    def __init__(self, data=b''):
        self.data = data

    def iter(self, dummy=False):
        # Split all elements in a list of sublists and sort them
        entries = [x.split(b':') for x in self.data.split(b'\n')[:-1]]
        entries.sort(key=lambda x:int(x[0]))
        for b, c, d in entries:
            b = int(b.decode())
            c = c.decode()
            d = d.decode()
            yield b, c, d
        if dummy:
            yield maxId, None, None

    def append(self, id, lines, family):
        p = (str(id) + ':' + lines + ':' + family).encode()
        # Canonical insertion as in DefList.append: docs, comps and
        # comps_docs appends for a shared ident arrive from parallel
        # threads, and one blob can append several entries (one per
        # line) which must keep their call order.
        entries = self.data.split(b'\n')[:-1]
        pos = len(entries)
        for i, entry in enumerate(entries):
            if int(entry.split(b':', 1)[0]) > id:
                pos = i
                break
        entries.insert(pos, p)
        self.data = b'\n'.join(entries) + b'\n'

    def pack(self):
        return self.data

class BsdDB:
    def __init__(self, filename, readonly, contentType, shared=False):
        self.filename = filename
        self.db = berkeleydb.db.DB()
        # Skip the shared-memory default cache; a per-handle cache matters
        # once the databases stop fitting the OS page cache. Measured -11%
        # indexing Linux v6.12.6 with a larger cache in the #372 benchmarks.
        self.db.set_cachesize(0, 256 * 1024 * 1024)
        flags = berkeleydb.db.DB_THREAD if shared else 0

        if readonly:
            flags |= berkeleydb.db.DB_RDONLY
            self.db.open(filename, flags=flags)
        else:
            flags |= berkeleydb.db.DB_CREATE
            self.db.open(filename, flags=flags, mode=0o644, dbtype=berkeleydb.db.DB_BTREE)
        self.ctype = contentType

    def exists(self, key):
        key = lib.autoBytes(key)
        return self.db.exists(key)

    def get(self, key):
        key = lib.autoBytes(key)
        p = self.db.get(key)
        return self.ctype(p) if p is not None else None

    def get_keys(self):
        return self.db.keys()

    def put(self, key, val, sync=False):
        key = lib.autoBytes(key)
        val = lib.autoBytes(val)
        if type(val) is not bytes:
            val = val.pack()
        self.db.put(key, val)
        if sync:
            self.db.sync()

    def put_new(self, key, val):
        '''put() that answers whether the key was fresh: False when it
        already existed (DB_NOOVERWRITE leaves the stored value)'''
        key = lib.autoBytes(key)
        val = lib.autoBytes(val)
        if type(val) is not bytes:
            val = val.pack()
        try:
            self.db.put(key, val, flags=berkeleydb.db.DB_NOOVERWRITE)
        except berkeleydb.db.DBKeyExistError:
            return False
        return True

    def sync(self):
        '''Flush this handle's dirty cache pages to the OS: what makes
        a write survive SIGKILL (the process-private cache dies with
        the process; the OS keeps what sync handed it). Boundaries
        only — it walks the whole cache, never a per-record cost'''
        self.db.sync()

    def delete(self, key):
        key = lib.autoBytes(key)
        try:
            self.db.delete(key)
        except berkeleydb.db.DBNotFoundError:
            pass

    def close(self):
        self.db.close()

    def __len__(self):
        return self.db.stat()["nkeys"]

class DB:
    def __init__(self, dir, readonly=True, dtscomp=False, shared=False):
        if os.path.isdir(dir):
            self.dir = dir
        else:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), dir)

        ro = readonly

        self.vars = BsdDB(dir + '/variables.db', ro, lambda x: int(x.decode()), shared=shared)
            # Key-value store of basic information
        self.blob = BsdDB(dir + '/blobs.db', ro, lambda x: int(x.decode()), shared=shared)
            # Map hash to sequential integer serial number
        self.hash = BsdDB(dir + '/hashes.db', ro, lambda x: x, shared=shared)
            # Map serial number back to hash
        self.file = BsdDB(dir + '/filenames.db', ro, lambda x: x.decode(), shared=shared)
            # Map serial number to filename
        self.vers = BsdDB(dir + '/versions.db', ro, PathList, shared=shared)
        self.defs = BsdDB(dir + '/definitions.db', ro, DefList, shared=shared)
        self.defs_cache = {}
        NOOP = lambda x: x
        self.defs_cache['C'] = BsdDB(dir + '/definitions-cache-C.db', ro, NOOP, shared=shared)
        self.defs_cache['K'] = BsdDB(dir + '/definitions-cache-K.db', ro, NOOP, shared=shared)
        self.defs_cache['D'] = BsdDB(dir + '/definitions-cache-D.db', ro, NOOP, shared=shared)
        self.defs_cache['M'] = BsdDB(dir + '/definitions-cache-M.db', ro, NOOP, shared=shared)
        assert sorted(self.defs_cache.keys()) == sorted(lib.CACHED_DEFINITIONS_FAMILIES)
        self.refs = BsdDB(dir + '/references.db', ro, RefList, shared=shared)
        self.docs = BsdDB(dir + '/doccomments.db', ro, RefList, shared=shared)
        self.dtscomp = dtscomp
        if dtscomp:
            self.comps = BsdDB(dir + '/compatibledts.db', ro, RefList, shared=shared)
            self.comps_docs = BsdDB(dir + '/compatibledts_docs.db', ro, RefList, shared=shared)
            # Use a RefList in case there are multiple doc comments for an identifier

    def sync_all(self):
        '''Sync every handle: the boundary call that makes a phase's
        writes durable as a set. Each handle has its own private
        cache (no shared environment), so syncing one says nothing
        about the others — order is the caller's business'''
        dbs = [self.vars, self.blob, self.hash, self.file, self.vers,
               self.defs, *self.defs_cache.values(), self.refs, self.docs]
        if self.dtscomp:
            dbs += [self.comps, self.comps_docs]
        for db in dbs:
            db.sync()

    def close(self):
        self.vars.close()
        self.blob.close()
        self.hash.close()
        self.file.close()
        self.vers.close()
        self.defs.close()
        self.defs_cache['C'].close()
        self.defs_cache['K'].close()
        self.defs_cache['D'].close()
        self.defs_cache['M'].close()
        self.refs.close()
        self.docs.close()
        if self.dtscomp:
            self.comps.close()
            self.comps_docs.close()

