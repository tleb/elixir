import multiprocessing.pool
import os
import resource
import time

import duckdb
import numpy as np
import pandas as pd

# TODO: make scriptLines return strings, that'll make everything easier.
import elixir.lib as lib

# Obtained from:
# >>> import duckdb
# >>> ddb = duckdb.connect()
# >>> ddb.sql("CREATE TYPE blobfamily AS ENUM ('A', 'B', 'C', 'D', 'K', 'M')")
# >>> ddb.sql("CREATE TABLE blobs (blobfamily blobfamily)")
# >>> ddb.table('blobs').df().dtypes.blobfamily
# CategoricalDtype(categories=['A', 'B', 'C', 'D', 'K', 'M'], ordered=True, categories_dtype=object)
BLOBFAMILY_DTYPE = pd.CategoricalDtype(
    categories=["A", "B", "C", "D", "K", "M"], ordered=True
)

# Must regenerate the databases when this gets updated!
DEFTYPES = {
    "compatible",
    "config",
    "define",
    "enum",
    "enumerator",
    "function",
    "label",
    "macro",
    "member",
    "prototype",
    "struct",
    "typedef",
    "union",
    "variable",
    "externvar",
}


def pool_and_write_output_to_db(ddb, worker_fn, worker_args, query):
    # An array of DataFrame. Instead of appending each time the new DataFrame to the
    # current buffer, we instead keep a list of DataFrames. At the end we do a single
    # concat call.
    ddb_data, nb_rows = [], 0

    with multiprocessing.pool.Pool() as pool:
        for x in pool.imap(worker_fn, worker_args, chunksize=10):
            if x is None:
                continue
            nb_rows += len(x)
            ddb_data.append(x)

            if nb_rows >= 10000:
                ddb_data = pd.concat(ddb_data)
                ddb.execute(query)
                ddb_data, nb_rows = [], 0

    if ddb_data:
        ddb_data = pd.concat(ddb_data)
        ddb.execute(query)


def stage01_ddb_init():
    ddb = duckdb.connect(os.path.join(os.environ["LXR_DATA_DIR"], "data.db"))

    deftypes = np.array(list(DEFTYPES))

    ddb.sql("""
    CREATE SEQUENCE IF NOT EXISTS blobid_sequence;

    CREATE TYPE IF NOT EXISTS blobfamily AS ENUM ('A', 'B', 'C', 'D', 'K', 'M');

    CREATE TYPE IF NOT EXISTS deftype AS ENUM (SELECT * FROM deftypes);

    -- db.blob :: hash -> blobid
    -- db.hash :: blobid -> hash
    -- db.file :: blobid -> blobfilename
    CREATE TABLE IF NOT EXISTS blobs (
        -- Using UINT64 because the sequence is incremented even on "INSERT ... ON
        -- CONFLICT DO NOTHING". We could work around that and allocate blobids more
        -- manually (by removing duplicates ourselves) to avoid unused blobids.
        --
        -- This behavior is expected:
        -- https://github.com/duckdb/duckdb/issues/12540#issuecomment-2168127847
        -- https://stackoverflow.com/a/37206177
        --
        -- CREATE SEQUENCE seq;
        -- CREATE TABLE foo (a INTEGER DEFAULT nextval('seq'), b VARCHAR UNIQUE);
        -- INSERT OR IGNORE INTO foo (b) VALUES ('x');
        -- INSERT OR IGNORE INTO foo (b) VALUES ('x');
        -- INSERT OR IGNORE INTO foo (b) VALUES ('y');
        -- SELECT * FROM foo;
        -- ┌───────┬─────────┐
        -- │   a   │    b    │
        -- │ int32 │ varchar │
        -- ├───────┼─────────┤
        -- │     1 │ x       │
        -- │     3 │ y       │
        -- └───────┴─────────┘
        blobid UINT64 DEFAULT nextval('blobid_sequence'), -- TODO: primary key?
        blobhash VARCHAR UNIQUE,
        blobfilename VARCHAR,
        blobfamily blobfamily,
    );

    CREATE SEQUENCE IF NOT EXISTS versionid_sequence;

    -- versionname is the pretty name ("v1.2etc")
    -- versiontag is the Git tag
    CREATE TABLE IF NOT EXISTS versions (
        versionid UINT64 DEFAULT nextval('versionid_sequence'), -- TODO: primary key?
        versionname VARCHAR,
        versiontag VARCHAR,
        is_rc BOOL
    );

    -- db.vers :: versions -> list of (blobid, filepath)
    CREATE TABLE IF NOT EXISTS version_objects (
        versionid UINT64,
        blobid UINT64,
        filepath VARCHAR,
    );

    CREATE TABLE IF NOT EXISTS defs (
        -- TODO: use an ident table (identid UINT64, ident VARCHAR) and then use UINT64 for defname?
        -- The goals:
        --  - make table smaller so faster to index
        --  - make linear scan with filtering on defname much faster because zonemaps
        --    will contain defname IDs that are close to one another
        defname VARCHAR,
        blobid UINT64, -- TODO: declare as foreign key?
        deftype deftype,
        defline UINT32,
        blobfamily blobfamily,
    );

    CREATE TABLE IF NOT EXISTS refs (
        refname VARCHAR,
        blobid UINT64,
        blobfamily blobfamily,
        refline UINT32,
    );
    """)

    return ddb


