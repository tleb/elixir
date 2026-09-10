# Port of t/100-basic.t: test basic elixir functions against the files in
# t/tree. The perl suite asserted through the utils/query.py CLI; these
# tests assert the same results through elixir.query.Query directly (what
# query.py itself calls). The `script.sh list-tags` sanity check became a
# direct `git tag` on the test repository.
#
# DTS coverage (the series decided this tree must exercise it, as the musl
# determinism baseline cannot): definitions and references in .dts/.dtsi
# files, and DT compatible strings (families B and D). Beyond the authored
# testproj fixtures, real Linux v7.3-rc2 material (provenance and licensing
# in README.adoc): the N800/N810 boards share omap2420-n8x0-common.dtsi
# over the omap2420 SoC dtsi, and the i2c-cbus-gpio, nokia,retu and
# regulator-fixed compatible strings each close a real triangle: a C
# driver match table, a devicetree use and a bindings document.
#
# This file is part of Elixir, a source code cross-referencer.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Elixir is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import TAG
from elixir import data

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deflist_families_order_is_arrival_independent():
    'Canonical record bytes regardless of append order (thread scheduling)'
    def built_in(calls):
        d = data.DefList()
        for id, family in calls:
            d.append(id, 'variable', 2, family)
        return d.pack()

    assert built_in([(1, 'C'), (2, 'D')]) == built_in([(2, 'D'), (1, 'C')])
    assert built_in([(1, 'C'), (2, 'D')]).endswith(b'#C,D')
    # Single family stays as-is
    assert built_in([(1, 'D')]).endswith(b'#D')


def search(query, ident, family):
    defs, refs, docs, exists = query.search_ident('v5.4', ident, family)
    return ([(s.path, s.line, s.type) for s in defs],
            [(s.path, s.line) for s in refs],
            [(s.path, s.line) for s in docs],
            exists)


# One tag, and it is the one build_repo created. TestEnvironment checked
# this through `script.sh list-tags` before building the database.
def test_one_tag(testenv):
    result = subprocess.run(['git', '-C', testenv.repo_dir, 'tag'],
                            stdout=subprocess.PIPE, universal_newlines=True)
    assert result.returncode == 0
    assert result.stdout.split() == [TAG]


# The database has the files we expect; testproj indexes compatible
# strings, so it also has the DT binding databases
def test_db_files(testenv):
    data_dir = Path(testenv.data_dir)
    for name in ['blobs.db', 'definitions.db', 'filenames.db', 'hashes.db',
                 'references.db', 'variables.db', 'versions.db',
                 'doccomments.db', 'compatibledts.db', 'compatibledts_docs.db',
                 'definitions-cache-C.db', 'definitions-cache-K.db',
                 'definitions-cache-D.db', 'definitions-cache-M.db']:
        assert (data_dir / name).is_file(), name


# A currentTag marker for a tag that is not in versions.db means an
# interrupted run: update.py must refuse the data directory rather than
# silently keep the partially indexed tag. The crashed run's walk
# scratch is swept even then: the sweep runs before the marker check
# and must not interact with it
def test_update_refuses_interrupted_run(build_env, tmp_path):
    env = build_env(tmp_path)
    current_tag_marker(env, 'v9.9')
    (Path(env.data_dir) / 'tmp-blobwalk-lists').write_bytes(b'stale')
    (Path(env.data_dir) / 'tmp-blobwalk-seen.db').write_bytes(b'stale')

    result = update(env)
    assert result.returncode != 0, result.stdout
    assert 'interrupted run' in result.stdout
    assert not list(Path(env.data_dir).glob('tmp-blobwalk-*'))


# A marker left over from a crash after the tag was committed is cleaned
# up, not mistaken for an interrupted run. No tags left to index: the
# early-exit path never creates the walk's scratch artifacts
def test_update_cleans_stale_marker(build_env, tmp_path):
    env = build_env(tmp_path)
    current_tag_marker(env, TAG)

    result = update(env)
    assert result.returncode == 0, result.stdout
    assert '0 new tags' in result.stdout
    assert not list(Path(env.data_dir).glob('tmp-blobwalk-*'))


