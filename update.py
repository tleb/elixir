#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#                           Maxime Chretien <maxime.chretien@bootlin.com>
#                           and contributors
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

# Throughout, an "idx" is the sequential number associated with a blob.
# This is different from that blob's Git hash.
#
# Indexing runs in phases, in dependency order, one tag at a time
# (oldest first): a tag is fully indexed before the next one starts, so
# every tag is a checkpoint in db.vers and refs cannot race defs of
# another tag. The phases for one tag:
#   1. ids:    assign idx numbers to the tag's new blobs
#   2. vers:   record blob paths for the tag (fills file_paths, bindings_idxes)
#   3. defs, docs, comps: per-blob indexing independent of each other
#   4. refs:   references, gated on definitions existing in the database
#   5. comps_docs: compatibles from DT bindings docs, gated on comps
#
# Phases are sequential: a phase starts only after the previous one
# finished, so data written by one phase is visible to the next without
# locking. Inside a phase, work units (chunks of blobs) run in a thread
# pool; refs lexes in a process pool instead (pure Python, GIL-bound),
# forked once at startup while the parent is still small, because fork
# cost grows with the parent's page tables as the caches fill. The databases are opened with DB_THREAD, so plain concurrent
# accesses are safe; only read-modify-write cycles on a key are not, and
# each has a dedicated lock, held around the cycle only, never around
# lexing or subprocess calls.

import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from sys import argv
from threading import Lock, local

from elixir.lexers import TokenType
import elixir.lib as lib
from elixir.lib import script, scriptLines
import elixir.data as data
from elixir.data import PathList
from elixir.project_utils import get_lexer
from find_compatible_dts import FindCompatibleDTS

dts_comp_support = int(script('dts-comp'))

compatibles_parser = FindCompatibleDTS()

db = data.DB(lib.getDataDir(), readonly=False, shared=True, dtscomp=dts_comp_support)

idx_key_mod = 1000000
chunk_size = 256 # Max blobs per work unit; chunks() caps it further

num_threads = os.cpu_count() or 1

# Cross-phase state, written in one phase, read in later phases:
file_paths = {} # idx -> path (vers -> refs, comps_docs)
bindings_idxes = [] # DT bindings documentation files (vers -> comps_docs)
defs_idxes = {} # (idx*idx_key_mod + line) -> ident (defs -> refs)
defs_keys = set() # idents known to db.defs, snapshotted before the refs phase

# Guards for read-modify-write cycles on database keys:
defs_lock = Lock() # db.defs
docs_lock = Lock() # db.docs
refs_lock = Lock() # db.refs
comps_lock = Lock() # db.comps
comps_docs_lock = Lock() # db.comps_docs


_batch_tls = local()

def get_blob_batch(hash):
    '''Blob content from a persistent per-thread `git cat-file --batch`.

    Same bytes as script('get-blob', hash) without one fork+exec of
    script.sh and git per blob. Responses arrive in request order; one
    process per thread needs no locking.
    '''
    p = getattr(_batch_tls, 'batch', None)
    if p is None or p.poll() is not None:
        p = subprocess.Popen(['git', 'cat-file', '--batch'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             cwd=lib.getRepoDir())
        _batch_tls.batch = p

    p.stdin.write(hash + b'\n')
    p.stdin.flush()

    header = p.stdout.readline().split()
    assert header[1] == b'blob', header
    size = int(header[2])
    data = p.stdout.read(size)
    p.stdout.read(1) # newline after the payload
    return data


executor = None # Created on first use; its threads live for the whole run

def parallel(fn, items):
    '''Run fn on every item with the thread pool, surfacing failures.'''
    global executor
    if not items: return
    # One executor for the whole run. Threads cache per-thread state
    # (the cat-file --batch pipes below) that a phase-scoped executor
    # would leak: its threads die with it and the pipes stay open.
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=num_threads)
    list(executor.map(fn, items))