def stage02_fill_blobs_table(ddb):
    # [closed, open) interval
    start_blobid = ddb.sql("SELECT max(blobid) FROM blobs;").fetchone()[0]
    start_blobid = (start_blobid or 0) + 1

    # TODO: don't do tags already indexed!!!
    #
    # TODO: I think this "INSERT OR IGNORE" is too costly, ideally we would be able to
    # parallelise part of it. This step is 100% on one thread and not much more. 19m52s
    # for full Linux kernel, all releases, on 2025-07-17.
    #
    # Add index on blobhash would help with the INSERT OR IGNORE? Ah no an index is
    # implicitely created for UNIQUE constraints. Maybe it is that index that slows down
    # the insertion process?
    versions = lib.scriptVersions()

    # TODO: is the dataframe really necessary?
    # Insertion order is important!!!
    ddb_data = pd.DataFrame(
        {
            "versionname": (version for _, version, _ in versions),
            "versiontag": (tag for tag, _, _ in versions),
            "is_rc": (is_rc for _, _, is_rc in versions),
        }
    )
    ddb.execute("INSERT INTO versions (versionname, versiontag, is_rc) FROM ddb_data")

    # Later on, code will need both the versionname AND the versionid.
    versions = ddb.execute(
        "SELECT versionid, versionname, versiontag FROM versions"
    ).fetchall()

    QUERY = "INSERT INTO blobs (blobhash, blobfilename, blobfamily) FROM ddb_data ON CONFLICT DO NOTHING"
    pool_and_write_output_to_db(ddb, stage02_worker, versions, QUERY)

    end_blobid = ddb.sql("SELECT max(blobid) FROM blobs;").fetchone()[0]
    end_blobid = (end_blobid or 0) + 1

    return versions, start_blobid, end_blobid


def stage02_worker(version):
    versionid, versionname, versiontag = version
    blobs = lib.scriptLines("list-blobs", "-f", versiontag)
    blobfilenames = [blob.split(b" ", maxsplit=1)[1] for blob in blobs]
    blobfamilies = (lib.getFileFamily(filename.decode()) for filename in blobfilenames)
    return pd.DataFrame(
        {
            "blobhash": (blob.split(b" ", maxsplit=1)[0] for blob in blobs),
            "blobfilename": blobfilenames,
            "blobfamily": pd.Series(blobfamilies, dtype=BLOBFAMILY_DTYPE),
        }
    )


stage03_ddb = None


def stage03_fill_version_objects_table(ddb, versions):
    global stage03_ddb

    # TODO: this stage is dominated by sed processes. We could probably do better; did I
    # not have a branch somewhere where I tuned it to perform better because the current
    # capturing is expensive? Or just do it from Python, maybe using numpy arrays.
    #
    # At a second stage the main thread kicks in and is at 100%.
    stage03_ddb = ddb
    QUERY = "INSERT INTO version_objects SELECT * FROM ddb_data"
    pool_and_write_output_to_db(ddb, stage03_worker, versions, QUERY)