# Leftovers of a crashed run's upfront walk are swept at startup, and
# a full run (walk, indexing) deletes its own scratch at the end
def test_update_sweeps_stale_walk_scratch(build_env, tmp_path):
    env = build_env(tmp_path)
    git = ['git', '-C', str(env.repo_dir), '-c', 'user.name=test',
           '-c', 'user.email=test']
    (Path(env.repo_dir) / 'stale.c').write_text('int stale;\n')
    subprocess.run(git + ['add', '.'], check=True)
    subprocess.run(git + ['commit', '-m', 'stale'], check=True)
    subprocess.run(git + ['tag', 'v5.5'], check=True)
    (Path(env.data_dir) / 'tmp-blobwalk-lists').write_bytes(b'stale')
    (Path(env.data_dir) / 'tmp-blobwalk-seen.db').write_bytes(b'stale')

    result = update(env)
    assert result.returncode == 0, result.stdout
    assert 'walk done: 1 tags' in result.stdout
    assert not list(Path(env.data_dir).glob('tmp-blobwalk-*'))


def update(env):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / 'update.py'), '4'],
        env=env.env(), cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True)


# The indexing log: every line timestamped by the parent (the single
# writer), one walk-done line for the upfront walk, one tag line per
# completed tag and one machine-parseable SUMMARY JSON line at the end
def test_update_log_format(build_env, tmp_path):
    env = build_env(tmp_path)
    # Three more tags with fresh blobs each, so the last tag line
    # (4/4) exists to pin the no-ETA end of the run and the first
    # (1/4) carries the ETA clause from tag one
    git = ['git', '-C', str(env.repo_dir), '-c', 'user.name=test',
           '-c', 'user.email=test']
    for i in (2, 3, 4):
        (Path(env.repo_dir) / ('extra%d.c' % i)).write_text('int v%d;\n' % i)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-m', 'tag %d' % i], check=True)
        subprocess.run(git + ['tag', 'v5.%d' % (i + 3)], check=True)

    # Re-index from scratch with the output captured: build_env's
    # own update.py run happened before the fixture returned its env
    shutil.rmtree(env.data_dir)
    os.mkdir(env.data_dir)

    result = update(env)
    assert result.returncode == 0, result.stdout

    # Run start line: timestamp, project, tag count
    assert re.search(r'^\[\d\d:\d\d:\d\d\] testproj: 4 new tags \(repo .+, data .+, 4 threads\)$',
                     result.stdout, re.M), result.stdout

    # Walk-done line: the upfront walk's totals. The t/tree walk is
    # faster than the progress interval, so no "walking tags" line
    assert re.search(r'^\[\d\d:\d\d:\d\d\] walk done: 4 tags, \d+ blobs walked, \d+ new, \S+$',
                     result.stdout, re.M), result.stdout

    # Tag line: timestamp, project tag (n/N), blobs, seconds, phases.
    # The run ETA is exact from the first completed tag (the walk
    # counted the total new blobs): cumulative rate over blobs left
    assert re.search(r'^\[\d\d:\d\d:\d\d\] testproj v5\.4 \(1/4\): '
                     r'\d+ blobs, \S+ \(defs \S+ docs \S+ comps \S+ refs \S+ comps_docs \S+\)'
                     r' — \d+ blobs/s, \d+ blobs left, ETA \S+$',
                     result.stdout, re.M), result.stdout

    # The last tag line ends after the phases: no ETA with nothing left
    assert re.search(r'^\[\d\d:\d\d:\d\d\] testproj v5\.7 \(4/4\): '
                     r'\d+ blobs, \S+ \(defs \S+ docs \S+ comps \S+ refs \S+ comps_docs \S+\)$',
                     result.stdout, re.M), result.stdout

    # The machine line parses as JSON with every phase and the walk's
    # counters (total_new cross-checks phase 1's count: the walk's
    # new-blob condition is exactly what update_blob_ids applies)
    line = next(l for l in result.stdout.splitlines() if 'SUMMARY {' in l)
    summary = json.loads(line.split('SUMMARY ', 1)[1])
    assert summary['project'] == 'testproj'
    assert summary['tags'] == 4
    assert summary['blobs'] > 0
    assert summary['total_new'] == summary['blobs']
    assert summary['walked_blobs'] > summary['blobs'] # tags share blobs
    assert summary['walk_s'] > 0
    assert summary['wall_s'] > 0
    assert summary['blobs_per_s'] > 0
    assert set(summary['phases']) == {'ids', 'vers', 'defs', 'docs', 'comps',
                                     'refs', 'comps_docs'}
    assert summary['phases']['defs'] > 0
    assert summary['phases']['refs'] > 0
    assert isinstance(summary['lexer_errors'], int)
    assert isinstance(summary['ctags_notices'], int)

    # The walk's scratch artifacts are gone once the run succeeded
    assert not list(Path(env.data_dir).glob('tmp-blobwalk-*'))

    # t/tree phases are all faster than the 5 s progress interval:
    # no in-phase progress lines (percent-done blobs counters), only
    # one line per tag plus the final summary lines
    assert not re.search(r'\d+% \d+/\d+ blobs', result.stdout)


