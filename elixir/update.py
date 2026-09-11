#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#                           Maxime Chretien <maxime.chretien@bootlin.com>
#                           and contributors
#
#  Elixir is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
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
# blocking walk lists every new tag's blobs up front into packed
# scratch segments (one per worker range) and counts the new blobs:
# indexing then reads its blob lists from disk (memory stays flat
# however many tags remain) and the run's remaining work is known
# exactly from the first tag line. The
# phases for one tag:
#   1. ids:    assign idx numbers to the tag's new blobs
#   2. vers:   the tag's blob paths, into version_objects
#   3. defs, docs, comps: per-blob indexing independent of each other
#   4. refs:   references, gated on definitions existing in the database
#   5. comps_docs: compatibles from DT bindings docs, gated on comps
#
# Storage is DuckDB (elixir/data_duckdb.py), one file per project. A
# PER-TAG TRANSACTION is the unit of crash safety: it opens at the start
# of the tag, carries every phase's inserts, and commits only when the
# tag is fully indexed — the versions row rides the same commit, so a
# tag present in the database is always complete. A SIGKILL rolls the
# interrupted tag back to the last committed one (the WAL replays), and
# the next run simply resumes there: pending tags are the ones absent
# from versions, and the dense counters (blobid, versionid, identid)
# restart where the committed state left them, so a resumed build ends
# logically identical to a clean one. The whole BDB-era crash machinery
# — the currentTag marker, sync-at-boundary commits, the startup count
# check, refuse-and-rebuild — is gone; transactions make it moot.
#
# Workers never touch the database (DuckDB allows one writer process).
# Each work unit writes its rows to an Arrow IPC scratch file
# (tmp-arrow-<phase>-<tag>-<chunk>.arrow in the data dir, swept at
# startup, deleted the moment it is ingested); the parent STREAMS the
# ingestion: whenever enough scratch has piled up (and once more
# at phase end) it runs their SQL as one dataset, while the pool's
# workers keep crunching later chunks — the SQL mostly hides under
# worker time instead of idling the pool at a barrier — and
# everything that was per-row Python over BDB — hash dedup, identid
# assignment, the refs defs-gate, the def-line self-reference
# suppression, family bitmasks — happens in SQL. Every scratch row
# carries a seq (chunk index and row index packed into one integer):
# file-scan order is not guaranteed, so determinism hangs on ordering
# by seq, never on arrival — and imap hands results back in chunk
# order, so ingesting consecutive arrivals in order reproduces the
# batched ingest's identid minting exactly.
#
# Phases are sequential: a phase starts only after the previous one
# finished, so data written by one phase is visible to the next. Inside
# a phase, work units (chunks of blobs) all run on the one process pool
# run() spawns before the parent opens its DuckDB connection (an
# ordering choice, not a correctness requirement — see run()). The
# workers are SPAWNED, not forked: they share nothing with the parent
# (no inherited DuckDB threads, no inherited globals), so every input
# they need — the project, the repo and data dirs — arrives in their
# item tuples, at the cost of one import of this module per worker per
# run. This module is therefore importable with no side effects: the
# whole run is run(), called once per project.
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
import time
from datetime import datetime

import pyarrow as pa
import pyarrow.dataset as pads

import elixir.lib as lib
from elixir import data_duckdb as dd
from elixir import parse, repo
from elixir.find_compatible_dts import FindCompatibleDTS
from elixir.lexers import TokenType
from elixir.lexers.fastscan import get_scanner
from elixir.project_utils import get_lexer


class UpdateError(Exception):
    '''A run() failure with a message fit for the log: a bad project
    layout, a missing directory, an invariant violation'''


chunk_size = 256 # Max blobs per work unit; chunks() caps it further

# All cores, always: the phases below are CPU-bound Python (lexing,
# ctags-output parsing, compatible scanning) and the pool parallelizes
# them for real; the historical thread-count argument is gone.
num_threads = os.cpu_count() or 1

log_verbose = os.environ.get('ELIXIR_LOG_VERBOSE') == '1'

progress_min_interval = 5.0 # seconds between in-phase progress lines

max_samples = 10 # lexer-error samples kept for the verbose dump

# The phases of one tag, in execution order; the SUMMARY line always
# carries all of them, 0.0 for the ones a project does not run
phases = ('ids', 'vers', 'defs', 'docs', 'comps', 'refs', 'comps_docs')

compatibles_parser = FindCompatibleDTS()

# Cross-phase state of one run, carried by run() as locals: the dicts
# below are written in one phase, read in later phases (they were
# module globals when this was a script):
#   new_hashes:     idx -> blob hash (ids -> every later phase)
#   new_filenames:  idx -> filename (ids -> defs, docs, comps, refs)
#   file_paths:     idx -> path (vers -> refs, comps_docs)
#   bindings_idxes: DT bindings documentation files (vers -> comps_docs)


