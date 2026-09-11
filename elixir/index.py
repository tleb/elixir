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

# The project bootstrap layer, ported from the utils/index shell
# script: create a project's <project>/{data,repo} layout around a bare
# repository, manage its remotes (remote0, remote1, ..., added
# idempotently by URL), fetch everything, and run the indexer. The
# folded pipeline — fetch then elixir.update.run() in-process, twice
# when starting from scratch — is project_index(); run() loops it over
# the requested projects (or every subdirectory of the root) and keeps
# going past failures. Custom remotes go through the `remote add` CLI
# subcommand or project_add_remote() directly.

import os
import re
import subprocess
import sys

from elixir import update

# The default remotes of every known project, ported verbatim from
# utils/index. index ensures them (idempotently) before the first
# fetch; extra remotes are the user's, via `remote add`.
DEFAULT_REMOTES = {
    'amazon-freertos': ('https://github.com/aws/amazon-freertos.git',),
    'arm-trusted-firmware': ('https://github.com/ARM-software/arm-trusted-firmware',),
    'barebox': ('https://git.pengutronix.de/git/barebox',),
    'busybox': ('https://git.busybox.net/busybox',),
    'coreboot': ('https://review.coreboot.org/coreboot.git',),
    'dpdk': ('https://dpdk.org/git/dpdk',
             'https://dpdk.org/git/dpdk-stable'),
    'glibc': ('https://sourceware.org/git/glibc.git',),
    'llvm': ('https://github.com/llvm/llvm-project.git',),
    'mesa': ('https://gitlab.freedesktop.org/mesa/mesa.git',),
    'musl': ('https://git.musl-libc.org/git/musl',),
    'ofono': ('https://git.kernel.org/pub/scm/network/ofono/ofono.git',),
    'op-tee': ('https://github.com/OP-TEE/optee_os.git',),
    'qemu': ('https://gitlab.com/qemu-project/qemu.git',),
    'u-boot': ('https://source.denx.de/u-boot/u-boot.git',),
    'uclibc-ng': ('https://cgit.uclibc-ng.org/cgi/cgit/uclibc-ng.git',),
    'zephyr': ('https://github.com/zephyrproject-rtos/zephyr',),
    'toybox': ('https://github.com/landley/toybox.git',),
    'grub': ('https://git.savannah.gnu.org/git/grub.git',),
    'bluez': ('https://git.kernel.org/pub/scm/bluetooth/bluez.git',),
    'linux': ('https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git',
              'https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git',
              'https://github.com/bootlin/linux-history.git'),
    'xen': ('https://xenbits.xen.org/git-http/xen.git',),
    'freebsd': ('https://git.freebsd.org/src.git',),
    'opensbi': ('https://github.com/riscv-software-src/opensbi',),
    'iproute2': ('https://git.kernel.org/pub/scm/network/iproute2/iproute2.git',),
    'vpp': ('https://gerrit.fd.io/r/vpp',),
}

# Remote names are remote<N>, N counting from 0
_remote_re = re.compile(r'remote(\d+)$')

def _git(repo_dir, *args, check=True, capture=False):
    '''Run git in a project's repository. safe.directory keeps the
    commands working when the repository is owned by another user
    (the deployment case: root indexes what the fetcher user cloned).'''
    return subprocess.run(
        ['git', '-C', repo_dir, '-c', 'safe.directory=' + repo_dir, *args],
        check=check, capture_output=capture, text=True)

def project_init(proj_dir):
    '''Create the <project>/{data,repo} layout. update expects the data
    directory to exist (mkdir is idempotent and must also run for
    already-initialized repositories); the bare repository is created
    only if `git tag -n1` fails on it — unlike `git status`, it works
    on bare repositories, so it detects both a missing and an existing
    bare repo.'''
    os.makedirs(os.path.join(proj_dir, 'data'), exist_ok=True)
    repo_dir = os.path.join(proj_dir, 'repo')
    if _git(repo_dir, 'tag', '-n1', check=False, capture=True).returncode == 0:
        return
    os.makedirs(repo_dir, exist_ok=True)
    _git(repo_dir, '-c', 'init.defaultBranch=main', 'init', '--bare')

def project_add_remote(proj_dir, url):
    '''Add url to the repository's remotes, unless some remote already
    has it (idempotent by URL, not by name). New remotes are named
    remote<N>, N one past the highest existing remote<N> index.'''
    repo_dir = os.path.join(proj_dir, 'repo')
    highest = -1
    for remote in _git(repo_dir, 'remote', capture=True).stdout.split():
        urls = _git(repo_dir, 'remote', 'get-url', remote,
                    check=False, capture=True).stdout.split()
        if url in urls:
            return
        m = _remote_re.match(remote)
        if m:
            highest = max(highest, int(m.group(1)))
    _git(repo_dir, 'remote', 'add', 'remote%d' % (highest + 1), url)

def project_fetch(proj_dir):
    '''Fetch every remote (with tags), then garbage-collect:
    aggressive if a past gc failed (it left a gc.log behind) or the
    ELIXIR_GC flag asks for one, else --auto — fetch is not a
    porcelain command, gc would never get an occasion to trigger.'''
    repo_dir = os.path.join(proj_dir, 'repo')
    _git(repo_dir, 'fetch', '--all', '--tags', '-j4')
    if os.path.exists(os.path.join(repo_dir, 'gc.log')) or os.environ.get('ELIXIR_GC'):
        _git(repo_dir, 'gc', '--aggressive')
    else:
        _git(repo_dir, 'gc', '--auto')

def project_index(proj_dir):
    '''The folded pipeline for one project: init, ensure the default
    remotes of the DEFAULT_REMOTES table (idempotent), then fetch and
    update. A from-scratch index (the data directory holds no file at
    all yet) runs fetch+update twice: the first pass took so long that
    the remotes may well have moved by the end of it.'''
    name = os.path.basename(proj_dir)
    project_init(proj_dir)
    for url in DEFAULT_REMOTES.get(name, ()):
        project_add_remote(proj_dir, url)

    data_dir = os.path.join(proj_dir, 'data')
    from_scratch = not any(files for _, _, files in os.walk(data_dir))
    for _ in range(2 if from_scratch else 1):
        print('%s: fetching' % name, flush=True)
        project_fetch(proj_dir)
        print('%s: indexing' % name, flush=True)
        update.run(os.path.join(proj_dir, 'repo'), data_dir)

def run(root, names):
    '''Index the given projects under root; names None means every
    direct subdirectory of root, whatever it is. A project failing
    with UpdateError or a git error is logged and skipped, the rest
    still runs; the list of failed names is returned so the caller
    can exit non-zero.'''
    if names is None:
        names = sorted(e.name for e in os.scandir(root) if e.is_dir())
    failed = []
    for name in names:
        try:
            project_index(os.path.join(root, name))
        except (update.UpdateError, subprocess.CalledProcessError) as e:
            print('elixir: index %s: %s' % (name, e), file=sys.stderr, flush=True)
            failed.append(name)
    return failed