def current_tag_marker(env, tag):
    """Leave the marker an update.py killed mid-tag would leave, the way
    the perl test did through a python one-liner"""
    from elixir import data
    db = data.DB(env.data_dir, readonly=False, shared=True, dtscomp=False)
    db.vars.put(b'currentTag:' + tag.encode(), b'1')
    db.close()


# Spot-check some identifiers
def test_ident_nonexistent(query):
    defs, refs, docs, exists = search(query, 'SOME_NONEXISTENT_IDENTIFIER_XYZZY_PLUGH', 'C')
    assert not exists and defs == [] and refs == [] and docs == []


def test_ident_i2c_acpi_notify(query):
    defs, refs, docs, exists = search(query, 'i2c_acpi_notify', 'C')
    assert exists
    assert defs == [('drivers/i2c/i2c-core-acpi.c', 402, 'function')]
    assert refs == [('drivers/i2c/i2c-core-acpi.c', '439')]


def test_ident_class_131(query):
    # #131: definitions and references in headers work
    defs, refs, docs, exists = search(query, 'class', 'C')
    assert exists
    assert ('issue131.h', 9, 'struct') in defs
    assert ('issue131.h', '13') in refs


def test_ident_memset_150(query):
    # #150: definitions in assembly are found
    defs, refs, docs, exists = search(query, 'memset', 'C')
    assert exists
    assert defs == [('issue150.S', 7, 'function')]
    assert ('drivers/i2c/i2c-core-acpi.c', '121,185,344,473') in refs


def test_ident_hypercall_paste_150(query):
    # #150: ENTRY(HYPERVISOR_##hypercall) is not a definition
    defs, refs, docs, exists = search(query, 'HYPERVISOR_##hypercall', 'C')
    assert not any('hypercall.S' in p for p, _, _ in defs)


def test_ident_hex_number_150(query):
    # #150: numbers are not definitions
    defs, refs, docs, exists = search(query, '0xfffffffe', 'C')
    assert not any('bcm74xx_sprom.c' in p for p, _, _ in defs)


def test_ident_syscall_define_228(query):
    # #228: SYSCALL_DEFINE produces sys_* definitions
    defs, refs, docs, exists = search(query, 'sys_init_module', 'C')
    assert defs == [('syscall_define.c', 1, 'function')]


# Kconfig options: definitions from Kconfig files, references from
# Makefiles (family M) and Kconfig files (family K), including files
# in subdirectories
def test_ident_kconfig_option(query):
    defs, refs, docs, exists = search(query, 'CONFIG_TESTOPT_FOO', 'K')
    assert defs == [('Kconfig', 2, 'config')]
    assert refs == [('Makefile', '3'), ('drivers/Kconfig', '4'), ('drivers/Makefile', '4')]


