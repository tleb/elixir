#!/usr/bin/env python3

import sys

import data
import lib

dts_comp_support = int(lib.script("dts-comp"))

db = data.DB(lib.getDataDir(), readonly=True, dtscomp=dts_comp_support)


def dump_db(name, dbx):
    print(f"{name}")

    keys = dbx.get_keys()
    keys.sort()
    for key in keys:
        val = dbx.db.get(key)
        print(f"    {key.decode()}={val}")


dump_db("vars", db.vars)
dump_db("blob", db.blob)
dump_db("hash", db.hash)
dump_db("file", db.file)
dump_db("vers", db.vers)
dump_db("defs", db.defs)
dump_db("refs", db.refs)
dump_db("docs", db.docs)
if dts_comp_support:
    dump_db("comps", db.comps)
    dump_db("comps_docs", db.comps_docs)