def stage03_worker(version):
    global stage03_ddb

    versionid, versionname, versiontag = version
    blobs = lib.scriptLines("list-blobs", "-p", versiontag)

    # Create list of blobsids by doing a (single) database lookup. We need to turn a list
    # of blobhashes into a list of blobids. I haven't found anything better than building
    # a dict (blobhash -> blobid) then using that.
    blobhashes = [blob.split(b" ", maxsplit=1)[0] for blob in blobs]
    blobhashes = np.array(blobhashes, dtype="U")
    with stage03_ddb.cursor() as cursor:  # concurrent database access!
        blobhash_to_blobid = dict(
            cursor.sql("""SELECT blobhash, blobid FROM blobs
                          WHERE blobhash IN (SELECT * FROM blobhashes)""").fetchall()
        )

    # TODO: can we use the proper type for blobid? currently int64 versus uint32 ideally.
    return pd.DataFrame(
        {
            "versionid": versionid,
            "blobid": (blobhash_to_blobid[blobhash] for blobhash in blobhashes),
            "filepath": (blob.split(b" ", maxsplit=1)[1] for blob in blobs),
        }
    )


def stage04_fill_defs_table(ddb, start_blobid, end_blobid, timer):
    # New blobs that appeared AND from which we want to extract defs.
    blobs = ddb.execute(
        """SELECT blobid, blobhash, blobfilename, blobfamily FROM blobs
           WHERE blobid >= ? AND blobid < ? AND
                 blobfamily IS NOT NULL AND blobfamily != 'M';""",
        (start_blobid, end_blobid),
    ).fetchall()

    timer.init_done()

    QUERY = "INSERT INTO defs SELECT * FROM ddb_data"
    pool_and_write_output_to_db(ddb, stage04_worker, blobs, QUERY)


def stage04_worker(args):
    blobid, blobhash, blobfilename, blobfamily = args
    defnames, deftypes, deflines = [], [], []
    lines = lib.scriptLines("parse-defs", blobhash, blobfilename, blobfamily)
    for line in lines:
        defname, deftype, defline = line.split(b" ")
        if not lib.isIdent(defname):
            continue

        deftype = deftype.decode()
        # Not stored in previous version, TODO: check it makes sense. Else refuse it.
        if deftype not in DEFTYPES:
            continue

        defnames.append(defname.decode())
        deftypes.append(deftype)
        deflines.append(int(defline.decode()))

    if defnames:
        return pd.DataFrame(
            {
                "defname": defnames,
                "blobid": blobid,
                "deftype": deftypes,
                "defline": deflines,
                "blobfamily": pd.Series([blobfamily], dtype=BLOBFAMILY_DTYPE).repeat(
                    len(defnames)
                ),
            }
        )
    return None


# Sending those to each worker is too expensive; global means it gets shared by forking.
stage05_all_defnames = None
stage05_defs_in_blobs = None


def stage05_fill_refs_table(ddb, start_blobid, end_blobid, timer):
    global stage05_all_defnames, stage05_defs_in_blobs

    blobs = ddb.execute(
        """SELECT blobid, blobhash, blobfamily FROM blobs
           WHERE blobid >= ? AND blobid < ? AND
                 blobfamily IS NOT NULL;""",
        (start_blobid, end_blobid),
    ).fetchall()

    # TODO: we could reduce that if desired. Work with hashes (u64?) or use better
    # datastructures (eg would a probabilistic DS like a bloom filter be useful?).
    stage05_all_defnames = set(
        x for (x,) in ddb.execute("SELECT defname FROM defs").fetchall()
    )

    # TODO: if we do a from-scratch indexing, this will contain all defs. We should
    # instead work in blocks to avoid having the full list of defs in memory.
    stage05_defs_in_blobs = ddb.execute(
        """SELECT defname, blobid, defline FROM defs WHERE blobid >= ? AND blobid < ?""",
        (start_blobid, end_blobid),
    ).df()

    timer.init_done()

    QUERY = "INSERT INTO refs SELECT * FROM ddb_data"
    pool_and_write_output_to_db(ddb, stage05_worker, blobs, QUERY)