def test_ident_kconfig_option_subdir(query):
    defs, refs, docs, exists = search(query, 'CONFIG_TESTOPT_BAR', 'K')
    assert defs == [('drivers/Kconfig', 2, 'config')]
    assert refs == [('drivers/Makefile', '3')]


# Devicetree: labels are definitions, phandle references are references
# (across .dts/.dtsi files)
def test_ident_dts_label_and_reference(query):
    defs, refs, docs, exists = search(query, 'led0', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/testproj-board.dts', 12, 'label')]
    assert refs == [('arch/arm/boot/dts/testproj-board.dtsi', '13')]

    defs, refs, docs, exists = search(query, 'testgpio', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/testproj-board.dtsi', 7, 'label')]
    assert refs == [('arch/arm/boot/dts/testproj-board.dts', '14')]


# DT compatible strings (family B): defined by .compatible = "..." in C
# files, used in .dts files, documented under Documentation/devicetree/
# bindings (populated only for strings that exist as comps)
def test_compatible_c_dts_and_bindings(query):
    defs, refs, docs, exists = search(query, 'vendor,thing', 'B')
    assert exists
    assert defs == [('drivers/i2c/i2c-boardinfo.c', '107', 'compatible')]
    assert docs == [('Documentation/devicetree/bindings/vendor,thing.yaml', '4,19')]


def test_compatible_dts_only(query):
    defs, refs, docs, exists = search(query, 'vendor,testproj-board', 'B')
    assert exists
    assert defs == []
    assert refs == [('arch/arm/boot/dts/testproj-board.dts', '7')]
    assert docs == []


# The real n8x0 devicetrees: labels defined in the SoC dtsi are
# referenced from the shared board dtsi and the board dts files, as
# phandles (&gpioN inside <...>) and as whole-node references (&mcbsp2)
def test_ident_dts_label_soci_referenced_by_board_files(query):
    defs, refs, docs, exists = search(query, 'gpio3', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/ti/omap/omap2420.dtsi', 112, 'label')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n8x0-common.dtsi', '17,18,19,120')]

    # gpio4 is referenced from two files: the shared dtsi and one board
    defs, refs, docs, exists = search(query, 'gpio4', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/ti/omap/omap2420.dtsi', 124, 'label')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '51'),
                    ('arch/arm/boot/dts/ti/omap/omap2420-n8x0-common.dtsi', '25,121')]


def test_ident_dts_label_node_reference(query):
    # &mcbsp2: a whole-node override of a SoC peripheral in the board dts
    defs, refs, docs, exists = search(query, 'mcbsp2', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/ti/omap/omap2420.dtsi', 165, 'label')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '70')]

    # a board-local label referenced only inside its own file
    defs, refs, docs, exists = search(query, 'v28_aic', 'D')
    assert exists
    assert defs == [('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', 17, 'label')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '59,60')]


# The multi-family record: one ident with a C definition AND a dts label
# (the add_family canonical-order case the linux determinism runs found
# on gpio_clk/i2c0_clk/i2c1_clk; testproj-i2c.dts authors the collision,
# as struct i2c_dev already exists in the real i2c-dev.c)
def test_ident_dts_label_and_c_definition(query, testenv):
    defs, refs, docs, exists = search(query, 'i2c_dev', 'A')
    assert exists
    assert defs == [('drivers/i2c/i2c-dev.c', 40, 'struct'),
                    ('arch/arm/boot/dts/testproj-i2c.dts', 14, 'label')]

    # each family view filters to its own definition
    defs, _, _, _ = search(query, 'i2c_dev', 'C')
    assert defs == [('drivers/i2c/i2c-dev.c', 40, 'struct')]
    defs, refs, _, _ = search(query, 'i2c_dev', 'D')
    assert defs == [('arch/arm/boot/dts/testproj-i2c.dts', 14, 'label')]
    assert refs == []

    # the stored record itself carries both families
    db = data.DB(testenv.data_dir, readonly=True, dtscomp=False)
    try:
        assert db.defs.get('i2c_dev').get_families() == ['C', 'D']
    finally:
        db.close()


# The real compatible-string triangles: defined by a C driver match
# table, used by the real devicetrees, documented under bindings/ (the
# CBUS and Retu strings even have TWO documents each: their own binding
# and the other binding's example)
def test_compatible_i2c_cbus_gpio(query):
    defs, refs, docs, exists = search(query, 'i2c-cbus-gpio', 'B')
    assert exists
    assert defs == [('drivers/i2c/busses/i2c-cbus-gpio.c', '259', 'compatible')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n8x0-common.dtsi', '16')]
    assert docs == [('Documentation/devicetree/bindings/i2c/i2c-cbus-gpio.txt', '1,4,15'),
                    ('Documentation/devicetree/bindings/mfd/retu.txt', '16')]


def test_compatible_nokia_retu(query):
    defs, refs, docs, exists = search(query, 'nokia,retu', 'B')
    assert exists
    assert defs == [('drivers/mfd/retu-mfd.c', '310', 'compatible')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n8x0-common.dtsi', '24')]
    assert docs == [('Documentation/devicetree/bindings/i2c/i2c-cbus-gpio.txt', '24'),
                    ('Documentation/devicetree/bindings/mfd/retu.txt', '9,19')]


def test_compatible_regulator_fixed_yaml(query):
    # the .yaml side of the bindings mix (the .txt side is above)
    defs, refs, docs, exists = search(query, 'regulator-fixed', 'B')
    assert exists
    assert defs == [('drivers/regulator/fixed.c', '361', 'compatible')]
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '11,18')]
    assert docs == [('Documentation/devicetree/bindings/regulator/fixed-regulator.yaml', '53,126')]


def test_compatible_driver_and_bindings_without_dts(query):
    'match-table strings no devicetree in the tree uses'
    defs, refs, docs, exists = search(query, 'nokia,tahvo', 'B')
    assert exists
    assert defs == [('drivers/mfd/retu-mfd.c', '311', 'compatible')]
    assert refs == []
    assert docs == [('Documentation/devicetree/bindings/mfd/retu.txt', '9')]

    defs, refs, docs, exists = search(query, 'regulator-fixed-clock', 'B')
    assert exists
    assert defs == [('drivers/regulator/fixed.c', '365', 'compatible')]
    assert refs == []
    assert docs == [('Documentation/devicetree/bindings/regulator/fixed-regulator.yaml', '25,54,69,70,138')]


def test_compatible_board_strings_across_dts(query):
    'every quoted string of a compatible list, in every board file'
    defs, refs, docs, exists = search(query, 'nokia,n8x0', 'B')
    assert exists
    assert defs == [] and docs == []
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n800.dts', '8'),
                    ('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '8')]

    # the SoC string also appears in the SoC dtsi itself
    defs, refs, docs, exists = search(query, 'ti,omap2420', 'B')
    assert exists
    assert defs == [] and docs == []
    assert refs == [('arch/arm/boot/dts/ti/omap/omap2420-n800.dts', '8'),
                    ('arch/arm/boot/dts/ti/omap/omap2420-n810.dts', '8'),
                    ('arch/arm/boot/dts/ti/omap/omap2420.dtsi', '11')]


# Spot-check some files (the perl suite ran `query.py file`; it prints
# the tokenized file, like get_tokenized_file)
def test_file_nonexistent(query):
    assert query.get_tokenized_file('v5.4', '/SOME_NONEXISTENT_FILENAME_XYZZY_PLUGH') == ''


def test_file_c(query):
    code = query.get_tokenized_file('v5.4', '/drivers/i2c/i2c-dev.c')
    assert 'i2c-dev.c' in code
    assert 'Vogl' in code


def test_file_h(query):
    code = query.get_tokenized_file('v5.4', '/drivers/i2c/i2c-core.h')
    assert 'i2c-core.h' in code
    assert 'We' in code
