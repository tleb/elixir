#!/usr/bin/env python3

import sys
import re

import data
import lib

dts_comp_support = int(lib.script("dts-comp"))

db = data.DB(lib.getDataDir(), readonly=True, dtscomp=dts_comp_support)


# b'185:134,135,136,137,138,146:C\n1102:134,135,136,137,138,146:C\n'
def parse_refs(ref_list):
    for entry in ref_list.split(b"\n")[:-1]:
        idx, lines, _ = entry.split(b":")
        for lineno in lines.split(b','):
            yield (int(idx.decode()), int(lineno.decode()))

deflist_regex = re.compile(r"^(\d+)(\w)(\d+)(\w)$")

# b'433t12C,849t36C,1293t43C,1445t43C#C'
def parse_defs(def_list):
    if def_list is None or def_list == b"" or def_list == b"#":
        return

    # Drop families.
    def_list = def_list[: def_list.find(b"#")]

    for entry in def_list.split(b','):
        m = deflist_regex.match(entry.decode())
        assert m is not None
        idx, def_type, lineno, _ = m.groups()
        yield (int(idx), def_type, int(lineno))


ref_idents = db.refs.get_keys()
ref_idents.sort()
for ref_ident in ref_idents:
    defs = db.defs.db.get(ref_ident)
    refs = db.refs.db.get(ref_ident)
    assert defs is not None and refs is not None

    defs = list(parse_defs(defs))

    # Check there is no def+ref for the same ident on the same line, in the same blob.
    for idx, lineno in parse_refs(refs):
        for def_idx, def_type, def_lineno in defs:
            if def_idx == idx and def_lineno == lineno:
                print(f'duplicate! idx {idx} line {lineno} has both a def and ref for {ref_ident}')

    # Check at least one def is present.
    def_idxes = [def_idx for def_idx, _, _ in defs]



    # refs = list(parse_refs(refs))
    # print(ref_ident, defs, refs)





def dump_db(name, dbx):
    print(f"{name}")

    keys = dbx.get_keys()
    keys.sort()
    for key in keys:
        val = dbx.db.get(key)
        print(f"    {key.decode()}={val}")
