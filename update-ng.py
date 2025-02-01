#!/usr/bin/env python3
#
# TODO: why are all strings handled as byte arrays?

import collections
import datetime
import functools
import multiprocessing
import os
import sys
import re
import resource
import tqdm

import elixir.lib as lib
import elixir.data as data

dts_comp_support = int(lib.script("dts-comp"))
assert dts_comp_support == 0

db = data.DB(lib.getDataDir(), readonly=False, dtscomp=dts_comp_support)


# Grab a list of new tags, ie those that do not exist in the database.
def get_new_tags():
    for tag in lib.scriptLines("list-tags"):
        if not db.vers.exists(tag):
            yield tag


# Return array of ident definitions in `idx` blob.
def compute_defs_for_idx(idx):
    blob_hash = db.hash.get(idx)
    blob_filename = db.file.get(idx)

    blob_family = lib.getFileFamily(blob_filename)
    if blob_family in [None, "M"]:
        return []

    res = []

    for line in lib.scriptLines("parse-defs", blob_hash, blob_filename, blob_family):
        ident, ident_type, ident_lineno = line.split(b" ")

        if lib.isIdent(ident):
            ident_lineno = int(ident_lineno.decode())
            res.append((ident, idx, ident_type, ident_lineno, blob_family.encode()))

    return res


deflist_regex = re.compile(r"^(\d+)(\w)(\d+)(\w)$")


# Example: b'433t12C,849t36C,1293t43C,1445t43C#C'
def parse_defs(def_list):
    if def_list is None or def_list == b"" or def_list == b"#":
        return

    # Drop families.
    def_list = def_list[: def_list.find(b"#")]

    for entry in def_list.split(b","):
        m = deflist_regex.match(entry.decode())
        assert m is not None
        idx, def_type, lineno, family = m.groups()
        yield (int(idx), def_type.encode(), int(lineno), family.encode())


# Example: b'185:134,135,136,137,138,146:C\n1102:134,135,136,137,138,146:C\n'
# => [(185, b'134,135,136,137,138,146', b'C'), ...]
#
# It also gets used for docs as those have the same format.
def parse_refs(ref_list):
    if ref_list is None or ref_list == b"" or ref_list == b"\n":
        return

    assert ref_list[-1] == ord("\n")

    for entry in ref_list.split(b"\n")[:-1]:
        idx, lines, family = entry.split(b":")
        yield (int(idx.decode()), lines, family)


defTypeR = {
    "c": "config",
    "d": "define",
    "e": "enum",
    "E": "enumerator",
    "f": "function",
    "l": "label",
    "M": "macro",
    "m": "member",
    "p": "prototype",
    "s": "struct",
    "t": "typedef",
    "u": "union",
    "v": "variable",
    "x": "externvar",
}

defTypeD = {v: k for k, v in defTypeR.items()}


# Run `script.sh parse-defs ...` on all new blobs, in parallel.
# Store results in db.defs.
# Return a set contain tuples of (idx, ident_lineno, ident). This allows later
# testing if a definition appears on a given line, in a given blob.
def compute_defs(new_idxes, chunksize):
    nb_new_defs = 0
    new_defs = {}
    new_defs_set = set()
    with multiprocessing.Pool() as pool:
        it = pool.imap_unordered(compute_defs_for_idx, new_idxes, chunksize=chunksize)
        it = tqdm.tqdm(it, desc="defs", total=len(new_idxes), leave=False)
        for i, defs in enumerate(it):
            for ident, idx, ident_type, ident_lineno, blob_family in defs:
                if ident in new_defs:
                    obj = new_defs[ident]
                else:
                    def_list = db.defs.db.get(ident)
                    obj = list(parse_defs(def_list))

                ident_type = ident_type.decode()
                if ident_type in defTypeD:
                    obj.append(
                        (idx, defTypeD[ident_type].encode(), ident_lineno, blob_family)
                    )
                    new_defs_set.add((idx, ident_lineno, ident))

                new_defs[ident] = obj

                # obj = db.defs.get(ident) or data.DefList()
                # TODO: dedup entry?
                # obj.append(idx, ident_type, ident_lineno, blob_family)
                # db.defs.put(ident, obj)
                nb_new_defs += 1

    for ident, defs in tqdm.tqdm(new_defs.items(), desc="defs->db", leave=False):
        # TODO: undo change in data.py which sorts on .append()
        families = b",".join(sorted(set([family for _, _, _, family in defs])))
        defs = sorted(defs, key=lambda x: (x[0], x[1], str(x[2]), x[3]))
        val = b",".join(
            [str(a).encode() + b + str(c).encode() + d for a, b, c, d in defs]
        )
        val = val + b"#" + families
        db.defs.db.put(ident, val)

    db.defs.db.sync()
    return (nb_new_defs, new_defs_set)


