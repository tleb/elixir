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
# refs cannot race defs of another tag. Before the phases run, one
# blocking walk lists every new tag's blobs up front into a packed
# scratch file and counts the new blobs: indexing then reads its blob
# lists from disk (memory stays flat however many tags remain) and the
# run's remaining work is known exactly from the first tag line. The
# phases for one tag:
#   1. ids:    assign idx numbers to the tag's new blobs
#   2. vers:   collect the tag's blob paths (fills file_paths,
#      bindings_idxes); the db.vers entry itself is only committed
#      after phase 5, as the tag's completion marker
#   3. defs, docs, comps: per-blob indexing independent of each other
#   4. refs:   references, gated on definitions existing in the database
#   5. comps_docs: compatibles from DT bindings docs, gated on comps
#
# A run interrupted during phases 3 to 5 is detected on the next start
# through a currentTag marker in variables.db and refused: partial
# defs/refs entries cannot be rolled back, so the data directory has
# to be rebuilt rather than silently kept incomplete. Interruptions
# during phases 1 and 2 need no marker, both are safe to rerun: no
# uncommitted cross-references exist then, and the sync-at-boundary
# commit regime (marker durable first, content durable before the
# completion that vouches for it) plus a startup consistency check
# turns whatever partial flush SIGKILL still left into a refusal.
#
# Phases are sequential: a phase starts only after the previous one
# finished, so data written by one phase is visible to the next without
# locking. Inside a phase, work units (chunks of blobs) run in a thread
# pool; refs and docs lex/scan in a process pool instead (pure Python,
# GIL-bound), forked once at startup while the parent is still small,
# because fork cost grows with the parent's page tables as the caches
# fill. The databases are opened with DB_THREAD, so plain concurrent
# accesses are safe; only read-modify-write cycles on a key are not, and
# each has a dedicated lock, held around the cycle only, never around
# lexing or subprocess calls.
#
# Logging: the parent process is the single writer. Every line carries
# a [HH:MM:SS] wall-clock timestamp; workers and pool processes never
# print — they return their counters (lexer errors, captured ctags
# stderr) inside their results and the parent aggregates them, so
# parallel prints can no longer interleave mid-line. Progress inside a
# phase is driven by the parent's consumption of results, throttled to
# one line per progress_min_interval; the run ends with a human summary
# and one machine-parseable SUMMARY line (JSON) for post-hoc phase-time
# analysis. ELIXIR_LOG_VERBOSE=1 additionally dumps the captured ctags
# stderr and the first lexer-error samples before the summary.

import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from sys import argv
from threading import Lock

from elixir.lexers import TokenType
from elixir.lexers.fastscan import get_scanner
from elixir import repo
from elixir import parse
import elixir.lib as lib
import elixir.data as data
from elixir.data import PathList
from elixir.project_utils import get_lexer
from find_compatible_dts import FindCompatibleDTS

project = lib.currentProject()

dts_comp_support = int(project in repo.DTS_COMP_SUPPORT)

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
refs_lock = Lock() # db.refs
comps_lock = Lock() # db.comps
comps_docs_lock = Lock() # db.comps_docs


executor = None # Created on first use; its threads live for the whole run

def parallel(fn, items):
    '''Run fn on every item with the thread pool, yielding each result
    as the caller consumes it, so the caller drives progress from
    completion and surfaces failures (a worker exception propagates
    from the generator, as it did from the old list()).'''
    global executor
    if not items: return
    # One executor for the whole run. Threads cache per-thread state
    # (the cat-file --batch pipes below) that a phase-scoped executor
    # would leak: its threads die with it and the pipes stay open.
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=num_threads)
    yield from executor.map(fn, items)