def chunks(idxs):
    # Never hand one worker more than a num_threads-th of the blobs:
    # most tags add fewer blobs than chunk_size * num_threads, and one
    # big chunk would leave the other workers idle for the whole phase.
    size = min(chunk_size, max(1, -(-len(idxs) // num_threads)))
    for i in range(0, len(idxs), size):
        yield idxs[i:i+size]


# ---- Logging: everything below prints from the parent only ----

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

class PhaseProgress:
    '''In-phase progress, driven by the parent's consumption of phase
    results (never from the workers): at most one line per
    progress_min_interval of phase time, and a final 100% line once any
    line was printed — a phase shorter than the interval prints nothing
    at all. The ETA is the remaining blobs over the average rate so far,
    approximate like every ETA.'''
    def __init__(self, project, tag, phase, total):
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
    '''Times one span into the tag's dict and the run-wide totals;
    repeated spans (a phase's per-chunk ingests) accumulate. totals is
    the run-wide dict to add to: run_phase_s for phase walls,
    run_ingest_s for the SQL share'''
    def __init__(self, times, name, totals):
        self.times = times
        self.name = name
        self.totals = totals
    def __enter__(self):
        self.start = time.monotonic()
    def __exit__(self, *_):
        seconds = time.monotonic() - self.start
        self.times[self.name] = self.times.get(self.name, 0.0) + seconds
        self.totals[self.name] += seconds


# ---- Arrow scratch: how workers hand rows to the parent's SQL ----

# One schema per phase's rows. seq orders first appearances (idents)
# and last-writer-wins (the def-line map): chunk index in the high
# bits, row index within the chunk in the low ones, so a scan of the
# files in any order still sees the workers' write order.
DEFS_SCHEMA = pa.schema([('seq', pa.uint64()), ('name', pa.string()),
                         ('blobid', pa.uint32()), ('defline', pa.uint32()),
                         ('deftype', pa.string()), ('family', pa.string())])
DOCS_SCHEMA = pa.schema([('seq', pa.uint64()), ('name', pa.string()),
                         ('blobid', pa.uint32()), ('line', pa.uint32()),
                         ('family', pa.string())])
REFS_SCHEMA = pa.schema([('name', pa.string()), ('blobid', pa.uint32()),
                         ('line', pa.uint32()), ('family', pa.string())])
WALK_SCHEMA = pa.schema([('blobhash', pa.binary())])

ipc_batch_rows = 65536

class IpcWriter:
    '''Streaming Arrow IPC file writer: rows appended in Python, flushed
    as RecordBatches, so a worker's or the walk's memory stays flat'''
    def __init__(self, path, schema):
        self.sink = pa.OSFile(path, 'wb')
        self.writer = pa.ipc.new_file(self.sink, schema)
        self.schema = schema
        self.buf = []

    def add(self, row):
        self.buf.append(row)
        if len(self.buf) >= ipc_batch_rows:
            self.flush()

    def flush(self):
        if self.buf:
            cols = {f.name: pa.array([row[i] for row in self.buf], type=f.type)
                    for i, f in enumerate(self.schema)}
            self.writer.write_batch(
                pa.RecordBatch.from_pydict(cols, schema=self.schema))
            self.buf = []

    def close(self):
        self.flush()
        self.writer.close()
        self.sink.close()

def scratch_path(data_dir, phase, tag_no, chunk_no):
    '''One work unit's scratch file: unique per (phase, tag, chunk), so
    parallel workers never share a writer'''
    return os.path.join(data_dir, 'tmp-arrow-%s-%04d-%04d.arrow'
                        % (phase, tag_no, chunk_no))

def write_chunk(path, schema, rows):
    '''A worker's whole-chunk output as one IPC file (a chunk of blobs'
    rows, never a phase's: bounded like chunk_size)'''
    w = IpcWriter(path, schema)
    for row in rows:
        w.add(row)
    w.close()

# The BDB write gates, ported to SQL: db.defs only ever held a key once
# DefList.append accepted a line, which dropped unknown deftypes, and
# update_definitions only created the DefList for lib.isIdent() names.
# The NAME filters gate defs rows and identid minting; rows with an
# unknown deftype are ingested too — the old world kept their defs key
# with an empty DefList ('ghost' keys: listed by acp, True for
# symbol_exists) — but they never display and never reach def_fams.
# UNfiltered scratch still feeds the def-line map, which — like the
# defs_idxes dict it ports — records every ctags line regardless.
# ctags' one-letter type codes -> the stored deftype names.
defTypeR = dd.defTypeR
valid_deftypes = dd.VALID_DEFTYPES
deftype_list = ', '.join("'%s'" % t for t in valid_deftypes)
ident_blacklist = [name.decode() for name in lib.blacklist]

# def_fams/macro_fams bits, as SQL: one CASE family -> bit
fam_bits = ' '.join("WHEN '%s' THEN %d" % (f, b)
                    for f, b in sorted(dd.FAM_BITS.items(), key=lambda x: x[1]))


# ---- The pool's work units ----
# Every worker function is module-level (picklable by reference) and
# takes ONE item tuple. The phase workers below take (phase, tag_no,
# chunk_no, triples, project, repo_dir, data_dir); the spawned
# workers share nothing with the parent, so the trailing three —
# everything the phases need beyond the chunk itself — travel with
# every item: project picks the refs scanners/lexers, repo_dir feeds
# repo.get_blob*'s cat-file pipes, data_dir is where the scratch file
# lands. The walk worker takes (range_no, tags, repo_dir, data_dir):
# the same dirs, a contiguous range of tags instead of a chunk.

def update_definitions(item):
    '''One chunk of the defs phase, in a pool process: ONE ctags per
    (chunk, family) over the chunk's blobs (parse_defs_chunk groups
    and preserves input order — the per-blob parse_defs wrapper would
    fork ctags once per blob, ~25s of pure exec overhead on a linux
    tag), every def line as one scratch row (the deftype letter
    mapped to its name, unknown letters kept for the def-line map and
    filtered at insert). Returns the chunk's captured ctags stderr and
    its scratch file, never printing anything'''
    phase, tag_no, chunk_no, triples, project, repo_dir, data_dir = item
    stderr_out = []
    metas = []
    items = []
    for idx, filename, hash in triples:
        family = lib.getFileFamily(filename)
        if family in [None, 'M']:
            continue

        metas.append((idx, family))
        items.append((repo.get_blob(repo_dir, hash), filename, family))

    lines_by_blob = parse.parse_defs_chunk(items, stderr_out)

    rows = []
    seq = chunk_no << 32
    for (idx, family), lines in zip(metas, lines_by_blob, strict=True):
        for ln in lines:
            ident, type, line = ln.split(b' ')
            type = type.decode()
            rows.append((seq, lib.decode(ident), idx, int(line.decode()),
                         defTypeR.get(type, type), family))
            seq += 1

    path = scratch_path(data_dir, phase, tag_no, chunk_no)
    write_chunk(path, DEFS_SCHEMA, rows)
    return b''.join(stderr_out), path


def _docs_chunk(item):
    '''Docs work for one chunk of (idx, filename, hash), in a pool
    worker: the /** gate, the temp files, one chunked ctags and the
    scan, then the rows to scratch. Returns (scratch file, the chunk's
    captured ctags stderr): pool processes do not print, the parent
    aggregates what they return'''
    phase, tag_no, chunk_no, triples, project, repo_dir, data_dir = item
    metas = []
    blobs = []
    for idx, filename, hash in triples:
        family = lib.getFileFamily(filename)
        if family in [None, 'M']:
            continue

        metas.append((idx, family))
        blobs.append(repo.get_blob(repo_dir, hash))

    stderr_out = []
    lines = parse.parse_doc_comments_chunk(blobs, stderr_out)

    rows = []
    seq = chunk_no << 32
    for (idx, family), out in zip(metas, lines, strict=True):
        for ln in out:
            ident, line = ln.split(b' ')
            rows.append((seq, lib.decode(ident), idx, int(line.decode()), family))
            seq += 1

    path = scratch_path(data_dir, phase, tag_no, chunk_no)
    write_chunk(path, DOCS_SCHEMA, rows)
    return path, b''.join(stderr_out)


def update_compatibles(item):
    '''One chunk of the comps phase, in a pool process: DT compatible
    strings of C and DTS blobs, as one scratch row per line (the old
    RefList joined the lines with commas; defs rows are per line).
    The ident is the URL-quoted string FindCompatibleDTS emits — the
    form query.py looks up. Nothing to return but the scratch file'''
    phase, tag_no, chunk_no, triples, project, repo_dir, data_dir = item
    rows = []
    seq = chunk_no << 32
    for idx, filename, hash in triples:
        family = lib.getFileFamily(filename)
        if family in [None, 'K', 'M']:
            continue

        for ln in compatibles_parser.run(repo.get_blob_lines(repo_dir, hash),
                                         family):
            ident, line = ln.split(' ')
            rows.append((seq, ident, idx, int(line), family))
            seq += 1

    path = scratch_path(data_dir, phase, tag_no, chunk_no)
    write_chunk(path, DOCS_SCHEMA, rows)
    return b'', path


def ident_str(tok):
    '''A name for the idents table, as str: lexer tokens are bytes,
    fast scanner keys already str (the old layer autoBytes-encoded
    both; the SQL join needs one representation)'''
    return tok if type(tok) is str else lib.decode(tok)

def _refs_lex_chunk(item):
    '''Lex a chunk of (idx, path, hash) into ALL identifier occurrences,
    as scratch rows — no gate, no filters: the defs gate and the
    def-line suppression happen in the parent's SQL, which the
    sequential phases keep consistent. Error tokens are counted and
    sampled here, never printed: pool processes do not write to the
    log. Returns (scratch file, error count, first error samples).

    The C, Kconfig and makefile families lex through the fast
    scanners (elixir/lexers/fastscan.py), which emit exactly what the
    simple_lexer-based lexers emit for identifiers, errors and samples;
    everything else (DTS, gas) keeps the lexer objects.
    '''
    phase, tag_no, chunk_no, triples, project, repo_dir, data_dir = item
    rows = []
    errors = 0
    samples = []
    for idx, filename, hash in triples:
        # getFileFamily expects a basename; the name-based families
        # (kconfig*, makefile*) must match in subdirectories too
        family = lib.getFileFamily(os.path.basename(filename))
        if family is None:
            continue

        scanner = get_scanner(filename, project)
        if scanner is None:
            lexer = get_lexer(filename, project)
            if lexer is None:
                continue

        try:
            code = repo.get_blob(repo_dir, hash).decode()
        except UnicodeDecodeError:
            code = repo.get_blob(repo_dir, hash).decode('raw_unicode_escape')

        if scanner is not None:
            idents, chunk_errors, error_tokens = scanner(
                code, max_samples - len(samples))
            errors += chunk_errors
            samples.extend((token, filename, line)
                           for token, line in error_tokens)
            for ident, lines in idents.items():
                for line in lines:
                    rows.append((ident_str(ident), idx, line, family))
            continue

        prefix = ''
        # Kconfig values are saved as CONFIG_<value>
        if family == 'K':
            prefix = 'CONFIG_'

        idents = {}
        for token_type, token, _, line in lexer(code).lex():
            if token_type == TokenType.ERROR:
                errors += 1
                if len(samples) < max_samples:
                    samples.append((token, filename, line))
                continue

            token = prefix + token

            if token_type != TokenType.IDENTIFIER:
                continue

            # We only index CONFIG_??? in makefiles
            config_or_not_makefile = family != 'M' or token.startswith('CONFIG_')
            if config_or_not_makefile:
                if token in idents:
                    idents[token].append(line)
                else:
                    idents[token] = [line]

        for ident, lines in idents.items():
            for line in lines:
                rows.append((ident_str(ident), idx, line, family))

    path = scratch_path(data_dir, phase, tag_no, chunk_no)
    write_chunk(path, REFS_SCHEMA, rows)
    return path, errors, samples


def update_compatibles_bindings(item):
    '''One chunk of the comps_docs phase, in a pool process: DT bindings
    documentation files only, as doc rows of family B. Like
    update_compatibles, only the scratch file comes back'''
    phase, tag_no, chunk_no, triples, project, repo_dir, data_dir = item
    rows = []
    seq = chunk_no << 32
    for idx, _, hash in triples:
        family = 'B'
        for ln in compatibles_parser.run(repo.get_blob_lines(repo_dir, hash),
                                         family):
            ident, line = ln.split(' ')
            rows.append((seq, ident, idx, int(line), family))
            seq += 1

    path = scratch_path(data_dir, phase, tag_no, chunk_no)
    write_chunk(path, DOCS_SCHEMA, rows)
    return b'', path


def walk_tag_range(item):
    '''One CONTIGUOUS RANGE of the upfront tag walk, in a pool worker:
    per tag, the same ls-tree and parse list_blobs does (the run's
    serial walk), packed into the range's OWN segment file (hash path
    lines, ls-tree order) and walk-hashes Arrow file (the same schema
    the single-file walk wrote). Returns (the two paths, per-tag
    (tag, offset, length) slices within the segment, the range's
    walked-blob count) — never the blob lists: pickling them back
    would dwarf the parallelism's savings'''
    range_no, tags, repo_dir, data_dir = item
    lists_path = os.path.join(data_dir, 'tmp-blobwalk-lists-%04d' % range_no)
    hashes_path = os.path.join(data_dir,
                               'tmp-arrow-walk-hashes-%04d.arrow' % range_no)
    lists = repo.BlobLists(lists_path)
    hashes = IpcWriter(hashes_path, WALK_SCHEMA)
    slices = []
    walked = 0
    for tag in tags:
        blobs = repo.list_blobs(repo_dir, tag)
        slices.append((tag,) + lists.add(tag, blobs))
        for hash, _, _ in blobs:
            hashes.add((hash,))
        walked += len(blobs)
    hashes.close()
    lists.close()
    return lists_path, hashes_path, slices, walked


# ---- Phase ingestion: the per-row BDB work, as SQL ----
# Streaming: results arrive in chunk order and are ingested in
# consecutive batches, so arrival order IS seq order and
# batched-on-arrival ingestion reproduces the whole-phase ingest's
# identid minting exactly — determinism needs nothing else.

ingest_scratch_bytes = 64 << 20 # flush a batch when its scratch files
                                # reach this size: big enough to
                                # amortize the SQL's fixed costs (the
                                # hash builds over idents/defs_all),
                                # small enough to keep the tail ingest
                                # short. Phases whose whole scratch is
                                # smaller (musl, docs, comps at any
                                # scale) take the single end-of-phase
                                # ingest

# The tag-long accumulators' shapes: tag_defs keeps every ctags line of
# the tag (the def-line map and the fams update read it after the
# phase); tag_comps keeps every compatible. Empty shapes, typed like
# the WORKERS' schemas (not the target tables), so a tag whose phase
# writes no rows still has them
EMPTY_DEFS = ("SELECT 0::BIGINT AS seq, ''::VARCHAR AS name,"
              " 0::INTEGER AS blobid, 0::INTEGER AS defline,"
              " ''::VARCHAR AS deftype, ''::VARCHAR AS family LIMIT 0")
EMPTY_DOCS = ("SELECT 0::BIGINT AS seq, ''::VARCHAR AS name,"
              " 0::INTEGER AS blobid, 0::INTEGER AS line,"
              " ''::VARCHAR AS family LIMIT 0")


def run(repo_dir, data_dir, project=None):
    '''Index every new tag of one project, top to bottom: validation,
    scratch sweep, pool, upfront walk, per-tag phases, recluster,
    invariants, summary — everything the module did as a script.

    repo_dir and data_dir are the project's <root>/<project>/{repo,data}
    directories; project defaults to the layout invariant
    basename(dirname(data_dir)) (== basename(dirname(repo_dir)), which
    is asserted, not trusted). Raises UpdateError on a bad layout or
    failed invariants (after logging them); two consecutive calls in
    one process each get their own pool and connection. Progress goes
    to stdout as timestamped lines, the SUMMARY line last.

    The workers are spawned, so the CALLING process's __main__ must be
    import-safe (the standard `if __name__ == '__main__'` guard) —
    every elixir CLI entry point already is.
    '''
    repo_dir = os.path.abspath(repo_dir)
    data_dir = os.path.abspath(data_dir)
    layout_project = os.path.basename(os.path.dirname(data_dir))
    assert layout_project == os.path.basename(os.path.dirname(repo_dir)), (
        'project layout mismatch: %s and %s do not sit in the same project '
        'directory' % (data_dir, repo_dir))
    if project is None:
        project = layout_project
    else:
        assert project == layout_project, (
            'project %s does not match the layout of %s' % (project, data_dir))

    if not os.path.isdir(data_dir):
        raise UpdateError('%s: data directory %s does not exist'
                          % (project, data_dir))
    if not os.path.isdir(repo_dir):
        raise UpdateError('%s: repository %s does not exist'
                          % (project, repo_dir))

    dts_comp_support = int(project in repo.DTS_COMP_SUPPORT)

    # Run-wide aggregates, written by the parent as it consumes results
    run_phase_s = dict.fromkeys(phases, 0.0)
    run_ingest_s = dict.fromkeys(phases, 0.0) # the SQL-ingest share of each phase
    lexer_errors = 0
    lexer_error_samples = [] # the first ERROR tokens, verbose mode only
    ctags_notices = 0
    ctags_stderr = [] # captured ctags stderr, verbose mode only

    # Cross-phase state, written in one phase, read in later phases
    new_hashes = {} # idx -> blob hash (ids -> every later phase)
    new_filenames = {} # idx -> filename (ids -> defs, docs, comps, refs)
    file_paths = {} # idx -> path (vers -> refs, comps_docs)
    bindings_idxes = [] # DT bindings documentation files (vers -> comps_docs)

    # Scratch leftovers of a crashed run (walk lists, Arrow files of an
    # interrupted phase): advisory garbage only, never read — sweep them
    # before anything else, so whatever happens next starts clean
    def sweep_scratch():
        for name in os.listdir(data_dir):
            if name.startswith(('tmp-blobwalk-', 'tmp-arrow-')):
                os.remove(os.path.join(data_dir, name))

    sweep_scratch()

    def collect_ctags_stderr(stderr):
        '''Count a worker's captured ctags notices; keep the text only
        in verbose mode'''
        nonlocal ctags_notices
        if not stderr:
            return
        ctags_notices += stderr.count(b'ctags: Notice:')
        if log_verbose:
            ctags_stderr.append(stderr)

    def count_lexer_errors(count, samples):
        '''Aggregate a worker's lexer-error counter; keep the first
        samples'''
        nonlocal lexer_errors
        lexer_errors += count
        if len(lexer_error_samples) < max_samples:
            lexer_error_samples.extend(samples[:max_samples - len(lexer_error_samples)])

    # Result unpackers: aggregate a result's counters in the parent (the
    # single writer of the log) and hand back the scratch file's path

    def _unpack_defs(result):
        stderr, path = result
        collect_ctags_stderr(stderr)
        return path

    def _unpack_docs(result):
        path, stderr = result
        collect_ctags_stderr(stderr)
        return path

    def _unpack_refs(result):
        path, errors, samples = result
        count_lexer_errors(errors, samples)
        return path

    def _unpack_plain(result):
        return result[1]

    run_start = time.monotonic()
    pool = None
    conn = None
    blob_lists = None
    walk_segments = [] # the walk's segment files, removed in finally
    walked_blobs = 0
    total_new = 0
    walk_s = 0.0
    violations = []

    try:
        # The workers' pool uses SPAWN, not fork: a spawned worker
        # starts a fresh interpreter, so it shares nothing with the
        # parent — no forked-in DuckDB threads (forking a process with
        # live DuckDB threads is unsupported) and no inherited
        # globals, which forces every worker input through the item
        # tuples. The cost is one import of this module per worker per
        # run, acceptable next to the phases it parallelizes. Creating
        # the pool before the DuckDB connect is no longer required for
        # correctness (spawn would not inherit the connection anyway)
        # but kept: the workers' boot overlaps the connect and the
        # walk instead of the first phase.
        pool = multiprocessing.get_context('spawn').Pool(num_threads)

        db_path = os.path.join(data_dir, 'data.duckdb')
        # DuckDB's buffer budget for ingest. The connect_rw default
        # (512MB) suits serving; a linux tag's refs ingest alone wants
        # gigabytes. A quarter of the RAM keeps room for the parse
        # pool beside it. ELIXIR_DDB_MEM overrides for constrained
        # machines.
        ram_gb = (os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')) >> 30
        conn = dd.connect_rw(db_path, threads=num_threads,
                             memory_limit=os.environ.get('ELIXIR_DDB_MEM',
                                                         '%dGB' % max(1, ram_gb // 4)))

        done = {row[0] for row in conn.execute('SELECT tag FROM versions').fetchall()}
        tag_buf = [tag for tag in repo.list_tags(repo_dir, project)
                   if lib.decode(tag) not in done]

        num_tags = len(tag_buf)

        log('%s: %d new tags (repo %s, data %s, %d threads)'
            % (project, num_tags, repo_dir, data_dir, num_threads))

        # ---- Walk: every tag's blobs, up front, on disk ----
        # One blocking pass before any indexing, fanned out over the
        # pool (idle until now) in CONTIGUOUS RANGES of tags — tag
        # order = range order, so each tag's list stays one worker's
        # own sequential ls-tree parse, byte-identical to the serial
        # walk's. Each range's worker packs its tags into its OWN
        # segment file and walk-hashes Arrow file and returns only
        # per-tag slices, never the blob lists. The parent's slice
        # index then maps tag -> (segment, offset, length), get()
        # reads its slice from the right segment, and the new count
        # falls out of SQL over ALL the hash files — distinct walked
        # hashes absent from the blobs table — a query order-free in
        # the files, where the old walk paid a scratch BDB for the
        # dedup.
        if num_tags:
            walk_start = time.monotonic()
            # Four ranges per worker, not one: a tag's walk cost grows
            # with its tree, and the early ranges of a one-range split
            # would finish while the last grinds the big modern trees;
            # imap hands the next range to whichever worker frees
            # first, so the oversubscription is the load balance
            n_ranges = min(num_tags, num_threads * 4)
            size = -(-num_tags // n_ranges)
            ranges = [tag_buf[i:i + size] for i in range(0, num_tags, size)]
            items = [(no, tags, repo_dir, data_dir)
                     for no, tags in enumerate(ranges)]

            last_walk_log = walk_start
            segments = []
            hashes_files = []
            walked_tags = 0
            for item, result in zip(items, pool.imap(walk_tag_range, items),
                                    strict=True):
                lists_path, hashes_path, slices, blobs = result
                segments.append((lists_path, slices))
                hashes_files.append(hashes_path)
                walk_segments.append(lists_path)
                walked_blobs += blobs
                walked_tags += len(item[1])
                t = time.monotonic()
                if (walked_tags < num_tags and
                        t - last_walk_log >= progress_min_interval):
                    log('walking tags %d/%d …' % (walked_tags, num_tags))
                    last_walk_log = t
            blob_lists = repo.BlobLists(*segments)

            conn.register('walk_hashes', pads.dataset(
                [str(f) for f in hashes_files], format='ipc'))
            total_new = conn.execute('''
                SELECT count(*) FROM (SELECT DISTINCT blobhash FROM walk_hashes) w
                WHERE NOT EXISTS (SELECT 1 FROM blobs b WHERE b.blobhash = w.blobhash)
                ''').fetchone()[0]
            conn.unregister('walk_hashes')
            for path in hashes_files: # their work ends with the walk
                os.remove(path)

            walk_s = time.monotonic() - walk_start
            log('walk done: %d tags, %d blobs walked, %d new, %s'
                % (num_tags, walked_blobs, total_new, fmt_secs(walk_s)))

        # ---- The per-phase SQL ingest helpers ----

        def ingest_scratch(name, files):
            '''Materialize one ingest batch's scratch files into a temp
            table, in the transaction the tag opened. Scan order is not
            guaranteed, so nothing downstream may depend on it —
            orderings key on seq'''
            conn.register(name + '_in',
                          pads.dataset([str(f) for f in files], format='ipc'))
            conn.execute('CREATE OR REPLACE TEMP TABLE %s AS SELECT * FROM %s_in'
                         % (name, name))

        def init_tag_tables():
            '''The per-tag accumulators, empty: created at tag start so
            every reader below sees them even when the phase produced
            nothing'''
            conn.execute('CREATE OR REPLACE TEMP TABLE tag_defs AS ' + EMPTY_DEFS)
            # The name-gated defs accumulator: what fams reads. Raw
            # tag_defs is NOT enough there — a blacklisted name can
            # still be minted by the docs phase (docs has no name
            # gate), and its raw defs rows must not set that ident's
            # family bits
            conn.execute('''CREATE OR REPLACE TEMP TABLE tag_defs_gated AS
                            SELECT name, seq, blobid, defline, deftype,
                                   family::deffam AS family FROM tag_defs LIMIT 0''')
            if dts_comp_support:
                conn.execute('CREATE OR REPLACE TEMP TABLE tag_comps AS ' + EMPTY_DOCS)

        def assign_idents(source):
            '''Dense identids for the source's first-seen names, in
            first-appearance (seq) order — the same counter discipline
            as blobid and versionid, and what makes two independent
            builds agree'''
            next_id = conn.execute(
                'SELECT coalesce(max(identid) + 1, 0) FROM idents').fetchone()[0]
            conn.execute('''
                INSERT INTO idents (identid, name, def_fams, macro_fams)
                SELECT ? + row_number() OVER (ORDER BY min(seq)) - 1, name, NULL, NULL
                FROM %s
                WHERE name NOT IN (SELECT name FROM idents)
                GROUP BY name''' % source, [next_id])

        def ingest_defs(files):
            '''One ingest batch of defs chunks: raw rows into the tag's
            accumulator, the name-gated rows minted and staged.
            Unknown-deftype rows ingest like any other — the NAME gate
            alone decides, as in the old DefList keys (ghosts: never
            displayed, never in def_fams)'''
            ingest_scratch('tag_defs_batch', files)
            conn.execute('INSERT INTO tag_defs SELECT * FROM tag_defs_batch')
            conn.execute('''
                CREATE OR REPLACE TEMP TABLE tag_defs_ok AS
                SELECT name, seq, blobid, defline, deftype, family::deffam AS family
                FROM tag_defs_batch
                WHERE length(name) >= 2 AND name NOT LIKE '~%'
                  AND name NOT IN (SELECT unnest(?::VARCHAR[]))''', [ident_blacklist])
            assign_idents('tag_defs_ok')
            conn.execute('INSERT INTO tag_defs_gated SELECT * FROM tag_defs_ok')
            conn.execute('''
                INSERT INTO defs_stage
                SELECT i.identid, c.blobid, c.defline, c.deftype, c.family
                FROM tag_defs_ok c JOIN idents i ON i.name = c.name''')

        def ingest_docs(files):
            '''One ingest batch of docs chunks: names minted, rows
            staged'''
            ingest_scratch('tag_docs_batch', files)
            assign_idents('tag_docs_batch')
            conn.execute('''
                INSERT INTO docs_stage
                SELECT i.identid, d.blobid, d.line, d.family::reffam
                FROM tag_docs_batch d JOIN idents i ON i.name = d.name''')

        def ingest_comps(files):
            '''One ingest batch of comps chunks: names minted,
            compatible defs rows staged'''
            ingest_scratch('tag_comps_batch', files)
            conn.execute('INSERT INTO tag_comps SELECT * FROM tag_comps_batch')
            assign_idents('tag_comps_batch')
            conn.execute('''
                INSERT INTO defs_stage
                SELECT i.identid, c.blobid, c.line, 'compatible', c.family::deffam
                FROM tag_comps_batch c JOIN idents i ON i.name = c.name''')

        def begin_refs_ingest():
            '''The (blob, line) -> def-name map the refs gate needs,
            from the defs phase's accumulated raw rows: every ctags
            line (valid types or not), where a later line of the same
            (blob, line) overwrote an earlier one — arg_max over the
            workers' write order (the defs_idxes dict it ports)'''
            conn.execute('''
                CREATE OR REPLACE TEMP TABLE tag_deflines AS
                SELECT blobid, defline, arg_max(name, seq) AS name
                FROM tag_defs GROUP BY blobid, defline''')

        def ingest_refs(files):
            '''One ingest batch of refs chunks, gated: the ident must
            have a defs row (any but compatibles — ghost keys
            included, the old defs_keys set was the KEY set), and a
            line that defines the same ident is not also a reference
            to it'''
            ingest_scratch('tag_refs_batch', files)
            conn.execute('''
                INSERT INTO refs_stage
                SELECT i.identid, r.blobid, r.line, r.family::reffam
                FROM tag_refs_batch r
                JOIN idents i ON i.name = r.name
                WHERE EXISTS (SELECT 1 FROM defs_all d
                              WHERE d.identid = i.identid
                                AND d.deftype <> 'compatible')
                  AND NOT EXISTS (SELECT 1 FROM tag_deflines t
                                  WHERE t.blobid = r.blobid AND t.defline = r.line
                                    AND t.name = r.name)''')

        def ingest_comps_docs(files):
            '''One ingest batch of comps_docs chunks: bindings
            compatibles as doc rows of family B, gated on the
            compatible having a defs row'''
            ingest_scratch('tag_bdocs_batch', files)
            conn.execute('''
                INSERT INTO docs_stage
                SELECT i.identid, b.blobid, b.line, 'B'::reffam
                FROM tag_bdocs_batch b JOIN idents i ON i.name = b.name
                WHERE EXISTS (SELECT 1 FROM defs_all d
                              WHERE d.identid = i.identid
                                AND d.deftype = 'compatible')''')

        def update_ident_fams():
            '''idents.def_fams/macro_fams, OR-accumulated from this
            tag's def rows only: name-gated ctags types and
            compatibles (the type filter drops ghosts). Reading the
            gated accumulator, not raw defs rows: a blacklisted name
            can be minted by the docs phase, and its raw rows must
            not set bits'''
            sources = ["SELECT i.identid AS identid, d.deftype AS deftype, d.family AS family "
                       "FROM tag_defs_gated d JOIN idents i ON i.name = d.name "
                       "WHERE d.deftype IN (%s)" % deftype_list]
            if dts_comp_support:
                sources.append("SELECT i.identid, 'compatible', c.family "
                               "FROM tag_comps c JOIN idents i ON i.name = c.name")
            union = ' UNION ALL '.join(sources)
            conn.execute('''
                CREATE OR REPLACE TEMP TABLE tag_fams AS
                SELECT identid,
                       bit_or(CASE family %s END) AS def_fams,
                       coalesce(bit_or(CASE WHEN deftype = 'macro'
                                         THEN CASE family %s END END), 0) AS macro_fams
                FROM (%s) GROUP BY identid''' % (fam_bits, fam_bits, union))
            conn.execute('''
                UPDATE idents SET
                    def_fams = coalesce(idents.def_fams, 0) | tag_fams.def_fams,
                    macro_fams = coalesce(idents.macro_fams, 0) | tag_fams.macro_fams
                FROM tag_fams WHERE idents.identid = tag_fams.identid''')

        def update_blob_ids(blobs):
            '''Assign dense idx numbers to the tag's new blobs: hashes
            are deduped by anti-join against the blobs table (a Python
            set would put the whole history in memory), new ids follow
            the walk order, and the first occurrence's filename is the
            one stored — exactly what the old per-key
            db.blob/db.hash/db.file writes did. Returns (the tag's new
            idxes in walk order, one blobid per walked occurrence).'''
            table = pa.table({
                'seq': pa.array(range(len(blobs)), type=pa.uint32()),
                'blobhash': pa.array([b[0] for b in blobs], type=pa.binary()),
                'filename': pa.array([lib.decode(b[1]) for b in blobs], type=pa.string()),
                'filepath': pa.array([lib.decode(b[2]) for b in blobs], type=pa.string())})
            conn.register('tag_blobs_in', table)
            conn.execute('CREATE OR REPLACE TEMP TABLE tag_blobs AS SELECT * FROM tag_blobs_in')

            next_id = conn.execute(
                'SELECT coalesce(max(blobid) + 1, 0) FROM blobs').fetchone()[0]
            conn.execute('''
                CREATE OR REPLACE TEMP TABLE tag_new AS
                SELECT ? + row_number() OVER (ORDER BY first_seq) - 1 AS blobid,
                       blobhash, filename
                FROM (SELECT blobhash, min(seq) AS first_seq, arg_min(filename, seq) AS filename
                      FROM tag_blobs b
                      WHERE NOT EXISTS (SELECT 1 FROM blobs k WHERE k.blobhash = b.blobhash)
                      GROUP BY blobhash)''', [next_id])
            conn.execute('INSERT INTO blobs SELECT blobid, blobhash, filename FROM tag_new')

            # One blobid per walked occurrence, in walk order (old
            # blobs too: version_objects covers every path of the tag)
            occ_blobids = [row[0] for row in conn.execute('''
                SELECT b.blobid FROM tag_blobs t
                JOIN blobs b ON b.blobhash = t.blobhash
                ORDER BY t.seq''').fetchall()]

            idxes = []
            for blobid, blobhash, filename in conn.execute(
                    'SELECT blobid, blobhash, filename FROM tag_new ORDER BY blobid').fetchall():
                idxes.append(blobid)
                new_hashes[blobid] = blobhash
                new_filenames[blobid] = filename
            return idxes, occ_blobids

        def update_versions(tag, blobs, occ_blobids):
            '''The tag's completion marker (versions) and its blob
            paths (version_objects), replacing the PathList: one row
            per walked occurrence — identical blobs under two paths
            list twice, like the PathList did'''
            versionid = conn.execute(
                'SELECT coalesce(max(versionid) + 1, 0) FROM versions').fetchone()[0]
            conn.execute('INSERT INTO versions VALUES (?, ?)',
                         [versionid, lib.decode(tag)])

            table = pa.table({
                'versionid': pa.array([versionid] * len(blobs), type=pa.uint32()),
                'blobid': pa.array(occ_blobids, type=pa.uint32()),
                'filepath': pa.array([lib.decode(b[2]) for b in blobs], type=pa.string())})
            conn.register('tag_vo_in', table)
            conn.execute('INSERT INTO version_objects_stage SELECT * FROM tag_vo_in')

            for (_h, _f, path), blobid in zip(blobs, occ_blobids, strict=True):
                file_paths[blobid] = path
                # Store DT bindings documentation files to parse them later
                if path[:33] == b'Documentation/devicetree/bindings':
                    bindings_idxes.append(blobid)

        # The run ETA's time base: indexing, not the walk before it
        index_start = time.monotonic()

        def index_tag(tag, tag_no):
            '''Index one tag inside its own transaction: every phase's
            inserts and the versions row commit together, so a tag is
            either fully in the database or not at all. Returns
            (per-phase seconds, ingest seconds, new blob count).'''
            # Per-tag state, so each tag starts clean
            file_paths.clear()
            bindings_idxes.clear()
            new_hashes.clear()
            new_filenames.clear()

            # One walk over the tag's blobs feeds every phase below:
            # read back from the upfront walk's packed file, same
            # triples in the same ls-tree order it captured
            blobs = blob_lists.get(tag)

            times = {}
            ingest_times = {}

            conn.execute('BEGIN TRANSACTION')
            try:
                init_tag_tables()

                # Phase 1: assign idx numbers to the tag's new blobs
                with PhaseTimer(times, 'ids', run_phase_s):
                    idxes, occ_blobids = update_blob_ids(blobs)

                # Phase 2: versions and the tag's blob paths
                with PhaseTimer(times, 'vers', run_phase_s):
                    update_versions(tag, blobs, occ_blobids)

                # One consume loop per phase below: results are
                # aggregated as they arrive and INGESTED in batches of
                # consecutive arrivals (chunk order = seq order, so
                # the streaming ingest reproduces the batched one
                # exactly) once enough scratch has piled up, while the
                # pool's other workers keep crunching; files are
                # deleted the moment their batch is ingested
                def pool_phase(name, fn, chunks_list, unpack, ingest):
                    prog = PhaseProgress(project, tag, name,
                                         sum(len(c) for c in chunks_list))
                    items = [(name, tag_no, ci, chunk, project, repo_dir, data_dir)
                             for ci, chunk in enumerate(chunks_list)]
                    pending = []
                    pending_bytes = 0
                    done_blobs = 0
                    def flush():
                        nonlocal pending_bytes
                        if not pending:
                            return
                        files = pending[:]
                        with PhaseTimer(ingest_times, name, run_ingest_s):
                            ingest(files)
                        for path in files:
                            os.remove(path)
                        del pending[:]
                        pending_bytes = 0
                    for item, result in zip(items, pool.imap(fn, items),
                                            strict=True):
                        pending.append(unpack(result))
                        pending_bytes += os.path.getsize(pending[-1])
                        done_blobs += len(item[3])
                        if pending_bytes >= ingest_scratch_bytes:
                            flush()
                        prog.update(done_blobs)
                    flush()

                # Phase 3: definitions, doc comments, compatibles —
                # all on the process pool (threads only helped the
                # ctags subprocesses; the JSON parsing and regex scans
                # are Python and serialized on the GIL). Workers take
                # (idx, filename, hash) triples in their items, with
                # the project and dirs: spawned workers share nothing
                # with the parent.
                work = list(chunks(idxes))
                triple_chunks = [[(idx, new_filenames[idx], new_hashes[idx])
                                  for idx in chunk] for chunk in work]

                with PhaseTimer(times, 'defs', run_phase_s):
                    pool_phase('defs', update_definitions, triple_chunks,
                               _unpack_defs, ingest_defs)

                with PhaseTimer(times, 'docs', run_phase_s):
                    pool_phase('docs', _docs_chunk, triple_chunks,
                               _unpack_docs, ingest_docs)

                if dts_comp_support:
                    with PhaseTimer(times, 'comps', run_phase_s):
                        pool_phase('comps', update_compatibles, triple_chunks,
                                   _unpack_plain, ingest_comps)

                # Family bitmasks for every ident this tag gave a def
                # (the DefList families blob and the defs caches, as
                # one column)
                update_ident_fams()

                # Phase 4: references (needs all definitions)
                # The gate runs in SQL. Each worker process owns its
                # own persistent cat-file --batch pipe.
                refs_chunks = [[(idx, lib.decode(file_paths[idx]), new_hashes[idx])
                                for idx in chunk] for chunk in work]

                with PhaseTimer(times, 'refs', run_phase_s):
                    if refs_chunks:
                        with PhaseTimer(ingest_times, 'refs', run_ingest_s):
                            begin_refs_ingest()
                    pool_phase('refs', _refs_lex_chunk, refs_chunks,
                               _unpack_refs, ingest_refs)

                # Phase 5: compatibles from bindings documentation
                # (needs all comps)
                if dts_comp_support:
                    # Only this tag's NEW bindings blobs: the old code
                    # iterated new-blob chunks filtered by
                    # bindings_idxes, and new_hashes/file_paths only
                    # carry new idxes
                    bwork = list(chunks([i for i in bindings_idxes if i in new_hashes]))
                    bchunks = [[(idx, file_paths[idx], new_hashes[idx])
                                for idx in chunk] for chunk in bwork]

                    with PhaseTimer(times, 'comps_docs', run_phase_s):
                        pool_phase('comps_docs', update_compatibles_bindings,
                                   bchunks, _unpack_plain, ingest_comps_docs)

                # Commit the tag: only now is it visible. A crash
                # before this point rolls the whole tag back; the next
                # run re-indexes it.
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise

            return times, ingest_times, len(idxes)

        done_tags = 0
        done_blobs = 0

        for tag_no, tag in enumerate(tag_buf, 1):
            times, ingest_times, blobs = index_tag(tag, tag_no)
            done_tags += 1
            done_blobs += blobs

            # Tag completion line with per-phase seconds and, while
            # tags remain, the run's remaining work: the upfront walk
            # counted the new blobs, so "blobs left" is a fact and the
            # ETA only carries the cumulative rate's noise, exact from
            # the first tag on
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

        # Merge this run's staged rows into the clustered main tables
        # (the staging discipline of data_duckdb: ident lookups stay
        # fast), then hold the result to the SQL invariants — a
        # violation is a bug, and the run must fail loudly rather
        # than publish a broken database.
        staged = any(conn.execute('SELECT count(*) FROM %s_stage' % t).fetchone()[0]
                     for t in dd.FACT_TABLES)
        recluster_s = 0.0
        if staged:
            start = time.monotonic()
            dd.recluster(conn)
            recluster_s = time.monotonic() - start
        violations = dd.check_invariants(conn)
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()
        if conn is not None:
            conn.close()
        if blob_lists is not None:
            blob_lists.close()
        for path in walk_segments:
            os.remove(path)

    for v in violations:
        log('%s: INVARIANT VIOLATION: %s' % (project, v))
    if violations:
        raise UpdateError('%s: %d invariant violations'
                          % (project, len(violations)))

    sweep_scratch()

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
        'blobs_per_s': round(done_blobs / wall, 3) if done_blobs else 0,
        'phases': {p: round(run_phase_s[p], 3) for p in phases},
        'ingest_s': {p: round(run_ingest_s[p], 3) for p in phases if run_ingest_s[p]},
        'recluster_s': round(recluster_s, 3),
        'lexer_errors': lexer_errors,
        'ctags_notices': ctags_notices,
    }))