def chunks(idxs):
    # Never hand one worker more than a num_threads-th of the blobs:
    # most tags add fewer blobs than chunk_size * num_threads, and one
    # big chunk would leave the other workers idle for the whole phase.
    size = min(chunk_size, max(1, -(-len(idxs) // num_threads)))
    for i in range(0, len(idxs), size):
        yield idxs[i:i+size]


def progress(msg):
    print(project + ' - ' + msg, flush=True)


def update_blob_ids(tag):
    if db.vars.exists('numBlobs'):
        idx = db.vars.get('numBlobs')
    else:
        idx = 0

    # Get blob hashes and associated file names (without path)
    blobs = scriptLines('list-blobs', '-f', tag)

    new_idxes = []
    for blob in blobs:
        hash, filename = blob.split(b' ',maxsplit=1)
        if not db.blob.exists(hash):
            db.blob.put(hash, idx)
            db.hash.put(idx, hash)
            db.file.put(idx, filename)

            new_idxes.append(idx)
            idx += 1
    db.vars.put('numBlobs', idx)
    return new_idxes


def update_versions(tag):
    # Get blob hashes and associated file paths
    blobs = scriptLines('list-blobs', '-p', tag)
    buf = []

    for blob in blobs:
        hash, path = blob.split(b' ', maxsplit=1)
        idx = db.blob.get(hash)
        buf.append((idx, path))
        file_paths[idx] = path

    buf = sorted(buf)
    obj = PathList()
    for idx, path in buf:
        obj.append(idx, path)

        # Store DT bindings documentation files to parse them later
        if path[:33] == b'Documentation/devicetree/bindings':
            bindings_idxes.append(idx)

    db.vers.put(tag, obj, sync=True)
    progress('vers: ' + tag.decode() + ' done')


def generate_defs_caches():
    for key in db.defs.get_keys():
        value = db.defs.get(key)
        for family in ['C', 'K', 'D', 'M']:
            if (lib.compatibleFamily(value.get_families(), family) or
                        lib.compatibleMacro(value.get_macros(), family)):
                db.defs_cache[family].put(key, b'')


def update_definitions(idxs):
    for idx in idxs:
        if idx % 1000 == 0: progress('defs: ' + str(idx))

        hash = db.hash.get(idx)
        filename = db.file.get(idx)

        family = lib.getFileFamily(filename)
        if family in [None, 'M']: continue

        lines = scriptLines('parse-defs', hash, filename, family)

        for l in lines:
            ident, type, line = l.split(b' ')
            type = type.decode()
            line = int(line.decode())

            defs_idxes[idx*idx_key_mod + line] = ident

            with defs_lock:
                if db.defs.exists(ident):
                    obj = db.defs.get(ident)
                elif lib.isIdent(ident):
                    obj = data.DefList()
                else:
                    continue

                obj.append(idx, type, line, family)
                db.defs.put(ident, obj)


def _refs_lex_chunk(triples):
    '''Lex a chunk of (idx, path, hash); return [(idx, family, idents)].

    idents maps every identifier token of the blob to the list of its
    line numbers. Workers carry no per-tag state, so the pool can be
    forked once at startup and live across tags; gating on definitions
    happens in the parent, which owns the defs state.
    '''
    out = []
    for idx, filename, hash in triples:
        # getFileFamily expects a basename; the name-based families
        # (kconfig*, makefile*) must match in subdirectories too
        family = lib.getFileFamily(os.path.basename(filename))
        if family == None: continue

        lexer = get_lexer(filename, project)
        if lexer is None:
            continue

        try:
            code = get_blob_batch(hash).decode()
        except UnicodeDecodeError:
            code = get_blob_batch(hash).decode('raw_unicode_escape')

        prefix = b''
        # Kconfig values are saved as CONFIG_<value>
        if family == 'K':
            prefix = b'CONFIG_'

        idents = {}
        for token_type, token, _, line in lexer(code).lex():
            if token_type == TokenType.ERROR:
                print("error token: ", token, token_type, filename, line, file=sys.stderr)
                continue

            token = prefix + token.encode()

            if token_type != TokenType.IDENTIFIER:
                continue

            # We only index CONFIG_??? in makefiles
            config_or_not_makefile = family != 'M' or token.startswith(b'CONFIG_')
            if config_or_not_makefile:
                if token in idents:
                    idents[token].append(line)
                else:
                    idents[token] = [line]

        out.append((idx, family, idents))
    return out


def update_references(triple_chunks):
    '''Lex references in the pool forked at startup; gate on definitions
    and write db.refs from this thread.

    Lexing is pure Python and GIL-bound, so it runs in process workers.
    Forking a fresh pool per tag measured at 88.8% of the refs phase:
    the parent's page tables grow with the database caches. The pool is
    therefore created once, before any phase runs. Workers forked that
    early cannot inherit the defs state, so they return every
    identifier token and this thread applies the gate, which the
    sequential phases keep consistent. Results arrive in chunk order,
    so the writes are deterministic and single-threaded (no refs_lock
    needed).
    '''
    global defs_keys
    defs_keys = set(db.defs.get_keys())
    done = 0
    for chunk in refs_pool.imap(_refs_lex_chunk, triple_chunks):
        for idx, family, idents in chunk:
            for ident, lines in idents.items():
                if ident not in defs_keys:
                    continue

                lines = [line for line in lines
                         if defs_idxes.get(idx*idx_key_mod + line) != ident]
                if not lines:
                    continue
                lines = ','.join(str(line) for line in lines)

                if db.refs.exists(ident):
                    obj = db.refs.get(ident)
                else:
                    obj = data.RefList()

                obj.append(idx, lines, family)
                db.refs.put(ident, obj)
            done += 1
            if done % 10 == 0: progress('refs: chunk %d/%d' % (done, len(triple_chunks)))


def update_doc_comments(idxs):
    for idx in idxs:
        if idx % 1000 == 0: progress('docs: ' + str(idx))

        hash = db.hash.get(idx)
        filename = db.file.get(idx)

        family = lib.getFileFamily(filename)
        if family in [None, 'M']: continue

        lines = scriptLines('parse-docs', hash, filename)
        for l in lines:
            ident, line = l.split(b' ')
            line = int(line.decode())

            with docs_lock:
                if db.docs.exists(ident):
                    obj = db.docs.get(ident)
                else:
                    obj = data.RefList()

                obj.append(idx, str(line), family)
                db.docs.put(ident, obj)


def update_compatibles(idxs):
    for idx in idxs:
        if idx % 1000 == 0: progress('comps: ' + str(idx))

        hash = db.hash.get(idx)
        filename = db.file.get(idx)

        family = lib.getFileFamily(filename)
        if family in [None, 'K', 'M']: continue

        lines = compatibles_parser.run(scriptLines('get-blob', hash), family)
        comps = {}
        for l in lines:
            ident, line = l.split(' ')

            if ident in comps:
                comps[ident] += ',' + str(line)
            else:
                comps[ident] = str(line)

        with comps_lock:
            for ident, lines in comps.items():
                if db.comps.exists(ident):
                    obj = db.comps.get(ident)
                else:
                    obj = data.RefList()

                obj.append(idx, lines, family)
                db.comps.put(ident, obj)


def update_compatibles_bindings(idxs):
    for idx in idxs:
        if idx % 1000 == 0: progress('comps_docs: ' + str(idx))

        if not idx in bindings_idxes: # Parse only bindings doc files
            continue

        hash = db.hash.get(idx)

        family = 'B'
        lines = compatibles_parser.run(scriptLines('get-blob', hash), family)
        comps_docs = {}
        for l in lines:
            ident, line = l.split(' ')

            if db.comps.exists(ident):
                if ident in comps_docs:
                    comps_docs[ident] += ',' + str(line)
                else:
                    comps_docs[ident] = str(line)

        with comps_docs_lock:
            for ident, lines in comps_docs.items():
                if db.comps_docs.exists(ident):
                    obj = db.comps_docs.get(ident)
                else:
                    obj = data.RefList()

                obj.append(idx, lines, family)
                db.comps_docs.put(ident, obj)


# Main

if len(argv) >= 2 and argv[1].isdigit():
    num_threads = max(1, int(argv[1]))

project = lib.currentProject()

tag_buf = []
for tag in scriptLines('list-tags'):
    if not db.vers.exists(tag):
        tag_buf.append(tag)

num_tags = len(tag_buf)

print(project + ' - found ' + str(num_tags) + ' new tags')

if not num_tags:
    # Backward-compatibility: generate defs caches if they are empty.
    if db.defs_cache['C'].db.stat()['nkeys'] == 0:
        generate_defs_caches()
    exit(0)

# Fork the refs pool before any phase runs: the parent only grows from
# here (database caches fill up) and fork cost follows its page tables.
# The workers are stateless, so an early fork loses nothing.
refs_pool = multiprocessing.get_context('fork').Pool(num_threads)

def index_tag(tag):
    '''Index one tag: every phase runs for it before the next tag
    starts, so db.vers records progress tag by tag.'''
    # Per-tag state, so each tag starts clean
    file_paths.clear()
    bindings_idxes.clear()
    defs_idxes.clear()

    # Phase 1: assign idx numbers to the tag's new blobs
    idxes = update_blob_ids(tag)
    progress('ids: ' + tag.decode() + ': ' + str(len(idxes)) + ' new blobs')

    # Phase 2: versions
    update_versions(tag)

    # Phase 3: definitions, doc comments, compatibles
    work = list(chunks(idxes))
    parallel(update_definitions, work)
    parallel(update_doc_comments, work)
    if dts_comp_support:
        parallel(update_compatibles, work)

    # Phase 4: references (needs all definitions)
    # The refs pool was forked at startup, before this tag's maps
    # existed; the gate runs in the parent. Each worker process owns
    # its own persistent cat-file --batch pipe.
    triple_chunks = [[(idx, file_paths[idx].decode(), db.hash.get(idx)) for idx in chunk]
                     for chunk in work]
    update_references(triple_chunks)

    # Phase 5: compatibles from bindings documentation (needs all comps)
    if dts_comp_support:
        parallel(update_compatibles_bindings, work)

    progress('done: ' + tag.decode())

for tag in tag_buf:
    index_tag(tag)

refs_pool.terminate()
refs_pool.join()

generate_defs_caches()
