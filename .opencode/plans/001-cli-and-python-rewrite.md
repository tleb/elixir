# Plan: Unified `./elixir` CLI + Eliminate `script.sh` + Drop Env Vars

## Preamble

### Project

Elixir is a source code cross-referencer (think bootlin.com's Elixir). It indexes source
code from git repositories and serves a web interface to browse definitions, references,
and file contents. The codebase lives in `/home/tleb/prog/public/elixir/`.

### Goal

Create a unified `./elixir` CLI tool (executable Python script at project root) that
replaces three shell/Python entrypoints (`utils/index`, `utils/query.py`,
`utils/speedtest.py`) and eliminates all environment variable dependencies from the
indexing and querying pipeline.

### Why

Currently the pipeline has four problems:

1. **`script.sh` is a shell dispatch layer** — every git operation, ctags invocation, and
   file parsing call goes through a 270-line shell script spawned as a subprocess from
   Python. This adds process overhead and makes the code harder to debug.
2. **26 shell plugins in `projects/*.sh`** — each project's tag filtering logic lives in
   a separate shell file sourced by `script.sh`. Two different APIs coexist:
   `shiny_versions()` (new, outputs `tag\\tdisplay_name\\tis_rc`) and
   `list_tags_h()` (old, outputs 3-column hierarchical format).
3. **Environment variables for configuration** — `LXR_DATA_DIR`, `LXR_REPO_DIR` are
   required to be set before running anything. `utils/index` passes them via env, query
   reads them from env. This is fragile and makes the pipeline hard to compose.
4. **No unified CLI** — users must know to call `utils/index` for indexing,
   `utils/query.py` for querying, `utils/speedtest.py` for benchmarking, each with
   different argument conventions.

### Approach chosen

Rather than incrementally patching shell scripts, we:

- **Reimplement all `script.sh` commands as Python functions** inside `elixir/lib.py`.
  The public API (`script()`, `scriptLines()`, `scriptVersions()`) stays the same so
  `update.py` and `query.py` call sites barely change — only the internal dispatch
  switches from `subprocess.run(["script.sh", ...])` to calling Python functions.
- **Centralize project config** in a new `elixir/projects.py` module. All 26 shell
  plugins are converted to `ProjectConfig` entries with a `get_versions(repo_dir)`
  callable.
- **Replace env vars with module-level state** — `lib.configure(data_dir, repo_dir,
  project)` sets module globals. `getDataDir()` and `getRepoDir()` read from those
  globals. Multiprocessing workers inherit via `fork()`.
- **Create `./elixir` CLI** with `argparse` subcommands that imports and calls the
  existing Python modules directly (no subprocess to run update.py).

### Key decisions

| Decision | Rationale |
|---|---|
| Keep `script()`/`scriptLines()` API in lib.py | Minimizes changes to update.py (13 call sites) and query.py (8 call sites) |
| `lib.configure()` sets module-level state | Works with `multiprocessing.Pool()` fork-based workers; no need to pass state through pool |
| `Query` passes `repo_dir` via kwarg, not env | Web app can serve multiple projects from one process; no env var collision |
| Translate Perl regexes to Python `re` | Avoids keeping Perl as a runtime dependency for `tokenize_file` and `parse_defs_C` |
| Import `FindCompatibleDTS` directly instead of subprocess | The class is already Python; spawning a subprocess for it is wasteful |
| ctags still called via subprocess | ctags is an external binary; no Python equivalent worth embedding |

---

## Execution rules

- Run steps sequentially, one at a time
- Each step runs in a subagent (use the task tool)
- Commit after each step completes
- A step is only started when the previous one has committed successfully

---

## Step 1: Create `elixir/projects.py` — Boilerplate

### Before

Project configuration is scattered: remotes are hardcoded in `utils/index` (lines 118-146)
as `add_default_remotes` calls, tag filtering logic lives in 26 separate `projects/*.sh`
files sourced by `script.sh`, and `dts_comp_support` is a shell variable set in those
files. There is no Python-accessible project registry.

### Todo

Create `elixir/projects.py` with:

1. **`ProjectConfig` dataclass** with fields:
   - `remotes: list[str]` — git remote URLs
   - `dts_comp_support: bool = False`
   - `get_versions: Callable[[str], list[tuple[str, str, bool]]]` — takes `repo_dir`,
     returns `[(tag, display_name, is_rc)]`

2. **Shared helper functions:**
   - `_default_get_versions(repo_dir)` — runs `git tag --sort=-creatordate`, returns all
     tags as `[(tag, tag, False)]`
   - `_tag_pattern_versions(repo_dir, pattern)` — same but filters by regex pattern

3. **`PROJECTS` dict** — keyed by project name (string), valued by `ProjectConfig`.
   Populate `remotes` from `utils/index` lines 118-146. Set `get_versions =
   _default_get_versions` for all projects initially (will be overridden in steps 2a-c).
   Set `dts_comp_support = True` for: `arm-trusted-firmware`, `barebox`, `linux`,
   `u-boot`, `zephyr`.

Complete list of all 26 projects and their remotes (from `utils/index`):

```
amazon-freertos: ["https://github.com/aws/amazon-freertos.git"]
arm-trusted-firmware: ["https://github.com/ARM-software/arm-trusted-firmware"]
barebox: ["https://git.pengutronix.de/git/barebox"]
busybox: ["https://git.busybox.net/busybox"]
coreboot: ["https://review.coreboot.org/coreboot.git"]
dpdk: ["https://dpdk.org/git/dpdk", "https://dpdk.org/git/dpdk-stable"]
glibc: ["https://sourceware.org/git/glibc.git"]
llvm: ["https://github.com/llvm/llvm-project.git"]
mesa: ["https://gitlab.freedesktop.org/mesa/mesa.git"]
musl: ["https://git.musl-libc.org/git/musl"]
ofono: ["https://git.kernel.org/pub/scm/network/ofono/ofono.git"]
op-tee: ["https://github.com/OP-TEE/optee_os.git"]
qemu: ["https://gitlab.com/qemu-project/qemu.git"]
u-boot: ["https://source.denx.de/u-boot/u-boot.git"]
uclibc-ng: ["https://cgit.uclibc-ng.org/cgi/cgit/uclibc-ng.git"]
zephyr: ["https://github.com/zephyrproject-rtos/zephyr"]
toybox: ["https://github.com/landley/toybox.git"]
grub: ["https://git.savannah.gnu.org/git/grub.git"]
bluez: ["https://git.kernel.org/pub/scm/bluetooth/bluez.git"]
linux: ["https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
        "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
        "https://github.com/bootlin/linux-history.git"]
xen: ["https://xenbits.xen.org/git-http/xen.git"]
freebsd: ["https://git.freebsd.org/src.git"]
opensbi: ["https://github.com/riscv-software-src/opensbi"]
iproute2: ["https://git.kernel.org/pub/scm/network/iproute2/iproute2.git"]
vpp: ["https://gerrit.fd.io/r/vpp"]
igt: ["https://gitlab.freedesktop.org/drm/igt-gpu-tools.git"]
```

### After

A new file `elixir/projects.py` exists with the `ProjectConfig` dataclass,
`_default_get_versions`, `_tag_pattern_versions` helpers, and a `PROJECTS` dict
containing all 26 projects with correct remotes and `dts_comp_support` flags. The module
is importable and `PROJECTS` is usable. No other files are modified yet.

---

## Step 2a: Convert SHINY projects (10 projects)

### Before

`elixir/projects.py` exists with all projects using `_default_get_versions`. 10 projects
have `shiny_versions()` in their `projects/*.sh` files — these already output the
`tag\\tdisplay_name\\tis_rc` TSV format that maps directly to the `(tag, display_name,
is_rc)` tuple format.

### Todo

For each of the 10 projects below, write a custom `get_versions` function and assign it
to the corresponding entry in `PROJECTS`. Each function takes `repo_dir: str`, runs `git
tag --sort=-creatordate` via subprocess, filters/transforms the output, and returns
`list[tuple[str, str, bool]]`.

**Reference: the `shiny_versions()` shell functions output lines of `tag\\tdisplay_name\\tis_rc`.**
Convert each one faithfully. The awk patterns translate directly to Python regex + list
comprehensions.

Projects and their logic:

1. **musl** — Filter to `^v\d+(\.\d+){2}$` exact match. Output `(tag, tag, False)`.
   Simple: use `_tag_pattern_versions(repo_dir, r"^v\d+(\.\d+){2}$")`.

2. **uclibc-ng** — Identical to musl. Use `_tag_pattern_versions`.

3. **barebox** — Filter to `^v\d+(\.\d+){2}$` but skip: `v2.0.0-rc*`,
   `freescale-mx35-3-stack-*`, `v2011.04.0-phytec-pcm049`. Output `(tag, tag, False)`.

4. **glibc** — Filter to `^glibc-\d+(\.\d+){1,2}...` but skip `cvs/*`, `fedora/*`,
   `changelog-ends-here`, and tags with minor version 90 or 9000 (dev branches).
   Display name: strip `glibc-` prefix, prepend `v`. No RCs. So tag `glibc-2.43`
   becomes display `v2.43`.

5. **igt** — Four tag naming conventions:
   - `intel-gpu-tools-X.Y` → display `vX.Y`
   - `igt-gpu-tools-X.Y` → display `vX.Y`
   - `X.Y` (no prefix) → display `vX.Y`
   - `vX.Y` → keep as-is, display `vX.Y`
   No RCs.

6. **llvm** — Two patterns:
   - `llvmorg-X.Y.Z[-rcN]` → display `vX.Y.Z`, is_rc if `-rc` present
   - `llvmorg-X-init` → display `vX.0-init`, always is_rc=True

7. **mesa** — Complex blacklist + two naming conventions. Many individual tags to skip
   (see `projects/mesa.sh` for the full list). Two match patterns:
   - `mesa-X.Y.Z[-rcN][-N.N]` → display `vX.Y.Z[-rcN][-N.N]`
   - `mesa_X_Y_Z[_rcN][_N]` → same with underscores → dots
   Special cases: `mesa-10.1-devel` (rc), `mesa_3_1_beta_3` (rc), `mesa_3_2_beta_1` (rc).

8. **op-tee** — Skip `20160825-for-lmg`. Match `^\d+\.\d+\.\d+(-rc\d+)?$`. Display name:
   prepend `v` to the tag. is_rc if `-rc` present.

9. **u-boot** — Two tag styles:
   - `vX.Y.Z[-rcN]` → display same, is_rc if `-rc`
   - `(U-Boot-|U_BOOT_)X_Y_Z` → display `vX.Y.Z`, no RC
   Skip: `*-dont-use`, `LABEL_*`, `DENX-*`.

10. **vpp** — Match `^v\d+(\.\d+){1,2}(-rc\d+)?$`. Display same as tag. is_rc if `-rc`.

### After

All 10 SHINY projects have custom `get_versions` functions in `elixir/projects.py`
that faithfully reproduce their shell `shiny_versions()` output. The remaining 16
projects still use `_default_get_versions`.

---

## Step 2b: Convert OLD projects (14 projects)

### Before

14 projects have only `list_tags_h()` (and sometimes `version_dir()`/`version_rev()`)
in their shell plugins. These use the old 3-column hierarchical format
(`top middle tag`) which is different from the `shiny_versions` format. They need to be
converted to output `[(tag, display_name, is_rc)]` tuples.

### Todo

Write a custom `get_versions` for each project. The old `list_tags_h()` outputs 3-column
lines like `v3 v3.1 v3.1-rc10` where column 1 is the top-level menu, column 2 is the
submenu, and column 3 is the full tag. The new format needs `(tag, display_name, is_rc)`
where `display_name` is what the user sees (typically the full tag with `v` prefix).

**General approach for `list_tags_h` conversions:** The old `list_tags_h` splits tags
into a 3-level hierarchy. For the new format, we need to determine:
- The actual git tag (column 3 in old format)
- The display name (usually same as the tag, sometimes with minor adjustments)
- Whether it's a release candidate (look for `-rc` in the tag)

**Note on `$tags` variable:** In `script.sh`, `$tags` is populated by `get_tags()` (if
defined) or `git tag | version_dir` (if `version_dir` is defined) or just `git tag`. The
`get_versions` functions must replicate the same tag source + filtering pipeline.

Projects and their logic:

1. **amazon-freertos** — Source: `git tag` (no `get_tags` or `version_dir`).
   Two groups: non-`v`-prefixed tags (`YYYYMM...` format) and `v`-prefixed tags.
   Each group sorted newest-first, split into 3-level hierarchy.
   Convert to: all tags, sorted by creatordate descending, with appropriate display
   names. Non-RC.

2. **arm-trusted-firmware** — Source: `git tag`. Two groups: normal `vX.Y.Z` tags
   (exclude `for-v0.4`) and `for-v0.4` tags (placed under `custom` top-level).
   The `for-v0.4` tags need special display naming.

3. **bluez** — Source: `git tag` filtered to `^[0-9]` (numeric-starting only).
   Sorted `sort -rV`, split into `vMAJOR vMAJOR.MINOR MAJOR.MINOR` hierarchy.
   Display name is the tag itself (e.g. `4.112`). Non-RC.

4. **busybox** — Source: `git tag | version_dir` where `version_dir` swaps `_` and `.`.
   So tags like `1_36_1` become `1.36.1`. Display names: `vMAJOR.MAJOR.MINOR[-suffix]`.
   Non-RC.

5. **coreboot** — Source: `git tag`. Simple `X.Y.Z` tags. Display: `vX vX.Y X.Y.Z`.
   Non-RC.

6. **dpdk** — Source: `git tag`. Two groups:
   - `v3+` tags → normal hierarchy `vMAJOR vMAJOR.MINOR tag`
   - `v1.`/`v2.` tags → under `old` prefix
   Non-RC.

7. **freebsd** — Has only `version_dir()` and `version_rev()` (no `list_tags_h`).
   `version_dir` filters `release/X.Y.Z` tags, strips `release/` prefix, strips
   trailing `.0`. So `release/13.3.0` → `v13.3`. The `get_versions` function must
   replicate this: get all tags matching `^release/\d+\.\d+\.\d+$`, strip prefix,
   strip trailing `.0`, use as both tag and display name. Non-RC.

8. **grub** — Source: `git tag`. Tags may or may not have `grub-` prefix.
   Pattern: `(grub-)?MAJOR.MINOR[suffix]`. Display: strip optional prefix, prepend
   `v` to major. Non-RC.

9. **linux** — Source: `get_tags()` which does complex sorting via
   `version_dir | sed | sort -V | sed`. `version_dir` is a no-op filter on git tags.
   The `get_tags` pipeline handles `pre` and `lia64-` prefixed tags and sorts with
   `sort -V`. Then `list_tags_h` splits into `vMAJOR vMAJOR.MINOR fulltag` hierarchy.
   Tags like `v6.12-rc1` have is_rc=True. Tags like `pre-v2.5` and `lia64-2.6.0` need
   special handling.
   **This is the most complex project.** Study `projects/linux.sh` carefully.
   The `get_tags()` function must be faithfully reproduced — it normalizes tags for
   `sort -V` then strips the normalization markers.

10. **ofono** — Source: `git tag`. Simple `X.Y.Z` tags. Display: `vX vX.Y X.Y.Z`.
    Non-RC.

11. **qemu** — Source: `git tag`. Three groups:
    - `vX.Y` tags → `vX vX.Y tag`
    - `release_*` tags → `old release release_TAG`
    - Synthetic entry: `old initial initial` (not a real tag)
    Non-RC.

12. **toybox** — Source: `git tag`. `X.Y.Z` tags (no `v` prefix on tag itself).
    Display: `MAJOR MAJOR.MINOR tag`. Non-RC.

13. **xen** — Has only `version_dir()` and `version_rev()` (no `list_tags_h`).
    `version_dir` filters `RELEASE-*` tags, strips `RELEASE-` prefix, prepends `v`.
    So `RELEASE-4.19.0` → `v4.19.0`. The `get_versions` function must replicate this.
    Non-RC.

14. **zephyr** — Source: `git tag` filtered to exclude `^zephyr-v` prefixed tags.
    Then standard `vX.Y.Z[-rc]` hierarchy. is_rc if `-rc` present.

### After

All 14 OLD projects have custom `get_versions` functions. Combined with step 2a, 24 of
26 projects now have proper version listing. The remaining 2 (iproute2, opensbi) will be
handled in step 2c.

---

## Step 2c: Configure EMPTY projects (2 projects)

### Before

2 projects (`iproute2`, `opensbi`) have empty shell plugins (0 bytes). They use the
default behavior: `git tag | sort -V` with no filtering.

### Todo

These two already use `_default_get_versions` from the boilerplate step. Verify that
`_default_get_versions` produces reasonable output for them. If adjustments are needed
(e.g., they might benefit from filtering out non-version tags), make them. Otherwise,
no code changes needed — they're already correctly configured.

### After

All 26 projects have correct `get_versions` implementations. `elixir/projects.py` is
complete and can fully replace `projects/*.sh` and the hardcoded remote list in
`utils/index`.

---

## Step 3: Rewrite `elixir/lib.py` internals

### Before

`elixir/lib.py` (239 lines) spawns `script.sh` as a subprocess for every operation:

- `script(*args)` → `subprocess.run(["script.sh", *args])`
- `scriptLines(*args)` → splits script output by newlines
- `scriptVersions()` → calls `scriptLines("versions")` and parses TSV
- `getDataDir()` → reads `os.environ["LXR_DATA_DIR"]`
- `getRepoDir()` → reads `os.environ["LXR_REPO_DIR"]`

`script.sh` sources a project plugin, `cd`s into the repo, and dispatches to shell
functions that call `git`, `ctags`, `perl`, `sed`, `awk`.

### Todo

Rewrite `lib.py` to replace subprocess-to-script.sh with in-process Python functions.

#### 3.1 Module-level state

Add module globals `_data_dir`, `_repo_dir`, `_project` (initially `None`).

```python
def configure(data_dir, repo_dir, project):
    global _data_dir, _repo_dir, _project
    _data_dir = data_dir
    _repo_dir = repo_dir
    _project = project
```

Update `getDataDir()` and `getRepoDir()` to read from globals (fall back to env vars
for backward compat). Add `getProject()` helper.

#### 3.2 Internal dispatch

Keep the public API identical: `script(*args, env=None)`, `scriptLines(*args, env=None)`,
`scriptVersions()`.

Modify `script()`:
- Add optional `repo_dir=None` and `project=None` kwargs (default from module state)
- Instead of `subprocess.run(["script.sh", ...])`, call `_dispatch(cmd, args, repo_dir,
  project)` which routes to Python implementations
- The `env` parameter is kept for backward compat but ignored

Implement `_dispatch(cmd, opts, repo_dir, project)` routing:

| `cmd` | Python function | Implementation notes |
|---|---|---|
| `versions` | `projects.PROJECTS[project].get_versions(repo_dir)` | Format as TSV bytes |
| `list-blobs` | `_list_blobs(opts, repo_dir)` | `git ls-tree -r` + Python string parsing instead of sed |
| `parse-defs` | `_parse_defs(opts, repo_dir)` | ctags via subprocess + Python regex for ENTRY/SYSCALL instead of perl |
| `tokenize-file` | `_tokenize_file(opts, repo_dir)` | `git cat-file blob` + translate Perl regex to Python `re.sub()` |
| `get-type` | `_get_type(opts, repo_dir)` | `git cat-file -t` via subprocess |
| `get-file` | `_get_file(opts, repo_dir)` | `git cat-file blob` via subprocess |
| `get-dir` | `_get_dir(opts, repo_dir)` | `git ls-tree -l` via subprocess + Python parsing |
| `parse-comps` | `_parse_comps(opts, repo_dir)` | Import `FindCompatibleDTS` directly, no subprocess |
| `dts-comp` | returns `projects.PROJECTS[project].dts_comp_support` | Simple dict lookup |

#### 3.3 Implementation details for each command

**`_list_blobs(opts, repo_dir)`:**
- `opts[0]` is `-p` (path) or `-f` (filename) or a version tag
- Run `git ls-tree -r <version>` via subprocess
- Parse output in Python: each line is `<mode> blob <hash>\t<path>`
- Return `hash path` pairs (for `-p`) or `hash filename` pairs (for `-f`)

**`_tokenize_file(opts, repo_dir)`:**
- `opts[0]` is `-b` (blob hash) or a version tag
- If `-b`, ref = `opts[1]`; else ref = `tag:path`
- Get blob content via `git cat-file blob <ref>`
- For D (devicetree) family: use regex with `[\w-]+` instead of `\w+`
- Translate the Perl regex to Python:
  ```
  s%((/\*.*?\*/|//.*?\001|[^'"'"']"(\\.|.)*?"|# *include *<.*?>|\W)+)(\w+)?%\1\n\4\n%g
  ```
  This becomes a Python `re.sub()` with the same pattern adapted for Python syntax.

**`_parse_defs(opts, repo_dir)`:**
- `opts[2]` is the file family: C, K, or D
- Write blob to temp file, run `ctags -x` via subprocess
- For C: additionally use Python `re` for ENTRY() and SYSCALL_DEFINE patterns instead of
  perl one-liners:
  - `^\s*ENTRY\((\w+)\)` → `\1 function LINE`
  - `^SYSCALL_DEFINE\d\(\s*(\w+)\W` → `sys_\1 function LINE`

**`_parse_comps(opts, repo_dir)`:**
- Get blob content via `git cat-file blob <hash>`
- Import `FindCompatibleDTS` from `find_compatible_dts.py` (move it to `elixir/` or
  adjust import path)
- Call `FindCompatibleDTS().run(lines, family)` directly

**`_get_type(opts, repo_dir)`:**
- `git cat-file -t "<tag>:<path>"` via subprocess

**`_get_file(opts, repo_dir)`:**
- `git cat-file blob "<tag>:<path>"` via subprocess

**`_get_dir(opts, repo_dir)`:**
- `git ls-tree -l "<tag>:<path>"` via subprocess
- Parse in Python (replace awk/sort pipeline)

#### 3.4 Keep unchanged

- `blacklist`, `isIdent()`, `autoBytes()`, `getFileFamily()`, `decode()`, `unescape()`,
  `CURRENT_DIR` — these are pure Python utilities used elsewhere.
- The `run_cmd()` helper.

### After

`elixir/lib.py` no longer spawns `script.sh`. All operations are Python functions that
call git/ctags via subprocess directly. The public API (`script()`, `scriptLines()`,
`scriptVersions()`) is unchanged, so `update.py` and `query.py` continue to work without
modification. `getDataDir()` and `getRepoDir()` read from module state (set by
`configure()`) with env var fallback.

---

## Step 4: Modify `elixir/update.py`

### Before

`elixir/update.py` (578 lines):
- Line 68: `os.environ["LXR_DATA_DIR"]` to construct database path
- Calls `lib.scriptVersions()`, `lib.scriptLines()` extensively in worker functions
- `main()` has no parameters — everything comes from env vars
- `if __name__ == "__main__": main()` runs unconditionally

### Todo

1. **Line 68**: Replace `os.environ["LXR_DATA_DIR"]` with `lib.getDataDir()`.

2. **`main()` function**: Add optional parameters:
   ```python
   def main(data_dir=None, repo_dir=None, project=None):
       if data_dir and repo_dir and project:
           lib.configure(data_dir, repo_dir, project)
       ...
   ```

3. **`__main__` block**: Keep backward compat:
   ```python
   if __name__ == "__main__":
       main()
   ```
   This works because `getDataDir()`/`getRepoDir()` still fall back to env vars.

No changes to worker functions (`stage02_worker`, `stage03_worker`, etc.) — they call
`lib.scriptLines()` and `lib.scriptVersions()` which are unchanged in their API.

### After

`update.py` can be called either via `lib.configure()` + `update.main()` (new way) or
via env vars + `python3 -m elixir.update` (old way, still works). The indexing pipeline
is unchanged.

---

## Step 5: Modify `elixir/query.py`

### Before

`elixir/query.py` (268 lines):
- `Query.__init__` (line 64): takes `data_dir, repo_dir`
- Line 67: `self.dts_comp_support = int(self.script("dts-comp"))` — calls script.sh
- Lines 74-78: `Query.script()` and `Query.scriptLines()` call `script()`/`scriptLines()`
  with `env=self.getEnv()` to set per-instance env vars
- Lines 80-85: `getEnv()` returns dict with `LXR_REPO_DIR` and `LXR_DATA_DIR`

### Todo

1. **Import projects**: Add `from . import projects` at top.

2. **`Query.__init__`**: Replace line 67:
   ```python
   self.dts_comp_support = projects.PROJECTS[project].dts_comp_support
   ```
   This requires adding a `project` parameter to `__init__`. Update `get_query()`
   factory to pass it through.

3. **`Query.script()` / `Query.scriptLines()`**: Replace `env=self.getEnv()` with
   `repo_dir=self.repo_dir`:
   ```python
   def script(self, *args):
       return script(*args, repo_dir=self.repo_dir)

   def scriptLines(self, *args):
       return scriptLines(*args, repo_dir=self.repo_dir)
   ```

4. **Remove `getEnv()`** entirely.

5. **Update `get_query()`**: Pass project name to `Query()`:
   ```python
   def get_query(basedir, project):
       ...
       return Query(datadir, repodir, project)
   ```

6. **Check `web.py` call site**: `get_query(ctx.config.project_dir, project)` in
   `web.py` passes the project name as second arg, which maps to the `project`
   parameter in `get_query()` — no change needed.

### After

`query.py` no longer uses environment variables. `Query` instances are fully
self-contained with explicit `repo_dir`, `data_dir`, and `project` parameters. The web
app (`web.py`) continues to work unchanged because `get_query()` signature is the same.

---

## Step 6: Create `./elixir` CLI

### Before

There are three separate entrypoints:
- `utils/index <data_path> <project>` — init repo, fetch, index
- `utils/query.py <subcommand>` — query indexed data (requires env vars)
- `utils/speedtest.py` — benchmark queries (requires env vars)

Each has different argument conventions and all require env vars to be set.

### Todo

Create an executable Python script at the project root named `elixir` (no `.py`
extension, `#!/usr/bin/env python3` shebang, `chmod +x`).

Subcommands:

```
elixir fetch  <data_path> [project...] [--gc {auto,aggressive}]
elixir index  <data_path> [project...]
elixir update <data_path> [project...] [--gc {auto,aggressive}]
elixir stats    <data_path> <project>
elixir versions <data_path> <project>
elixir ident    <data_path> <project> <version> <ident>
elixir file     <data_path> <project> <version> <path>
elixir speedtest <data_path> <project> [-v]
```

**Common behavior:**
- `<data_path>` is the root data directory (e.g., `/srv/elixir-data/`)
- `<project>` must exist in `projects.PROJECTS`, else error
- No project args (for fetch/index/update) → operate on all project directories found
  under `<data_path>` that exist in `PROJECTS`

**`fetch` subcommand:**
For each project:
1. `project_init(data_path, project)`: create `<data_path>/<project>/data` and `repo`
   dirs, `git init --bare` if not already done
2. `project_add_remotes(data_path, project)`: add default remotes from
   `PROJECTS[project].remotes` (skip if already present)
3. `git fetch --all --tags -j4` in the repo dir
4. GC: `--gc aggressive` runs `git gc --aggressive`; `--gc auto` (default) runs
   `git gc --auto`. Also run aggressive GC if `gc.log` exists in the repo.

**`index` subcommand:**
For each project:
1. Call `lib.configure(data_dir, repo_dir, project)`
2. Call `update.main()`

**`update` subcommand:**
For each project:
1. Run `fetch` logic
2. If `<data_path>/<project>/data/` is empty (no indexed data yet): fetch + index +
   fetch + index (double pass, matching `utils/index` behavior)
3. Else: fetch + index

**`stats` subcommand:**
1. `lib.configure(data_dir, repo_dir, project)`
2. Create `Query(data_dir, repo_dir, project)`
3. Print version count, blob count, defs count, refs count (from `utils/query.py:cmd_stats`)

**`versions` subcommand:**
1. Same setup as stats
2. Print all versions in hierarchical format (from `utils/query.py:cmd_versions`)

**`ident` subcommand:**
1. Same setup
2. Print symbol definitions, references, doc comments for the given identifier
   (from `utils/query.py:cmd_ident`)

**`file` subcommand:**
1. Same setup
2. Print tokenized file contents (from `utils/query.py:cmd_file`)

**`speedtest` subcommand:**
1. `lib.configure(data_dir, repo_dir, project)`
2. Import and run the speedtest logic from `utils/speedtest.py`, adapted to take
   `data_dir`/`repo_dir`/`project` as arguments instead of reading env vars

### After

A single `./elixir` executable replaces `utils/index`, `utils/query.py`, and
`utils/speedtest.py`. No environment variables need to be set. All subcommands work by
importing the elixir Python package and calling functions directly.

---

## Step 7: Delete old files

### Before

All new code is in place and working. The old files are now dead code.

### Todo

Delete the following files:

- `script.sh` — replaced by Python functions in `lib.py`
- `utils/index` — replaced by `./elixir fetch/index/update`
- `utils/query.py` — replaced by `./elixir stats/versions/ident/file`
- `utils/speedtest.py` — replaced by `./elixir speedtest`
- `projects/*.sh` (all 26 files) — replaced by `elixir/projects.py`
- `find_compatible_dts.py` — its `FindCompatibleDTS` class is now imported directly
  (either moved into `elixir/` package or the import path was updated in `lib.py`)

Verify:
- `python3 -c "import elixir.lib"` succeeds
- `python3 -c "import elixir.projects; print(len(elixir.projects.PROJECTS))"` prints 26
- `./elixir --help` shows all subcommands
- `python3 -m elixir.update --help` still works (backward compat via env vars)

### After

The old shell-based pipeline is fully removed. The codebase is pure Python with a single
CLI entrypoint. The web app continues to work unchanged.

---

## Conclusion

After all steps are complete:

1. **Single entrypoint** — `./elixir` with subcommands replaces three separate tools
2. **No env vars** — all configuration via CLI arguments
3. **No shell scripts** — everything is Python; `script.sh` and all 26 `projects/*.sh`
   files are deleted
4. **Cleaner architecture** — project config is centralized, lib.py dispatches to Python
   functions instead of spawning shell scripts
5. **Backward compat** — `python3 -m elixir.update` still works with env vars (for
   existing deployments), `web.py` is untouched

### What was done & issues encountered

*(To be filled in by the implementing agent after each step.)*