def stage05_worker(args):
    # Operations needed:
    #  - Is defname present in the defs database? stage05_all_defnames
    #  - Is there a def declared in blob at line X? stage05_defs_in_blobs
    global stage05_all_defnames, stage05_defs_in_blobs

    blobid, blobhash, blobfamily = args

    refnames = []
    reflines = []
    even = True
    lineno = 1
    for refname in lib.scriptLines("tokenize-file", "-b", blobhash, blobfamily):
        even = not even
        if even:
            refnames.append(refname.decode())
            reflines.append(lineno)
        else:
            lineno += refname.count(b"\1")

    if not refnames:
        return None

    refs = pd.DataFrame(
        {
            "refname": refnames,
            "refline": reflines,
        }
    )

    # Remove numbers (base10, base16) straight away.
    # TODO: remove known language keywords?
    refs = refs[~refs["refname"].str.match("^([0-9_]+|0[xX][0-9a-fA-F]+)$")]
    if blobfamily == "M":
        refs = refs[refs["refname"].str.startswith("CONFIG_")]
    if refs.empty:
        return None

    # Kconfig values are saved as CONFIG_<value>
    if blobfamily == "K":
        refs["refname"] = "CONFIG_" + refs["refname"]

    # Check a def exists for this ref. Notice we dedup refnames first. Remember that our
    # tokenizer will throw at us all "tokens" it detects, so keywords will be present
    # many times.
    defined_refnames = set(refs["refname"]) & stage05_all_defnames
    refs = refs[refs["refname"].isin(defined_refnames)]
    if refs.empty:
        return None

    # Drop refs on lines where there is a def.
    #
    # stage05_defs_in_blobs is a superset of the defs that are present in the current blob.
    # Do an initial filter so that further lookups don't have to go through each def.
    #
    # TODO: use np.unique() and np.searchsorted()?
    blobdefs = stage05_defs_in_blobs[stage05_defs_in_blobs["blobid"] == blobid]
    blobdefs = set(blobdefs[["defname", "defline"]].apply(tuple, axis=1))
    refs = set(refs.apply(tuple, axis=1)) - blobdefs
    if not refs:
        return None

    refs = list(refs)  # I am afraid iterating over a set is non deterministic.
    return pd.DataFrame(
        {
            "refname": [refname for (refname, refline) in refs],
            "blobid": blobid,
            "blobfamily": pd.Series([blobfamily], dtype=BLOBFAMILY_DTYPE).repeat(
                len(refs)
            ),
            "refline": [refline for (refname, refline) in refs],
        }
    )


def stage06_fill_comps_defs(ddb, start_blobid, end_blobid, timer):
    blobs = ddb.execute(
        """SELECT blobid, blobhash, blobfilename, blobfamily FROM blobs
           WHERE blobid >= ? AND blobid < ?
             AND blobfamily = 'C';""",
        (start_blobid, end_blobid),
    ).fetchall()

    timer.init_done()

    QUERY = "INSERT INTO defs SELECT * FROM ddb_data"
    pool_and_write_output_to_db(ddb, stage06_worker, blobs, QUERY)


def stage06_worker(args):
    blobid, blobhash, blobfilename, blobfamily = args
    compnames, complines = [], []
    lines = lib.scriptLines("parse-comps", blobhash, blobfamily)
    for line in lines:
        ident, lineno = line.split(b" ", 1)
        compnames.append(ident.decode())
        complines.append(int(lineno.decode()))
    if compnames:
        return pd.DataFrame(
            {
                "defname": compnames,
                "blobid": blobid,
                "deftype": ["compatible"] * len(compnames),
                "defline": complines,
                "blobfamily": pd.Series([blobfamily], dtype=BLOBFAMILY_DTYPE).repeat(
                    len(compnames)
                ),
            }
        )
    return None


stage07_all_compnames = None


