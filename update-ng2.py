#!/usr/bin/env python3

import multiprocessing
import subprocess
import time
import tqdm

import lib
import data

dts_comp_support = int(lib.script("dts-comp"))
assert dts_comp_support == 0

db = data.DB(lib.getDataDir(), readonly=False, dtscomp=dts_comp_support)


# Take a list of commands to run, and spawn processes in parallel.
# cmds is a list of (arguments as array, stdout destination filepath) tuples.
# It displays a progress bar on stdout using tqdm.
def run_commands(cmds):
    target_process_count = 1.5 * multiprocessing.cpu_count()
    running_processes = []
    pbar = tqdm.tqdm(desc='blobs', total=len(cmds), leave=False)

    assert target_process_count >= 1

    # TODO: add timeout

    for args, stdout_dst in cmds:
        # Wait until a process is done, to replace it.
        while len(running_processes) >= target_process_count:
            for i, p in enumerate(running_processes):
                if p.poll() is not None:
                    assert p.returncode == 0 # TODO: reliability
                    running_processes[i] = None
                    pbar.update()

            running_processes = list(filter(lambda p: p is not None, running_processes))

        with open(stdout_dst, 'w') as stdout_fd:
            running_processes.append(subprocess.Popen(args, stdout=stdout_fd))

    for p in running_processes:
        p.wait()
        pbar.update()

    pbar.close()

    # TODO: wrap in a finally that does cleanup, ie send kill, wait a bit, then
    # kill really

def get_new_tags():
    for tag in lib.scriptLines("list-tags"):
        if not db.vers.exists(tag):
            yield tag

def get_blobs_cmds(new_tags):
    for new_tag in new_tags:
        new_tag = new_tag.decode()
        yield (
            ('./script.sh', 'list-blobs', '-p', new_tag),
            f'lxr-blobs-{new_tag}.txt',
        )


new_tags = list(get_new_tags())

run_commands(list(get_blobs_cmds(new_tags)))



# Maybe replace this whole script by a script generating a Ninja build script?