def compute_refs_for_idx(idx, defs_set):
    blob_hash = db.hash.get(idx)
    blob_filename = db.file.get(idx)
    idx_bytes = str(idx).encode()

    blob_family = lib.getFileFamily(blob_filename)
    if blob_family is None:
        return (b'\n', 0)

    # TODO: maybe do it in `./script.sh tokenize-file`?
    prefix = b"CONFIG_" if blob_family == "K" else b""

    blob_family_bytes = blob_family.encode()

    nb_new_refs = 0
    even = True
    line_number = 1
    refs = bytearray()

    for ident in lib.scriptLines("tokenize-file", "-b", blob_hash, blob_family):
        even = not even
        if even:
            ident = prefix + ident

            if (
                # TODO: we could do a set that contains all defs in this
                # version... Here we check in the DB, so we haven't improved
                # our situation.
                db.defs.exists(ident)
                and (idx, line_number, ident) not in defs_set
                and (blob_family != "M" or ident.startswith(b"CONFIG_"))
            ):
                refs += ident
                refs += b"\t"
                refs += idx_bytes
                refs += b"\t"
                refs += str(line_number).encode()
                refs += b"\t"
                refs += blob_family_bytes
                refs += b"\n"
                nb_new_refs += 1
        else:
            # TODO: this is so weird. Format is: first line gives line number
            # offset, second line gives ident, third line gives another offset,
            # fourth gives ident, etc.
            line_number += ident.count(b"\1")

    return (bytes(refs), nb_new_refs)


# new_idxes is all blobs that should be parsed for refs.
# all_idxes is all blobs that contain interesting defs (ie blobs in the tag).
# refs_aof_fd: file descriptor to the append-only file storing new references.
def compute_refs(new_idxes, all_idxes, defs_set, new_defs_set, refs_aof_fd, chunksize):
    # The valid defs in this version are:
    #  - The valid defs in the previous version, minus those in blobs that are
    #    not in the current version.
    #  - Plus the defs given by the parse-defs step.
    defs_set = set(filter(lambda x: x[0] not in all_idxes, defs_set))
    defs_set |= new_defs_set

    nb_new_refs = 0
    fn = functools.partial(compute_refs_for_idx, defs_set=defs_set)
    new_refs = {}

    # TODO: this is too memory expensive.

    # TODO: we lose too much parallelism by doing tag-per-tag for small tags.
    # Could we use a single pool and consume from it using multiple calls?
    #
    # We should probably compute all blobs then we could do a progress bar for
    # each type (defs, refs, etc.) on the total.

    # Maybe it is because they must each access to defs_set?

    # tqdm.tqdm.write(f"size defs_set: {sys.getsizeof(defs_set) // (1000*1000)} MB")
    # 268MB is a lot if this gets copied to each thread

    chunksize = 10

    with multiprocessing.Pool() as pool:
        it = pool.imap_unordered(fn, new_idxes, chunksize=chunksize)
        it = tqdm.tqdm(it, desc="refs", total=len(new_idxes), leave=False)
        for refs, nb_new in it:
            refs_aof_fd.write(refs)
            nb_new_refs += nb_new

    return (nb_new_refs, defs_set)


def compute_docs_for_idx(idx):
    blob_hash = db.hash.get(idx)
    blob_filename = db.file.get(idx)

    blob_family = lib.getFileFamily(blob_filename)
    if blob_family in [None, "M"]:
        return []

    idents = collections.OrderedDict()

    for line in lib.scriptLines("parse-docs", blob_hash, blob_filename):
        ident, lineno = line.split(b" ", maxsplit=1)
        if ident in idents:
            idents[ident] += b"," + lineno
        else:
            idents[ident] = lineno

    return [
        (ident, idx, line_numbers, blob_family)
        for ident, line_numbers in idents.items()
    ]


def compute_docs(new_idxes, chunksize):
    new_docs = {}
    nb_new_docs = 0

    with multiprocessing.Pool() as pool:
        it = pool.imap_unordered(compute_docs_for_idx, new_idxes, chunksize=chunksize)
        it = tqdm.tqdm(it, desc="docs", total=len(new_idxes), leave=False)
        for i, docs in enumerate(it):
            for ident, idx, line_numbers, blob_family in docs:
                if ident in new_docs:
                    obj = new_docs[ident]
                else:
                    docs_list = db.docs.db.get(ident)
                    obj = list(parse_refs(docs_list))

                obj.append((idx, line_numbers, blob_family.encode()))
                new_docs[ident] = obj
                nb_new_docs += 1

    # No need to have a progress bar, this is always fast.
    for ident, docs in new_docs.items():
        docs.sort()
        value = b"\n".join(
            [
                str(idx).encode() + b":" + lines + b":" + family
                for idx, lines, family in docs
            ]
        )
        value += b"\n"
        db.docs.db.put(ident, value)

    return nb_new_docs


def print_timing(t0, name, nb, blobs=None, it="it"):
    t1 = datetime.datetime.now()
    duration = (t1 - t0).total_seconds()
    its_per_sec = f" ({blobs / duration:.2f} {it}/s)" if blobs else ""
    tqdm.tqdm.write(f"{name}: {nb} found, in {duration:.2f}s{its_per_sec}")


t0 = datetime.datetime.now()

new_tags = list(get_new_tags())
print_timing(t0, "tags", len(new_tags))

# TODO: init defs_set with defs in the previous tag. We won't always start from
# an empty database.
defs_set = set()

