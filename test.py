import os
import subprocess
import multiprocessing
import tqdm

import lib
import data

dts_comp_support = int(lib.script("dts-comp"))
assert dts_comp_support == 0

db = data.DB(lib.getDataDir(), readonly=False, dtscomp=dts_comp_support)

def get_new_tags():
    for tag in lib.scriptLines("list-tags"):
        if not db.vers.exists(tag):
            yield tag

def run_cmd(arg):
    cmd, stdout_dest = arg
    with open(stdout_dest, 'w') as f:
        p = subprocess.run(cmd, stdout=f)

    # TODO: this is not really resilient
    assert p.returncode == 0

new_tags = list(get_new_tags())
cmds = []
for new_tag in new_tags:
    cmds.append((('./script.sh', 'list-blobs', '-p', new_tag), f"/tmp/blobs-{new_tag}.txt"))

# TODO: creating subprocesses to then create subprocesses is not useful. But I
# don't see any other abstraction that allows doing this easily.
with multiprocessing.Pool() as pool:
    it = pool.imap_unordered(run_cmd, cmds)
    it = tqdm.tqdm(it, desc="blobs", total=len(cmds), leave=False)
    for x in it:
        pass
