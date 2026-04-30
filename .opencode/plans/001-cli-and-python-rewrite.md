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
   _default_get_versions` for all projects initially (will be overridden in steps 3a-c).
   Set `dts_comp_support = True` for: `arm-trusted-firmware`, `barebox`, `linux`,
   `u-boot`, `zephyr`.

**Important: `$tags` variable is never set in `script.sh`.** The `list_tags_h()` functions
reference `$tags` but `script.sh` never populates it — it only calls `shiny_versions()`.
This means OLD projects currently **cannot be indexed** via `update.py`. The conversions
in steps 3a-3c are creating **new working code**, not converting existing working code.
The `list_tags_h` logic must be reverse-engineered, including reconstructing what the old
`$tags` pipeline would have produced (`git tag | version_dir` or `get_tags`).

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

## Step 2: Add version validation script

### Before

`elixir/projects.py` exists with all 26 projects using `_default_get_versions`.
All project repos are available at `data/<project>/repo`.

### Todo

Create `elixir/check_versions.py` runnable as `python3 -m elixir.check_versions`.
For each project (or a filtered list via CLI args), call `get_versions(repo_dir)`
and validate:

- Element 0 (tag) exists as a real git tag in `data/<project>/repo`
- Element 1 (version) matches `^v\d+\.\d+`
- Element 2 (is_rc) is a bool

Prints any violations. Exits non-zero if any are found.

### After

`python3 -m elixir.check_versions [project ...]` validates all `get_versions`
output. **Steps 3a, 3b, 3c must pass `check-versions` without errors before being
considered complete.** Step 7 (Create CLI) will absorb this as a `./elixir check-versions`
subcommand.

---

## Version/Tag Semantics (applies to Steps 3a–3c)

`get_versions(repo_dir) -> list[tuple[str, str, bool]]`

- **Element 0: "tag"** — the exact tag string in the git repository.
  Used by `update.py` as a git ref (e.g. `git ls-tree -r <tag>`).
  MUST be a real tag that exists in the repo.

- **Element 1: "version"** — a pretty display string starting with `v\d+\.\d+`.
  Anything can follow that prefix: `-rc1`, `.3`, `-init`, `-beta-3`, `-devel`, etc.
  The web frontend parses versions to build a 3-level hierarchy
  (`v5` → `v5.6` → `v5.6.2`).

- **Element 2: `is_rc` (bool)** — `True` if this is a pre-release / release candidate.

For most projects, tag and version are the same string. When they differ, the tag is
a git-specific naming convention (e.g. `glibc-2.43`, `release/13.3.0`, `1_36_1`) and
the version is the normalized `v\d+\.\d+...` form.

**Source of truth:** read the corresponding `projects/<name>.sh` file. If it has
`shiny_versions()`, convert it directly — it already outputs `tag\tversion\tis_rc`.
If it has `list_tags_h()` or `version_dir()`/`version_rev()`, reverse-engineer the
full pipeline including how `$tags` would have been populated (`get_tags()` if defined,
else `git tag | version_dir` if `version_dir` is defined, else `git tag`).

**The old `$tags` variable is never set in `script.sh`** — it only calls
`shiny_versions()`. This means OLD projects currently cannot be indexed. The conversions
are creating new working code.

**Verification:** All 26 project repos are available locally at `data/<project>/repo`.
You can inspect real tags with:
```
git -C data/<project>/repo tag --sort=-creatordate
```
Use this to validate that your `get_versions` function's tag values (element 0) all
exist as real git tags, and that the version strings (element 1) follow the
`v\d+\.\d+...` convention.

---

## Step 3a: Convert SHINY projects (10 projects)

### Before

`elixir/projects.py` exists with all projects using `_default_get_versions`. 10 projects
have `shiny_versions()` in their `projects/*.sh` files — these already output the
`tag\tversion\tis_rc` TSV format that maps directly to the tuple format.

### Todo

For each project below, read its `projects/<name>.sh`, convert `shiny_versions()` to a
Python `get_versions(repo_dir)` function, and assign it in `PROJECTS`.

Projects: **musl**, **uclibc-ng**, **barebox**, **glibc**, **igt**, **llvm**, **mesa**,
**op-tee**, **u-boot**, **vpp**.

### After

All 10 SHINY projects have custom `get_versions` functions. The remaining 16 projects
still use `_default_get_versions`.

**Gate:** `python3 -m elixir.check_versions` must pass for all converted projects.

---

## Step 3b: Convert OLD projects (14 projects)

### Before

14 projects have only `list_tags_h()` and/or `version_dir()`/`version_rev()` in their
shell plugins — no `shiny_versions()`. The `list_tags_h` outputs a 3-column hierarchy
that was consumed by the old web frontend. The new `get_versions` returns flat
`(tag, version, is_rc)` tuples; the web app builds the hierarchy from versions.

### Todo

For each project below, read its `projects/<name>.sh`, reverse-engineer the full tag
pipeline, and write a `get_versions(repo_dir)` function. See "Version/Tag Semantics"
above for tuple format.

Projects: **amazon-freertos**, **arm-trusted-firmware**, **bluez**, **busybox**,
**coreboot**, **dpdk**, **freebsd**, **grub**, **linux**, **ofono**, **qemu**,
**toybox**, **xen**, **zephyr**.

### After

All 14 OLD projects have custom `get_versions` functions. Combined with step 2a, 24 of
26 projects now have proper version listing.

**Gate:** `python3 -m elixir.check_versions` must pass for all converted projects.

---

## Step 3c: Configure EMPTY projects (2 projects)

### Before

2 projects (`iproute2`, `opensbi`) have empty shell plugins (0 bytes). They use the
default behavior with no filtering.

### Todo

These two already use `_default_get_versions` from the boilerplate step. Verify that
`_default_get_versions` produces reasonable output for them. If adjustments are needed
(e.g., they might benefit from filtering out non-version tags), make them. Otherwise,
no code changes needed — they're already correctly configured.

### After

All 26 projects have correct `get_versions` implementations. `elixir/projects.py` is
complete and can fully replace `projects/*.sh` and the hardcoded remote list in
`utils/index`.

**Gate:** `python3 -m elixir.check_versions` must pass for all 26 projects.

---

## Step 4: Rewrite `elixir/lib.py` internals

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

**Also update `scriptLines()`** to forward the new kwargs:
```python
def scriptLines(*args, repo_dir=None, project=None, env=None):
    p = script(*args, repo_dir=repo_dir, project=project, env=env)
    ...
```

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

**Dropped commands** (dead code, not called by any Python code):
- `get-blob` — no call site found
- `untokenize` — no call site found
- `parse-docs` — no call site found (only used `find-file-doc-comments.pl`)

**`_list_blobs` details:** Three modes:
- `-p`: return `hash path` (blob hash + full path)
- `-f`: return `hash filename` (blob hash + basename)
- default (first arg is a version tag): return `hash` only (not used by `update.py` but
  include for completeness)

#### 3.3 Helper: denormalize

`script.sh:206-209` defines `denormalize()` which strips the leading `/` from path
arguments. It is used by `get_type`, `get_file`, `get_dir`, and `tokenize_file`. Without
this, git commands like `git cat-file blob "v5.6:/Makefile"` fail — the path must be
`v5.6:Makefile`.

Implement as a helper:
```python
def _denormalize(path):
    return path[1:]  # strip leading /
```

Use it in `_get_type`, `_get_file`, `_get_dir`, and `_tokenize_file` when constructing
git refs like `f"{tag}:{_denormalize(path)}"`.

#### 3.4 Implementation details for each command

**`_list_blobs(opts, repo_dir)`:**
- `opts[0]` is `-p` (path) or `-f` (filename) or a version tag
- Run `git ls-tree -r <version>` via subprocess
- Parse output in Python: each line is `<mode> blob <hash>\t<path>`
- Return `hash path` pairs (for `-p`) or `hash filename` pairs (for `-f`)

**`_tokenize_file(opts, repo_dir)`:**
- `opts[0]` is `-b` (blob hash) or a version tag
- If `-b`, ref = `opts[1]`; else ref = `f"{tag}:{_denormalize(path)}"`
- Get blob content via `git cat-file blob <ref>`
- For D (devicetree) family: both the non-token alternation group and the token capture
  group change: `[^\w-]` (doesn't match `-`) instead of `\W`, and `[\w-]+` instead of
  `\w+`. This prevents splitting on hyphens in devicetree property names.
- Translate the Perl regex to Python:
  ```
  Non-D: s%((/\*.*?\*/|//.*?\001|[^'"'"']"(\\.|.)*?"|# *include *<.*?>|\W)+)(\w+)?%\1\n\4\n%g
  D:     s%((/\*.*?\*/|//.*?\001|[^'"'"']"(\\.|.)*?"|# *include *<.*?>|[^\w-])+)([\w-]+)?%\1\n\4\n%g
  ```
  **This is the highest-risk translation in the plan.** The regex uses Perl-specific
  features. Test against real source files after implementation.

**`_parse_defs(opts, repo_dir)`:**
- `opts[0]` = blob hash, `opts[1]` = filename, `opts[2]` = file family (C, K, or D)
- Write blob to temp file (preserve the original filename including extension — ctags
  uses it for language detection). Create as `tmpdir/filename`.
- **Family C:** Run `ctags -x --kinds-c=+p+x --extras='-{anonymous}' <file>`. Filter
  output: exclude lines starting with `operator ` or `CONFIG_` (the `grep -avE` at
  `script.sh:141`). Parse remaining lines: `name type lineno`. Then additionally scan
  for ENTRY/SYSCALL macros using Python `re` instead of perl:
  - `^\s*ENTRY\((\w+)\)` → `\1 function LINE`
  - `^SYSCALL_DEFINE\d\(\s*(\w+)\W` → `sys_\1 function LINE`
- **Family K:** Run `ctags -x --language-force=kconfig --kinds-kconfig=c
  --extras-kconfig=-{configPrefixed} <file>`. Parse lines, prepend `CONFIG_` to each
  name.
- **Family D:** Run `ctags -x --language-force=dts <file>`. Parse lines as-is.

**`_parse_comps(opts, repo_dir)`:**
- Get blob content via `git cat-file blob <hash>`
- Import `FindCompatibleDTS` directly from `find_compatible_dts.py` — **move this file
  into `elixir/` package** and change its import from `from elixir.lib import decode` to
  `from .lib import decode`. Then `from .find_compatible_dts import FindCompatibleDTS`.
- Call `FindCompatibleDTS().run(lines, family)` directly

**`_get_type(opts, repo_dir)`:**
- `git cat-file -t "<tag>:<denormalized_path>"` via subprocess (strip leading `/`)

**`_get_file(opts, repo_dir)`:**
- `git cat-file blob "<tag>:<denormalized_path>"` via subprocess

**`_get_dir(opts, repo_dir)`:**
- `git ls-tree -l "<tag>:<denormalized_path>"` via subprocess
- Parse in Python (replace awk/sort pipeline from `script.sh:54-59`)
- awk `{print $2" "$5" "$4" "$1}` maps to: `type size hash name`
- `grep -v ' \.'` removes dot-prefixed entries (hidden files)
- `sort -t ' ' -k 1,1r -k 2,2` sorts: trees first (reversed), then by name

#### 3.5 Keep unchanged

- `blacklist`, `isIdent()`, `autoBytes()`, `getFileFamily()`, `decode()`, `unescape()`,
  `CURRENT_DIR` — these are pure Python utilities used elsewhere.
- The `run_cmd()` helper.

**Note:** Steps 1-2c create `elixir/projects.py` which isn't imported by anything yet.
These steps produce no behavioral change and cannot be tested until Step 4 is complete.
The commit after each is a checkpoint, not a working state.

### After

`elixir/lib.py` no longer spawns `script.sh`. All operations are Python functions that
call git/ctags via subprocess directly. The public API (`script()`, `scriptLines()`,
`scriptVersions()`) is unchanged, so `update.py` and `query.py` continue to work without
modification. `getDataDir()` and `getRepoDir()` read from module state (set by
`configure()`) with env var fallback.

---

## Step 5: Modify `elixir/update.py`

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

## Step 6: Modify `elixir/query.py`

### Before

`elixir/query.py` (268 lines):
- `Query.__init__` (line 64): takes `data_dir, repo_dir`
- Line 67: `self.dts_comp_support = int(self.script("dts-comp"))` — calls script.sh
- Lines 74-78: `Query.script()` and `Query.scriptLines()` call `script()`/`scriptLines()`
  with `env=self.getEnv()` to set per-instance env vars
- Lines 80-85: `getEnv()` returns dict with `LXR_REPO_DIR` and `LXR_DATA_DIR`

### Todo

1. **Import projects**: Add `from . import projects` at top.

2. **`Query.__init__`**: Add `project` parameter (optional, defaults to `None` for backward
   compat with `utils/query.py` and `utils/speedtest.py` which will be deleted in Step 8).
   Store it and use it:
   ```python
   def __init__(self, data_dir, repo_dir, project=None):
       self.repo_dir = repo_dir
       self.data_dir = data_dir
       self.project = project
       if project:
           self.dts_comp_support = projects.PROJECTS[project].dts_comp_support
       else:
           self.dts_comp_support = 0
   ```
   When `project` is `None` (legacy callers), `dts_comp_support` defaults to `0` (disabled).
   The `project` is also needed by `_dispatch` for the `versions` command, which calls
   `projects.PROJECTS[project].get_versions(repo_dir)`.

3. **`Query.script()` / `Query.scriptLines()`**: Replace `env=self.getEnv()` with
   `repo_dir` and `project` kwargs:
   ```python
   def script(self, *args):
       return script(*args, repo_dir=self.repo_dir, project=self.project)

   def scriptLines(self, *args):
       return scriptLines(*args, repo_dir=self.repo_dir, project=self.project)
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

## Step 7: Create `./elixir` CLI

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
elixir check-versions <data_path> [project...]
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
   The old `ELIXIR_GC` env var is intentionally **not** supported — use `--gc aggressive`.

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

**`check-versions` subcommand:**
1. Import and run the validation logic from `elixir/check_versions.py` (created in
   Step 2), wrapping it as a CLI subcommand.

### After

A single `./elixir` executable replaces `utils/index`, `utils/query.py`, and
`utils/speedtest.py`. No environment variables need to be set. All subcommands work by
importing the elixir Python package and calling functions directly.

---

## Step 8: Delete old files

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

### Testing strategy

After Step 4 (the riskiest step), verify against a real project:
```bash
# Set up test data (use a small project like musl)
export LXR_DATA_DIR=/path/to/musl/data LXR_REPO_DIR=/path/to/musl/repo

# Test version listing
python3 -c "from elixir import lib; lib.configure('$LXR_DATA_DIR', '$LXR_REPO_DIR', 'musl'); print(lib.scriptVersions())"

# Test tokenize_file (the highest-risk regex translation)
python3 -c "from elixir import lib; lib.configure(...); print(len(lib.scriptLines('tokenize-file', '-b', '<some_hash>', 'C')))"
```

After Step 7, verify the CLI:
```bash
./elixir versions /path/to/data musl
./elixir stats /path/to/data musl
./elixir ident /path/to/data musl latest memcpy
```

### What was done & issues encountered

*(To be filled in by the implementing agent after each step.)*