def stage07_fill_comps_refs(ddb, start_blobid, end_blobid, timer):
    global stage07_all_compnames

    dts_blobs = ddb.execute(
        """SELECT blobid, blobhash, blobfamily FROM blobs
           WHERE blobid >= ? AND blobid < ?
             AND blobfamily = 'D';""",
        (start_blobid, end_blobid),
    ).fetchall()

    docs_blobs = ddb.execute(
        """SELECT DISTINCT b.blobid, b.blobhash, 'B' FROM blobs b
           INNER JOIN version_objects vo ON b.blobid = vo.blobid
           WHERE vo.filepath LIKE 'Documentation/devicetree/bindings/%'
             AND b.blobid >= ? AND b.blobid < ?;""",
        (start_blobid, end_blobid),
    ).fetchall()

    stage07_all_compnames = set(
        x
        for (x,) in ddb.execute(
            "SELECT DISTINCT defname FROM defs WHERE deftype = 'compatible'"
        ).fetchall()
    )

    timer.init_done()

    all_blobs = dts_blobs + docs_blobs
    QUERY = "INSERT INTO refs SELECT * FROM ddb_data"
    pool_and_write_output_to_db(ddb, stage07_worker, all_blobs, QUERY)


def stage07_worker(args):
    global stage07_all_compnames

    blobid, blobhash, blobfamily = args
    refnames, reflines = [], []
    lines = lib.scriptLines("parse-comps", blobhash, blobfamily)
    for line in lines:
        ident, lineno = line.split(b" ", 1)
        ident = ident.decode()
        if ident not in stage07_all_compnames:
            continue
        refnames.append(ident)
        reflines.append(int(lineno.decode()))
    if refnames:
        return pd.DataFrame(
            {
                "refname": refnames,
                "blobid": blobid,
                "blobfamily": pd.Series([blobfamily], dtype=BLOBFAMILY_DTYPE).repeat(
                    len(refnames)
                ),
                "refline": reflines,
            }
        )
    return None


def print_row(label, total_wallclock, init_wallclock, cpu_self, cpu_children):
    print(
        f"{label:20s} {total_wallclock:>15s} {init_wallclock:>15s} {cpu_self:>15s} {cpu_children:>15s}"
    )


class Section:
    def __init__(self, label):
        self.label = label

    def __enter__(self):
        self.t0 = time.perf_counter_ns()
        self.rusage_start_self = resource.getrusage(resource.RUSAGE_SELF)
        self.rusage_start_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        return self

    def init_done(self):
        self.t1 = time.perf_counter_ns()

    def __exit__(self, type, value, traceback):
        t2 = time.perf_counter_ns()

        wallclock = (t2 - self.t0) / 1e9

        a = self.rusage_start_self
        b = resource.getrusage(resource.RUSAGE_SELF)
        cpu_self = (b.ru_utime - a.ru_utime) + (b.ru_stime - a.ru_stime)
        cpu_self = cpu_self / wallclock

        a = self.rusage_start_children
        b = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_children = (b.ru_utime - a.ru_utime) + (b.ru_stime - a.ru_stime)
        cpu_children = cpu_children / wallclock

        print_row(
            self.label,
            f"{(t2 - self.t0) / 1e9:.0f}s",
            f"{(self.t1 - self.t0) / 1e9:.0f}s" if hasattr(self, "t1") else "",
            f"{cpu_self:.1f}x",
            f"{cpu_children:.1f}x",
        )


def main():
    # TODO: dump the CPU (usr/sys) time spent in: main process, Python children processes
    # and other subprocesses. Do so on a per-step basis.
    #
    # TODO: allow fast profiling of any step. It should use a tmp database, do previous
    # steps on a part of the data and finally call the profiled step using cprofiler. Or
    # it works with an existing database so that the data loaded is valid: eg for refs we
    # want a big defs database as without it the operations are really much easier.

    print_row("label", "wallclock", "init", "cpu_self", "cpu_children")

    with Section("total"):
        with Section("db-init"):
            ddb = stage01_ddb_init()
        with Section("blobs"):
            versions, start_blobid, end_blobid = stage02_fill_blobs_table(ddb)
        with Section("version-objects"):
            stage03_fill_version_objects_table(ddb, versions)
        with Section("defs") as timer:
            stage04_fill_defs_table(ddb, start_blobid, end_blobid, timer)
        with Section("refs") as timer:
            stage05_fill_refs_table(ddb, start_blobid, end_blobid, timer)
        with Section("comps-defs") as timer:
            stage06_fill_comps_defs(ddb, start_blobid, end_blobid, timer)
        with Section("comps-refs") as timer:
            stage07_fill_comps_refs(ddb, start_blobid, end_blobid, timer)


if __name__ == "__main__":
    main()