def chunks(idxs):
    # Never hand one worker more than a num_threads-th of the blobs:
    # most tags add fewer blobs than chunk_size * num_threads, and one
    # big chunk would leave the other workers idle for the whole phase.
    size = min(chunk_size, max(1, -(-len(idxs) // num_threads)))
    for i in range(0, len(idxs), size):
        yield idxs[i:i+size]


# ---- Logging: everything below prints from the parent only ----

log_verbose = os.environ.get('ELIXIR_LOG_VERBOSE') == '1'

progress_min_interval = 5.0 # seconds between in-phase progress lines

# The phases of one tag, in execution order; the SUMMARY line always
# carries all of them, 0.0 for the ones a project does not run
phases = ('ids', 'vers', 'defs', 'docs', 'comps', 'refs', 'comps_docs')

# Run-wide aggregates, written by the parent as it consumes results
run_phase_s = dict.fromkeys(phases, 0.0)
lexer_errors = 0
lexer_error_samples = [] # the first ERROR tokens, verbose mode only
max_samples = 10
ctags_notices = 0
ctags_stderr = [] # captured ctags stderr, verbose mode only

def log(msg):
    '''One timestamped line, flushed: the only print in the run'''
    print(datetime.now().strftime('[%H:%M:%S] ') + msg, flush=True)

def fmt_secs(s):
    '''A duration: tenths of a second under a minute, else m+s'''
    if s < 60:
        return '%.1fs' % s
    m, sec = divmod(int(round(s)), 60)
    return '%dm%02ds' % (m, sec)

def fmt_eta(s):
    '''An ETA: whole seconds, floored at zero'''
    s = max(0, int(s))
    if s < 60:
        return '%ds' % s
    return '%dm%02ds' % divmod(s, 60)

def collect_ctags_stderr(stderr):
    '''Count a worker's captured ctags notices; keep the text only in
    verbose mode'''
    global ctags_notices
    if not stderr:
        return
    ctags_notices += stderr.count(b'ctags: Notice:')
    if log_verbose:
        ctags_stderr.append(stderr)

def count_lexer_errors(count, samples):
    '''Aggregate a worker's lexer-error counter; keep the first samples'''
    global lexer_errors
    lexer_errors += count
    if len(lexer_error_samples) < max_samples:
        lexer_error_samples.extend(samples[:max_samples - len(lexer_error_samples)])

class PhaseProgress:
    '''In-phase progress, driven by the parent's consumption of phase
    results (never from the workers): at most one line per
    progress_min_interval of phase time, and a final 100% line once any
    line was printed — a phase shorter than the interval prints nothing
    at all. The ETA is the remaining blobs over the average rate so far,
    approximate like every ETA.'''
    def __init__(self, tag, phase, total):
        self.ctx = project + ' ' + tag.decode() + ' ' + phase
        self.total = total
        self.done = 0
        self.start = time.monotonic()
        self.last = self.start

    def update(self, done):
        self.done = done
        t = time.monotonic()
        final = self.done >= self.total
        if not final and t - self.last < progress_min_interval:
            return
        if final and self.last == self.start:
            return # nothing was shown: a short phase stays silent
        rate = self.done / (t - self.start)
        msg = ('%s %d%% %d/%d blobs, %d blobs/s'
               % (self.ctx, self.done * 100 // self.total, self.done,
                  self.total, round(rate)))
        if not final:
            msg += ', ETA ' + fmt_eta((self.total - self.done) / rate)
        log(msg)
        self.last = t

class PhaseTimer:
    '''Times one phase into the tag's dict and the run-wide totals'''
    def __init__(self, times, name):
        self.times = times
        self.name = name
    def __enter__(self):
        self.start = time.monotonic()
    def __exit__(self, *exc):
        seconds = time.monotonic() - self.start
        self.times[self.name] = seconds
        run_phase_s[self.name] += seconds


def update_blob_ids(blobs):
    if db.vars.exists('numBlobs'):
        idx = db.vars.get('numBlobs')
    else:
        idx = 0

    new_idxes = []
    for hash, filename, path in blobs:
        if not db.blob.exists(hash):
            db.blob.put(hash, idx)
            db.hash.put(idx, hash)
            db.file.put(idx, filename)

            new_idxes.append(idx)
            idx += 1
    db.vars.put('numBlobs', idx)
    return new_idxes


def update_versions(blobs):
    '''Collect the tag's blob paths into a PathList; the caller
    commits it to db.vers once every phase of the tag has run, so a
    tag listed in db.vers is fully indexed.'''
    buf = []

    for hash, filename, path in blobs:
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

    return obj


def generate_defs_caches():
    for key in db.defs.get_keys():
        value = db.defs.get(key)
        for family in ['C', 'K', 'D', 'M']:
            if (lib.compatibleFamily(value.get_families(), family) or
                        lib.compatibleMacro(value.get_macros(), family)):
                db.defs_cache[family].put(key, b'')


def update_definitions(idxs):
    '''One chunk of the defs phase, in a pool thread: writes db.defs,
    returns the chunk's captured ctags stderr (byte string) instead of
    printing anything'''
    stderr_out = []
    for idx in idxs:
        hash = db.hash.get(idx)
        filename = db.file.get(idx)

        family = lib.getFileFamily(filename)
        if family in [None, 'M']: continue

        # One ctags subprocess (parse.parse_defs), the blob fetched
        # through the batch reader
        lines = parse.parse_defs(repo.get_blob(hash), filename, family,
                                 stderr_out)

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

    return b''.join(stderr_out)


def _refs_lex_chunk(triples):
    '''Lex a chunk of (idx, path, hash); return ([(idx, family,
    idents)], lexer error count, first error samples).

    idents maps every identifier token of the blob to the list of its
    line numbers. Workers carry no per-tag state, so the pool can be
    forked once at startup and live across tags; gating on definitions
    happens in the parent, which owns the defs state. Error tokens are
    counted and sampled here, never printed: pool processes do not
    write to the log.

    The C, Kconfig and makefile families lex through the fast
    scanners (elixir/lexers/fastscan.py), which emit exactly what the
    simple_lexer-based lexers emit for identifiers, errors and samples;
    everything else (DTS, gas) keeps the lexer objects.
    '''
    out = []
    errors = 0
    samples = []
    for idx, filename, hash in triples:
        # getFileFamily expects a basename; the name-based families
        # (kconfig*, makefile*) must match in subdirectories too
        family = lib.getFileFamily(os.path.basename(filename))
        if family == None: continue

        scanner = get_scanner(filename, project)
        if scanner is None:
            lexer = get_lexer(filename, project)
            if lexer is None:
                continue

        try:
            code = repo.get_blob(hash).decode()
        except UnicodeDecodeError:
            code = repo.get_blob(hash).decode('raw_unicode_escape')

        if scanner is not None:
            idents, chunk_errors, error_tokens = scanner(
                code, max_samples - len(samples))
            errors += chunk_errors
            samples.extend((token, filename, line)
                           for token, line in error_tokens)
            out.append((idx, family, idents))
            continue

        prefix = b''
        # Kconfig values are saved as CONFIG_<value>
        if family == 'K':
            prefix = b'CONFIG_'

        idents = {}
        for token_type, token, _, line in lexer(code).lex():
            if token_type == TokenType.ERROR:
                errors += 1
                if len(samples) < max_samples:
                    samples.append((token, filename, line))
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
    return out, errors, samples


def update_references(triple_chunks, progress):
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
    for chunk, (lexed, errors, samples) in zip(
            triple_chunks, refs_pool.imap(_refs_lex_chunk, triple_chunks)):
        count_lexer_errors(errors, samples)
        for idx, family, idents in lexed:
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
        done += len(chunk)
        progress.update(done)


def _docs_chunk(triples):
    '''Docs work for one chunk of (idx, filename, hash), in a pool
    worker: the /** gate, the temp files, one chunked ctags and the
    scan. Workers carry no per-tag state, like the refs workers; the
    blob is fetched through the worker's own batch reader. Returns
    ([(idx, family, lines) per parsed blob], the chunk's captured
    ctags stderr): pool processes do not print, the parent aggregates
    what they return'''
    metas = []
    blobs = []
    for idx, filename, hash in triples:
        family = lib.getFileFamily(filename)
        if family in [None, 'M']: continue

        metas.append((idx, family))
        blobs.append(repo.get_blob(hash))

    stderr_out = []
    lines = parse.parse_doc_comments_chunk(blobs, stderr_out)
    return ([(idx, family, out) for (idx, family), out in zip(metas, lines)],
            b''.join(stderr_out))

def update_doc_comments(triple_chunks, progress):
    '''Doc comments in the pool forked at startup, like refs: the scan
    around the ctags output is pure Python and GIL-bound, so it runs
    in process workers while the pool idles between the defs and
    refs phases (the ctags waits release the GIL, which is why defs
    stays on threads). Results arrive in chunk order, so the writes
    are deterministic and single-threaded (no docs_lock needed,
    like refs).'''
    done = 0
    for chunk, (parsed, stderr) in zip(
            triple_chunks, refs_pool.imap(_docs_chunk, triple_chunks)):
        collect_ctags_stderr(stderr)
        for idx, family, lines in parsed:
            for l in lines:
                ident, line = l.split(b' ')
                line = int(line.decode())

                if db.docs.exists(ident):
                    obj = db.docs.get(ident)
                else:
                    obj = data.RefList()

                obj.append(idx, str(line), family)
                db.docs.put(ident, obj)
        done += len(chunk)
        progress.update(done)


def update_compatibles(idxs):
    '''One chunk of the comps phase, in a pool thread: writes db.comps.
    Nothing to return: the phase produces no worker-side counters.'''
    for idx in idxs:
        hash = db.hash.get(idx)
        filename = db.file.get(idx)

        family = lib.getFileFamily(filename)
        if family in [None, 'K', 'M']: continue

        lines = compatibles_parser.run(repo.get_blob_lines(hash), family)
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
    '''One chunk of the comps_docs phase, in a pool thread: writes
    db.comps_docs. Like update_compatibles, nothing to return.'''
    for idx in idxs:
        if not idx in bindings_idxes: # Parse only bindings doc files
            continue

        hash = db.hash.get(idx)

        family = 'B'
        lines = compatibles_parser.run(repo.get_blob_lines(hash), family)
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


# The vars handle decodes every value as an int (numBlobs), so the
# in-flight tag marker rides the key, not the value.
current_tag_prefix = b'currentTag:'

def current_tag():
    '''The tag the previous run was working on when it died, if any'''
    for key in db.vars.get_keys():
        if key.startswith(current_tag_prefix):
            return key[len(current_tag_prefix):]

# Main

if len(argv) >= 2 and argv[1].isdigit():
    num_threads = max(1, int(argv[1]))

run_start = time.monotonic()

# Scratch leftovers of a crashed run's upfront walk (advisory garbage
# only, never read): sweep them before anything else, notably before
# the interrupted-run marker logic below, so a refused data directory
# does not keep them around either.
data_dir = lib.getDataDir()
for name in os.listdir(data_dir):
    if name.startswith('tmp-blobwalk-'):
        os.remove(os.path.join(data_dir, name))

# Cheap structural invariant (stat() is O(1) per database): blob,
# hash and file are written one-to-one in phase 1, numBlobs at its
# end, so equal counts mean a rerun can trust the registered blob
# set. A SIGKILL mid-phase-1 loses an arbitrary subset of those
# unsynced records (each rides its own handle's cache) and the counts
# come back unequal — refuse rather than let a rerun index on top of
# a partial registration. The numBlobs comparison also pins the tag
# boundary's sync order: numBlobs only reaches disk with the marker
# (one database), so counts catching up to it mid-sync is what a
# crash between the two syncs looks like. Residual risk: count
# equality can miss same-cardinality corruption (a lost blob record
# whose idx survives elsewhere) — a far narrower class than the
# partial flushes caught here. Absent numBlobs means phase 1 never
# ran: expect zero.
counts = (len(db.blob), len(db.hash), len(db.file))
num_blobs = db.vars.get('numBlobs') if db.vars.exists('numBlobs') else 0
if len(set(counts)) > 1 or num_blobs != counts[0]:
    print(project + ' - the data directory is inconsistent (blobs '
          + str(counts[0]) + ', hashes ' + str(counts[1]) + ', filenames '
          + str(counts[2]) + ', numBlobs ' + str(num_blobs)
          + '): likely from a crashed run; rebuild it before updating again',
          file=sys.stderr)
    exit(1)

tag_buf = []
for tag in repo.list_tags(lib.getRepoDir(), project):
    if not db.vers.exists(tag):
        tag_buf.append(tag)

# Refuse a data directory left by a run interrupted mid-tag: its blob
# ids are registered and its defs/refs are partially written, and a
# rerun would either skip the tag (it counts as new, but phase 1 sees
# no new blobs) or duplicate the entries already written. Neither is
# acceptable, so the only sound recovery is a rebuild.
interrupted = current_tag()
if interrupted is not None:
    if db.vers.exists(interrupted):
        # The tag finished after all; only the marker cleanup was
        # lost to the crash.
        db.vars.delete(current_tag_prefix + interrupted)
    else:
        print(project + ' - the data directory is from an interrupted run:'
              + ' tag ' + interrupted.decode() + ' is partially indexed and'
              + ' cannot be resumed; rebuild it before updating again',
              file=sys.stderr)
        exit(1)

num_tags = len(tag_buf)

log('%s: %d new tags (repo %s, data %s, %d threads)'
    % (project, num_tags, lib.getRepoDir(), lib.getDataDir(), num_threads))

if not num_tags:
    # Backward-compatibility: generate defs caches if they are empty.
    if db.defs_cache['C'].db.stat()['nkeys'] == 0:
        generate_defs_caches()
    exit(0)

# Fork the refs pool before any phase runs: the parent only grows from
# here (the walk below, then filling database caches) and fork cost
# follows its page tables. The workers are stateless, so an early
# fork loses nothing.
refs_pool = multiprocessing.get_context('fork').Pool(num_threads)

# ---- Walk: every tag's blobs, up front, on disk ----
# One blocking pass before any indexing: list each tag's blobs into a
# packed scratch file and count the new blobs (first walk sighting AND
# absent from db.blob; indexing only starts after, so db.blob equals
# its start state for the whole walk and no snapshot is needed). The
# counts buy the exact-from-tag-1 run ETA; the packed file buys flat
# memory and the same per-tag blob order the lazy calls produced.
lists_path = os.path.join(data_dir, 'tmp-blobwalk-lists')
seen_path = os.path.join(data_dir, 'tmp-blobwalk-seen.db')
blob_lists = repo.BlobLists(lists_path)
seen = data.BsdDB(seen_path, False, lambda x: x)

walked_blobs = 0
total_new = 0
walk_start = time.monotonic()
last_walk_log = walk_start
for n, tag in enumerate(tag_buf, 1):
    blobs = repo.list_blobs(lib.getRepoDir(), tag)
    blob_lists.add(tag, blobs)
    for hash, _, path in blobs:
        if seen.put_new(hash, b'') and not db.blob.exists(hash):
            total_new += 1
    walked_blobs += len(blobs)
    t = time.monotonic()
    if n < num_tags and t - last_walk_log >= progress_min_interval:
        log('walking tags %d/%d …' % (n, num_tags))
        last_walk_log = t
walk_s = time.monotonic() - walk_start
log('walk done: %d tags, %d blobs walked, %d new, %s'
    % (num_tags, walked_blobs, total_new, fmt_secs(walk_s)))
seen.close() # its work ends with the walk; both files go at run end

# The run ETA's time base: indexing, not the walk before it
index_start = time.monotonic()

def parallel_phase(name, fn, work, tag, times):
    '''One thread-pool phase under its timer: the parent consumes the
    workers' results as they arrive (aggregating whatever counters
    they return) and prints all progress itself.'''
    prog = PhaseProgress(tag, name, sum(len(chunk) for chunk in work))
    with PhaseTimer(times, name):
        done = 0
        for chunk, res in zip(work, parallel(fn, work)):
            collect_ctags_stderr(res)
            done += len(chunk)
            prog.update(done)

def index_tag(tag):
    '''Index one tag: every phase runs for it before the next tag
    starts, and db.vers records it only after the last phase. Returns
    (per-phase seconds, new blob count).'''
    # Per-tag state, so each tag starts clean
    file_paths.clear()
    bindings_idxes.clear()
    defs_idxes.clear()

    # One walk over the tag's blobs feeds every phase below: read
    # back from the upfront walk's packed file, same triples in the
    # same ls-tree order it captured
    blobs = blob_lists.get(tag)

    times = {}

    # Phase 1: assign idx numbers to the tag's new blobs
    with PhaseTimer(times, 'ids'):
        idxes = update_blob_ids(blobs)

    # Phase 2: versions - collect the paths, commit after phase 5
    with PhaseTimer(times, 'vers'):
        vers_obj = update_versions(blobs)

    # From here on the phases write defs, docs, comps and refs, which
    # cannot be rolled back: mark the tag as in-flight so that a run
    # interrupted past this point is detected and refused on the next
    # start instead of silently keeping a partial database.
    #
    # SIGKILL dies with BDB's process cache, so durability here is a
    # two-phase commit and the order of every step is load-bearing:
    # the marker goes durable FIRST and its sync drags numBlobs with
    # it (same database, one cache), then the phase 1-2 records catch
    # up under its protection. A kill in between leaves numBlobs
    # ahead of the blob/hash/file counts and the startup check
    # refuses; with the syncs the other way around, a kill between
    # them leaves equal counts and NO marker, and the rerun commits
    # the tag with zero new blobs — without its defs. A marker that
    # outlives what it guards only costs a needless rebuild.
    db.vars.put(current_tag_prefix + tag, b'1', sync=True)
    db.sync_all()

    # Phase 3: definitions, doc comments, compatibles
    work = list(chunks(idxes))
    parallel_phase('defs', update_definitions, work, tag, times)
    with PhaseTimer(times, 'docs'):
        update_doc_comments(
            [[(idx, db.file.get(idx), db.hash.get(idx))
              for idx in chunk] for chunk in work],
            PhaseProgress(tag, 'docs', len(idxes)))
    if dts_comp_support:
        parallel_phase('comps', update_compatibles, work, tag, times)

    # Phase 4: references (needs all definitions)
    # The refs pool was forked at startup, before this tag's maps
    # existed; the gate runs in the parent. Each worker process owns
    # its own persistent cat-file --batch pipe.
    triple_chunks = [[(idx, file_paths[idx].decode(), db.hash.get(idx)) for idx in chunk]
                     for chunk in work]
    with PhaseTimer(times, 'refs'):
        update_references(triple_chunks, PhaseProgress(tag, 'refs', len(idxes)))

    # Phase 5: compatibles from bindings documentation (needs all comps)
    if dts_comp_support:
        parallel_phase('comps_docs', update_compatibles_bindings, work, tag, times)

    # Commit the tag: only now is it fully indexed. Same rule as the
    # marker write, other end of the tag: the phases 3-5 writes go
    # durable first, then the completion marker (the vers entry),
    # then the in-flight marker is dropped after it (delete has no
    # sync kwarg, so an explicit sync flushes it). Committing vers
    # before the content would let a kill during the NEXT tag's
    # phases 1-2 lose this tag's defs to the cache while the rerun
    # skips the tag as done; the crash drills caught exactly that.
    db.sync_all()
    db.vers.put(tag, vers_obj, sync=True)
    db.vars.delete(current_tag_prefix + tag)
    db.vars.sync()

    return times, len(idxes)

done_tags = 0
done_blobs = 0

for tag in tag_buf:
    times, blobs = index_tag(tag)
    done_tags += 1
    done_blobs += blobs

    # Tag completion line with per-phase seconds and, while tags
    # remain, the run's remaining work: the upfront walk counted the
    # new blobs, so "blobs left" is a fact and the ETA only carries
    # the cumulative rate's noise, exact from the first tag on
    msg = ('%s %s (%d/%d): %d blobs, %s (%s)'
           % (project, tag.decode(), done_tags, num_tags, blobs,
              fmt_secs(sum(times.values())),
              ' '.join('%s %s' % (p, fmt_secs(times[p]))
                       for p in ('defs', 'docs', 'comps', 'refs', 'comps_docs')
                       if p in times)))
    if done_tags < num_tags and done_blobs:
        rate = done_blobs / (time.monotonic() - index_start)
        left = total_new - done_blobs
        msg += ' — %d blobs/s, %d blobs left, ETA %s' % (
            round(rate), left, fmt_eta(left / rate))
    log(msg)

refs_pool.terminate()
refs_pool.join()

# The walk's scratch artifacts live only for the run; a crash leaves
# them, and the next start sweeps them
blob_lists.close()
os.remove(lists_path)
os.remove(seen_path)

generate_defs_caches()

wall = time.monotonic() - run_start

# Verbose detail first, so the summary stays the last thing in the log
if log_verbose:
    for token, filename, line in lexer_error_samples:
        log('%s: lexer error token %r at %s:%d'
            % (project, token, filename, line))
    for stderr in ctags_stderr:
        for line in stderr.decode('utf-8', 'replace').splitlines():
            log('%s: ctags: %s' % (project, line))

# The human summary: where the run's time went
phase_total = sum(run_phase_s.values())
log('%s: %d tags, %d new blobs, %s wall'
    % (project, num_tags, done_blobs, fmt_secs(wall)))
log('%s: phases: %s'
    % (project, ' '.join(
        '%s %s (%d%%)' % (p, fmt_secs(run_phase_s[p]),
                          round(run_phase_s[p] * 100 / phase_total) if phase_total else 0)
        for p in phases)))
log('%s: %d lexer errors, %d ctags notices'
    % (project, lexer_errors, ctags_notices))

# The machine interface: one valid-JSON line for post-hoc analysis
log('SUMMARY ' + json.dumps({
    'project': project,
    'tags': num_tags,
    'blobs': done_blobs,
    'walk_s': round(walk_s, 3),
    'walked_blobs': walked_blobs,
    'total_new': total_new,
    'wall_s': round(wall, 3),
    'blobs_per_s': round(done_blobs / wall, 3),
    'phases': {p: round(run_phase_s[p], 3) for p in phases},
    'lexer_errors': lexer_errors,
    'ctags_notices': ctags_notices,
}))