# TODO: wait! this doesn't work well as we only deal with new tags. We could in
# theory have new tags that are not at the end. This is the issue with complex
# state: it needs to be kept valid whatever happens...

def compute_blobs_for_tag(tag):
    res = bytearray()

    for line in lib.scriptLines("list-blobs", "-p", tag):
        blob_hash, blob_filepath = line.split(b" ", maxsplit=1)
        res += tag + b"\t" + blob_hash + b"\t" + blob_filepath + b"\n"

    return bytes(res)


def compute_blobs(new_tags):
    with multiprocessing.Pool() as pool:
        it = pool.imap(compute_blobs_for_tag, new_tags)
        it = tqdm.tqdm(it, desc="blobs", total=len(new_tags), leave=False)
        for blobs in it:
            # print(blobs.count(b'\n'))
            pass




with open(f'/tmp/elixir-update-{os.getpid()}-refs.txt', 'wb') as refs_aof_fd:
    for tag in tqdm.tqdm(new_tags, desc="tags", leave=False):
        t1 = datetime.datetime.now()

        # TODO: extract this part into its function

        vers_buffer = []
        all_idxes = set()
        new_idxes = set()

        next_free_idx = db.vars.get("numBlobs")
        if next_free_idx is None:
            next_free_idx = 0

        blobs_it = lib.scriptLines("list-blobs", "-p", tag)
        blobs_it = tqdm.tqdm(blobs_it, desc="blobs", leave=False)
        for line in blobs_it:
            blob_hash, blob_filepath = line.split(b" ", maxsplit=1)

            idx = db.blob.get(blob_hash)
            if idx is None:
                idx = next_free_idx
                new_idxes.add(idx)
                next_free_idx += 1

                db.blob.put(blob_hash, idx)  # Map blob hash to idx.
                db.hash.put(idx, blob_hash)  # Map idx to blob hash.
                blob_filename = blob_filepath[blob_filepath.rfind(b"/") + 1 :]
                db.file.put(idx, blob_filename)  # Map idx to filename.

            all_idxes.add(idx)
            vers_buffer.append((idx, blob_filepath))

        vers_buffer.sort()
        value = b"\n".join(
            [str(idx).encode() + b" " + blob_filepath for idx, blob_filepath in vers_buffer]
        )
        value += b"\n"
        db.vers.put(tag, value)

        db.vars.put("numBlobs", next_free_idx)

        db.vars.db.sync()
        db.blob.db.sync()
        db.hash.db.sync()
        db.file.db.sync()
        db.vers.db.sync()

        t2 = datetime.datetime.now()

        # For small batches, avoid using too big of a chunksize else most cores will
        # be starved.
        chunksize = int(len(new_idxes) / multiprocessing.cpu_count())
        chunksize = min(max(1, chunksize), 400)

        nb_new_defs, new_defs_set = compute_defs(new_idxes, chunksize)

        t3 = datetime.datetime.now()

        nb_new_refs, defs_set = compute_refs(new_idxes, all_idxes, defs_set, new_defs_set, refs_aof_fd, chunksize)

        t4 = datetime.datetime.now()

        nb_new_docs = compute_docs(new_idxes, chunksize)

        t5 = datetime.datetime.now()

        blobs_duration = (t2 - t1).total_seconds()
        defs_duration = (t3 - t2).total_seconds()
        refs_duration = (t4 - t3).total_seconds()
        docs_duration = (t5 - t4).total_seconds()
        total_duration = (t5 - t1).total_seconds()

        nb = len(new_idxes)
        stats = f"{nb:5} blobs, {nb_new_defs:6} defs, {nb_new_refs:7} refs, {nb_new_docs:6} docs"
        timings = f"{nb / defs_duration:4.0f}, {nb / refs_duration:4.0f}, {nb / docs_duration:4.0f} blobs/s"
        tqdm.tqdm.write(f"{tag.decode()}:\t{stats}, in {total_duration:6.2f}s ({timings})")

# TODO: put all refs into the db

t6 = datetime.datetime.now()
duration = (t6 - t0).total_seconds()
# print(f"total time: {duration:.2f}s")

max_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
# print(f"max rss: {max_rss_kb / (1000*1000):.3f} GB")

# TODO: update comps
# TODO: update comps docs

# TODO: versions.db stores (idx, filename) but we could remove filename and make
# a query in filenames.db at runtime. This will make versions.db much smaller
# which could make things faster.

# TODO: move blob computation to each tag, it is too long and big for Linux.
# This means it will be done in single-core as well, which is even slow. Maybe
# just retrieve from database when its time. This means we keep listing of
# blobs parallel.

# TODO: check max rss, I'm seeing >4GB when indexing a few Linux tags. Is it
# linked with using the definitions.db from prod?

# TODO: we could simplify the logic by writing to a file all refs. That means we
# are not reloading all the time, we are not merging all the time the same
# thing, etc.
#
# Then run sort on the file to aggregate keys together. Then do the merging.
# That way we merge once and not thousand of times (at each tag). And we don't
# have to grab from DB and parse each old ref.
#
# The same logic can be achieved for defs *if* we do all defs then all refs
# (and docs).
