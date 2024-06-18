#!/usr/bin/env python3

import data
import sys

assert len(sys.argv) >= 3

db_a = data.DB(sys.argv[1], readonly=True, dtscomp=0)
db_b = data.DB(sys.argv[2], readonly=True, dtscomp=0)

def to_str(x):
    if x is int:
        return str(x)
    elif x is bytes:
        return x.decode()
    else:
        return str(x)

def compare(name, a, b):
    keys_a = sorted(a.get_keys())
    keys_b = sorted(b.get_keys())

    if len(keys_a) != len(keys_b):
        print(f'{name}: {len(keys_a)} keys in A (/tmp/keys_{name}_a), {len(keys_b)} keys in B (/tmp/keys_{name}_b)')
        with open(f'/tmp/keys_{name}_a', 'wb') as f:
            f.write(b'\n'.join(sorted(keys_a)))
        with open(f'/tmp/keys_{name}_b', 'wb') as f:
            f.write(b'\n'.join(sorted(keys_b)))
        return

    for key_a, key_b in zip(keys_a, keys_b):
        if key_a != key_b:
            print(f'{name}: found key {key_a} in A, found key {key_b} in B')
            continue

        val_a = a.get(key_a)
        val_b = b.get(key_a)

        if getattr(val_a, 'pack', None) is not None:
            val_a = val_a.pack()
            val_b = val_b.pack()

        if val_a != val_b:
            if val_a is bytes and val_b is bytes and (b'\n' in val_a or b'\n' in val_b):
                filepath_a = f'/tmp/cmp_{name}_{key_a.decode()}_a'
                filepath_b = f'/tmp/cmp_{name}_{key_a.decode()}_b'
                with open(filepath_a, 'wb') as f:
                    f.write(val_a)
                with open(filepath_b, 'wb') as f:
                    f.write(val_b)
                print(f'{name}: values for key {key_a} are different, see {filepath_a} and {filepath_b}')
            else:
                print(f'{name}: values for key {key_a} are different: "{to_str(val_a)}" vs "{to_str(val_b)}"')
            continue

    print(f'{name}: done')

def dump(db):
    for key in db.get_keys():
        val = db.get(key)
        if getattr(val, 'pack', None) is not None:
            val = val.pack()
        print(f'{key.decode()}={val}')

if len(sys.argv) == 4:
    import IPython
    IPython.embed(colors="neutral")
    exit(0)

compare('vars', db_a.vars, db_b.vars) # single key: numBlobs=
compare('blob', db_a.blob, db_b.blob) # map blob to idx
compare('hash', db_a.hash, db_b.hash) # map idx to blob
compare('file', db_a.file, db_b.file) # map idx to filename (no directory)
compare('vers', db_a.vers, db_b.vers) # map version to multiline string, each line is idx+filepath
compare('defs', db_a.defs, db_b.defs) # map definition name to (idx, type, line, family) list
compare('refs', db_a.refs, db_b.refs) # map definition name to (idx, lines, family) list
compare('docs', db_a.docs, db_b.docs) # ?

# That means we can fill in vars+blob+hash+file+vers without any threading,
# quickly. Then we do all defs. Then all refs. Then all docs. Maybe doc can be
# done during refs.
