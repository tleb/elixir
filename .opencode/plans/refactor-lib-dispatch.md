# Refactor lib.py: Replace string-dispatch with typed functions

## Preamble

Elixir is a source code cross-referencer that indexes code repositories (Linux
kernel, VPP, etc.) and provides a web UI to browse identifiers, definitions,
and references.

`elixir/lib.py` currently has a string-based dispatch system: callers invoke
`script("parse-defs", hash, name, family)` or `scriptLines("list-blobs", "-f",
tag)`, which routes through `_dispatch()` to internal `_`-prefixed functions.
These internal functions serialize their output as `bytes` (newline-separated),
which callers then manually parse by splitting on `b" "` or `b"\n"`.

This design caused a real bug: when `parse_defs` found no definitions in a file,
it returned `b"\n"`. `scriptLines()` split this into `[b""]` — a list
containing a single empty bytestring. The caller then did
`defname, deftype, defline = line.split(b" ")` on `b""`, getting only 1 value
instead of 3, causing a `ValueError` crash during indexing.

The fix is not to patch the serialization edge case, but to eliminate it
entirely: replace all string-dispatch with properly typed functions that return
structured Python types (lists of tuples). This makes the code type-checkable,
eliminates the entire class of serialization bugs, and removes the indirection
through `_dispatch()`.

Key decisions:
- **Separate `list_blobs` functions** instead of one function with a mode flag,
  because each mode (`-f`, `-p`) has a different caller pattern.
- **`tokenize_file` returns `list[tuple[bool, str]]`** using `str` (not `bytes`)
  because the internal regex operates on decoded `str` already. This avoids
  re-encoding overhead and eliminates `.decode()` calls in every caller.
- **`tokenize_file` accepts separate `version`/`path` params** (when not using
  blob mode), consistent with `get_file`/`get_dir`/`get_type`. Callers should
  not need to know about `_denormalize`.
- **`parse_comps` returns `list[tuple[str, int]]`** using `str` (not `bytes`)
  because `FindCompatibleDTS.run()` already returns `list[str]`.
- **`list_blobs` functions return `str` hashes and paths** since the DB stores
  `VARCHAR` and `bytes` would force every caller to `.decode()`.
- **`functools.partial` for workers** — multiprocessing workers need `repo_dir`;
  `functools.partial` pre-binds it before passing the worker to the pool.
  `functools.partial` is picklable in Python 3, so `multiprocessing.Pool` can
  handle it.
- **No global state in `update.py`** — `repo_dir`, `data_dir`, `project` are
  passed explicitly. `lib.configure()` and the globals it sets are removed.
- **No classes** — plain data structures and functions, per project convention.
- **`dts_comp` is dead code** — no caller in the codebase uses it. It is removed
  rather than migrated.

## Execution rules

- Run steps sequentially, one at a time
- Each step runs in a subagent (use the task tool)
- Commit after each step completes
- A step is only started when the previous one has committed successfully

## Step 1: Add new typed public functions to `lib.py`

### Before

`lib.py` contains internal `_`-prefixed functions (`_versions`, `_list_blobs`,
`_tokenize_file`, `_parse_defs`, `_parse_comps`, `_get_type`, `_get_file`,
`_get_dir`, `_dts_comp`) that are only reachable through the `_dispatch()`
string-router. All return raw `bytes` with newline-separated records.

### Todo

Add new public functions alongside the existing internal ones. Do NOT remove the
old functions yet (that happens in step 5). Each new function wraps the logic of
its corresponding internal function but returns structured Python types instead
of serialized bytes.

Functions to add (signatures):

```python
def versions(repo_dir: str, project: str) -> list[tuple[str, str, bool]]:
    """[(tag, version_name, is_rc)]"""

def list_blobs_by_file(version: str, repo_dir: str) -> list[tuple[str, str]]:
    """[(blob_hash, filename)]"""

def list_blobs_by_path(version: str, repo_dir: str) -> list[tuple[str, str]]:
    """[(blob_hash, path)]"""

def tokenize_file(
    version: str = None, path: str = None, family: str = "C",
    *, blob_hash: str = None, repo_dir: str
) -> list[tuple[bool, str]]:
    """[(is_ident, content)] alternating non-ident/ident.
    Either (version, path) for a version:path git ref, or blob_hash for
    a raw blob hash."""

def parse_defs(
    blob_hash: str, filename: str, family: str, repo_dir: str
) -> list[tuple[str, str, int]]:
    """[(defname, deftype, defline)]"""

def parse_comps(
    blob_hash: str, family: str, repo_dir: str
) -> list[tuple[str, int]]:
    """[(ident, lineno)]"""

def get_type(version: str, path: str, repo_dir: str) -> str:
    """Decoded type string: "blob" or "tree" """

def get_file(version: str, path: str, repo_dir: str) -> bytes:
    """Raw file content"""

def get_dir(version: str, path: str, repo_dir: str) -> bytes:
    """Raw directory listing"""
```

Implementation notes for each:

- **`versions`**: Reuse `projects.PROJECTS[project].get_versions(repo_dir)` directly
  (same as `_versions` body). Return the list as-is — it already returns
  `list[tuple[str, str, bool]]`.

- **`list_blobs_by_file`**: Same as `_list_blobs` with `mode="file"`. Parse the
  git `ls-tree` output internally. Decode blob hashes and filenames to `str`.
  Return `[(blob_hash_str, filename_str)]`.

- **`list_blobs_by_path`**: Same as `_list_blobs` with `mode="path"`. Return
  `[(blob_hash_str, path_str)]`.

- **`tokenize_file`**: Same logic as `_tokenize_file`. When `blob_hash` is
  given, use it directly as the git ref. Otherwise construct the ref as
  `f"{version}:{path.lstrip('/')}"` (replaces the old `_denormalize(path)`).
  Apply the regex on the decoded `str` content. The regex substitution
  (`pat.sub(r"\1\n\4\n", decoded)`) produces alternating non-ident/ident
  entries separated by `\n`. Split on `\n`, then pair them up.

  **Critical: do NOT skip empty entries.** The regex produces alternating
  pairs: `[non_ident, ident, non_ident, ident, ...]`. If an ident is empty
  (e.g. at end of file), skipping it would shift subsequent entries and break
  the alternating pattern. Instead, include all entries — callers already
  handle empty idents (they skip them via `is_ident` check or the `defnames`
  set lookup).

  The result is `[(False, non_ident_str), (True, ident_str), ...]`. Note:
  `str` not `bytes`. The content uses `\x01` (as `str`, i.e. `"\x01"`) in place
  of newlines for line counting, same as the current `\1` replacement done
  before the regex.

- **`parse_defs`**: Same logic as `_parse_defs` (handles families C, K, D; runs
  ctags; applies ENTRY/SYSCALL regexes for C family). Instead of building a
  `lines: list[str]` and joining with `"\n"`, build and return
  `[(defname_str, deftype_str, defline_int)]` directly. When no definitions are
  found, return `[]` (this fixes the bug). The `isIdent` check and `DEFTYPES`
  check are NOT done here — they remain the caller's responsibility (verified:
  the current `_parse_defs` does not filter by `isIdent` either).

- **`parse_comps`**: Same logic as `_parse_comps`. The `FindCompatibleDTS.run()`
  method returns `list[str]` in the format `"ident lineno"`. Parse each into
  `(ident_str, lineno_int)` and return `list[tuple[str, int]]`. No
  `.decode()` needed since the data is already `str`.

- **`get_type`**: Call `_git(repo_dir, "cat-file", "-t", ref)`, decode and strip
  the output. Return the string. `ref = f"{version}:{path.lstrip('/')}"`.

- **`get_file`**: Call `_git(repo_dir, "cat-file", "blob", ref)`, return
  `out.stdout` as bytes (same as `_get_file`).

- **`get_dir`**: Call `_git(repo_dir, "ls-tree", "-l", ref)`, return `out.stdout`
  as bytes (same as `_get_dir`).

Also update `blacklist` from `bytes` tuples to `str` tuples and update
`isIdent()` to accept `str`:

```python
blacklist = ("NULL", "__", "adapter", ...)

def isIdent(s: str) -> bool:
    return len(s) >= 2 and s not in blacklist and not s.startswith("~")
```

`isIdent` has only one caller (`update.py:271`) which will pass `str` after
migration, so this is safe.

**Do NOT add a `dts_comp` function.** It is dead code — no caller in the
entire codebase invokes `script("dts-comp", ...)` or `scriptLines("dts-comp", ...)`

### After

`lib.py` has both old (`_dispatch`/`script`/`scriptLines`) and new (typed
public functions) APIs coexisting. Nothing calls the new functions yet. All
existing code still works via the old path.

---

## Step 2: Migrate `update.py` to new `lib` functions

### Before

`update.py` calls `lib.scriptLines(...)` in 6 places and
`lib.scriptVersions()` in 1 place. It uses `lib.getDataDir()` for the database
path. It calls `lib.configure()` to set global state. Workers receive no
`repo_dir` — they rely on `lib.script()` falling back to global `_repo_dir`.

### Todo

**Add `import functools` to `update.py`.**

**`main(data_dir, repo_dir, project)`** — make all three params required (no
defaults, no `if` guard). Remove `lib.configure()` call. Pass `data_dir`,
`repo_dir`, and `project` through to every stage function.

**`stage01_ddb_init(data_dir)`** — accept `data_dir` param instead of calling
`lib.getDataDir()`.

**`stage02_fill_blobs_table(ddb, repo_dir, project)`** — add `repo_dir` and
`project` params. Replace `lib.scriptVersions()` with
`lib.versions(repo_dir=repo_dir, project=project)`. Create partial:
`worker_fn = functools.partial(stage02_worker, repo_dir=repo_dir)`, pass to
`pool_and_write_output_to_db`.

**`stage02_worker(version, *, repo_dir)`** — add `repo_dir` keyword arg.
Replace `lib.scriptLines("list-blobs", "-f", versiontag)` with
`lib.list_blobs_by_file(versiontag, repo_dir=repo_dir)`. The result is now
`[(blob_hash_str, filename_str)]` tuples. Full replacement:

```python
def stage02_worker(version, *, repo_dir):
    versionid, versionname, versiontag = version
    blobs = lib.list_blobs_by_file(versiontag, repo_dir=repo_dir)
    blobfilenames = [filename for _, filename in blobs]
    blobfamilies = (lib.getFileFamily(filename) for filename in blobfilenames)
    return pd.DataFrame(
        {
            "blobhash": (blobhash for blobhash, _ in blobs),
            "blobfilename": blobfilenames,
            "blobfamily": pd.Series(blobfamilies, dtype=BLOBFAMILY_DTYPE),
        }
    )
```

**`stage03_fill_version_objects_table(ddb, versions, repo_dir)`** — add
`repo_dir` param. Create partial for `stage03_worker`. Note: the existing
module-level global `stage03_ddb` is still needed for concurrent DB access from
workers — keep it unchanged.

**`stage03_worker(version, *, repo_dir)`** — add `repo_dir`. Replace
`lib.scriptLines("list-blobs", "-p", versiontag)` with
`lib.list_blobs_by_path(versiontag, repo_dir=repo_dir)`. Result is
`[(blob_hash_str, path_str)]` tuples. Full replacement:

```python
def stage03_worker(version, *, repo_dir):
    global stage03_ddb

    versionid, versionname, versiontag = version
    blobs = lib.list_blobs_by_path(versiontag, repo_dir=repo_dir)

    blobhashes = np.array([h for h, _ in blobs], dtype="U")
    with stage03_ddb.cursor() as cursor:
        blobhash_to_blobid = dict(
            cursor.sql("""SELECT blobhash, blobid FROM blobs
                          WHERE blobhash IN (SELECT * FROM blobhashes)""").fetchall()
        )

    return pd.DataFrame(
        {
            "versionid": versionid,
            "blobid": (blobhash_to_blobid[blobhash] for blobhash in blobhashes),
            "filepath": (path for _, path in blobs),
        }
    )
```

**`stage04_fill_defs_table(ddb, start_blobid, end_blobid, timer, repo_dir)`**
— add `repo_dir`. Create partial for `stage04_worker`.

**`stage04_worker(args, *, repo_dir)`** — add `repo_dir`. Replace the entire
`scriptLines` + `line.split(b" ")` block with:
```python
defs = lib.parse_defs(blobhash, blobfilename, blobfamily, repo_dir=repo_dir)
for defname, deftype, defline in defs:
    if not lib.isIdent(defname):
        continue
    if deftype not in DEFTYPES:
        continue
    defnames.append(defname)
    deftypes.append(deftype)
    deflines.append(defline)
```
Note: `defname` is now `str` (not `bytes`), so `.decode()` calls are removed.
`deftype` is already `str`. `defline` is already `int`. This directly fixes
the `ValueError` bug.

**`stage05_fill_refs_table(ddb, start_blobid, end_blobid, timer, repo_dir)`**
— add `repo_dir`. Create partial for `stage05_worker`.

**`stage05_worker(args, *, repo_dir)`** — add `repo_dir`. Replace the
`scriptLines("tokenize-file", "-b", ...)` + even/odd tracking with:
```python
lineno = 1
for is_ident, content in lib.tokenize_file(blob_hash=blobhash, family=blobfamily, repo_dir=repo_dir):
    if is_ident:
        refnames.append(content)
        reflines.append(lineno)
    else:
        lineno += content.count("\x01")
```
Note: `content` is now `str` (not `bytes`). No `.decode()` needed. Use
`"\x01"` (str) instead of `b"\1"` (bytes) for line counting.

**`stage06_fill_comps_defs(ddb, start_blobid, end_blobid, timer, repo_dir)`**
— add `repo_dir`. Create partial for `stage06_worker`.

**`stage06_worker(args, *, repo_dir)`** — add `repo_dir`. Replace
`scriptLines("parse-comps", ...)` + `line.split(b" ", 1)` with:
```python
for ident, lineno in lib.parse_comps(blobhash, blobfamily, repo_dir=repo_dir):
    compnames.append(ident)
    complines.append(lineno)
```
Note: `ident` is now `str` (not `bytes`). No `.decode()` needed.

**`stage07_fill_comps_refs(ddb, start_blobid, end_blobid, timer, repo_dir)`**
— add `repo_dir`. Create partial for `stage07_worker`.

**`stage07_worker(args, *, repo_dir)`** — add `repo_dir`. Same pattern as
stage06_worker but with the additional `stage07_all_compnames` filter.

Update `main()` to pass `data_dir`, `repo_dir`, `project` through all stage
calls.

Remove the `if __name__ == "__main__"` block at the bottom of `update.py`
(should no longer be invocable standalone).

### After

`update.py` uses only the new typed `lib` functions. No more `scriptLines()`,
`script()`, `scriptVersions()`, `getDataDir()`, or `configure()`. All `repo_dir`
flow is explicit via function parameters and `functools.partial`.

---

## Step 3: Migrate `query.py` to new `lib` functions

### Before

`query.py` imports `script` and `scriptLines` from `lib`. It has wrapper methods
`self.script()` and `self.scriptLines()` that delegate to the `lib` versions
with `repo_dir=self.repo_dir, project=self.project`. It calls these wrappers in
~8 places.

### Todo

**Update imports**: Replace `from .lib import decode, script, scriptLines` with
`from .lib import decode`.

**Remove** `self.script()` and `self.scriptLines()` wrapper methods.

**Update each call site:**

- **`get_tokenized_file`** (line ~123-151): This is the most complex migration.
  Current code:
  ```python
  tag = self.version_to_tag(version)
  filename = os.path.basename(path)
  family = lib.getFileFamily(filename)
  if family is None:
      return decode(self.script("get-file", tag, path))
  even = True
  prefix = b"CONFIG_" if family == "K" else b""
  tokens = []
  for tok in self.scriptLines("tokenize-file", tag, path, family):
      even = not even
      tokens.append((tok, prefix + tok, even))
  defnames = {tok2.decode() for _, tok2, even in tokens if even}
  ...
  buffer = BytesIO()
  for tok, tok2, even in tokens:
      if even and tok2.decode() in defs:
          buffer.write(b"\033[31m" + tok2 + b"\033[0m")
      else:
          buffer.write(lib.unescape(tok))
  return decode(buffer.getvalue())
  ```

  New code:
  ```python
  tag = self.version_to_tag(version)
  filename = os.path.basename(path)
  family = lib.getFileFamily(filename)
  if family is None:
      return decode(lib.get_file(tag, path, repo_dir=self.repo_dir))
  prefix = "CONFIG_" if family == "K" else ""
  tokens = lib.tokenize_file(version=tag, path=path, family=family, repo_dir=self.repo_dir)
  # tokens is [(is_ident, content_str), ...]
  defnames = {prefix + content for is_ident, content in tokens if is_ident}
  defnames = np.unique(list(defnames))
  defs = self.ddb.sql("""SELECT DISTINCT defname FROM defs
                           WHERE defname IN (SELECT * FROM defnames)""")
  defs = set(defs.df()["defname"])
  buffer = BytesIO()
  for is_ident, content in tokens:
      if is_ident:
          prefixed = prefix + content
          if prefixed in defs:
              buffer.write(b"\033[31m" + prefixed.encode() + b"\033[0m")
          else:
              buffer.write(content.encode())
      else:
          buffer.write(lib.unescape(content.encode()))
  return decode(buffer.getvalue())
  ```

  Key changes: `prefix` is `str`, tokens are `(is_ident, content_str)`,
  `unescape` takes `bytes` so encode `content` before passing. The `BytesIO`
  and final `decode()` remain since the web layer expects `str` output.

- **`get_dir_contents`** (line ~155-158): Replace
  `self.script("get-dir", tag, path)` with
  `lib.get_dir(tag, path, repo_dir=self.repo_dir)`. Rest unchanged.

- **`get_file_type`** (line ~190-193): Replace
  `self.script("get-type", ...)` with
  `lib.get_type(self.version_to_tag(version), path, repo_dir=self.repo_dir)`.
  `get_type` already returns a decoded `str`, so remove the `decode(...).strip()`
  wrapper — just return the result directly (after `.strip()` if needed).

- **`versions_cmd`** (line ~198-203): Replace
  `self.scriptLines("versions")` + manual parsing with
  `lib.versions(repo_dir=self.repo_dir, project=self.project)`. The method now
  just yields the tuples directly:
  ```python
  def versions_cmd(self):
      yield from lib.versions(repo_dir=self.repo_dir, project=self.project)
  ```

- **`get_file_raw`** (line ~215-216): Replace
  `self.script("get-file", ...)` with
  `lib.get_file(self.version_to_tag(version), path, repo_dir=self.repo_dir)`.
  `lib.get_file` returns `bytes`, so `decode()` stays:
  ```python
  def get_file_raw(self, version, path):
      return decode(lib.get_file(self.version_to_tag(version), path, repo_dir=self.repo_dir))
  ```

- **Line ~129** (`get_tokenized_file` early return for `family is None`): Replace
  `self.script("get-file", tag, path)` with
  `lib.get_file(tag, path, repo_dir=self.repo_dir)`.

### After

`query.py` uses only the new typed `lib` functions. No `script`/`scriptLines`
imports or wrappers remain.

---

## Step 4: Update `elixir_cli` and remove global state from `lib.py`

### Before

`elixir_cli` calls `lib.configure(data_dir, repo_dir, project)` before
`update.main()`. `lib.py` has global variables `_data_dir`, `_repo_dir`,
`_project` and accessor functions `getDataDir()`, `getRepoDir()`, `getProject()`,
`currentProject()`. The `script()` function falls back to these globals.

### Todo

**In `elixir_cli`** (`_project_index` function): Remove the `lib.configure()`
call and the `from elixir import lib` import. Pass `data_dir`, `repo_dir`,
`project` directly to `update.main()`:
```python
def _project_index(data_path, project):
    from elixir import update
    project_dir = os.path.join(data_path, project)
    data_dir = os.path.join(project_dir, "data")
    repo_dir = os.path.join(project_dir, "repo")
    update.main(data_dir, repo_dir, project)
```

**Remove dead global state from `lib.py`**. Verified: `lib.configure()`,
`getDataDir()`, `getRepoDir()`, `getProject()`, `currentProject()` are only
used by `update.py` (being migrated away in step 2) and by `script()` itself
(being removed in step 5). No other file in the codebase uses them (confirmed
by exhaustive search — `web.py` only imports `getFileFamily`, `web_utils.py`
only imports `run_cmd`, all filters have no `lib` imports). Remove:
- `_data_dir`, `_repo_dir`, `_project` global variables
- `configure()`, `getDataDir()`, `getRepoDir()`, `getProject()`, `currentProject()`

### After

`elixir_cli` passes explicit arguments. All global state removed from `lib.py`.

---

## Step 5: Remove old dispatch machinery from `lib.py`

### Before

`lib.py` still has the old `_dispatch()`, `script()`, `scriptLines()`,
`scriptVersions()`, and all `_`-prefixed internal functions. Nothing calls them
anymore.

### Todo

Remove these functions from `lib.py`:

- `_dispatch()`
- `script()`
- `scriptLines()`
- `scriptVersions()`
- `_versions()`
- `_list_blobs()`
- `_tokenize_file()`
- `_parse_defs()`
- `_parse_comps()`
- `_get_type()`
- `_get_file()`
- `_get_dir()`
- `_dts_comp()`
- `_denormalize()` — no longer needed, `tokenize_file`/`get_type`/`get_file`/`get_dir`
  use `path.lstrip("/")` internally
- The `TODO` comment on line 10 about `scriptLines` returning strings

Verify no other file in the codebase imports or uses `script`, `scriptLines`,
`scriptVersions`, `_dispatch`, `_denormalize`.

### After

`lib.py` is clean: only typed public functions, no string-dispatch, no
serialization. The old code path is completely gone.

---

## Step 6: Verify and test

### Before

All code changes are complete but untested.

### Todo

1. Run `python -c "from elixir import lib"` to verify no import errors.
2. Run `python -c "from elixir import update"` to verify no import errors.
3. Run `python -c "from elixir import query"` to verify no import errors.
4. Check if there are any tests in the repository and run them.
5. Search the entire codebase for any remaining references to `script`,
   `scriptLines`, `scriptVersions`, `_dispatch`, `lib.configure`,
   `getDataDir`, `getRepoDir`, `_denormalize` to confirm nothing was missed.
6. Look for any other files that might reference the old API (e.g. filters,
   web templates, scripts).

Note: this repository has no automated test suite. The only reliable test is
running `elixir_cli index` against a real project. The `python -c` import checks
above catch syntax and import errors but not runtime type mismatches. Manual
testing with a small repository (like VPP) is recommended after all steps are
complete.

### After

All imports succeed. No stale references to old API remain.

---

## Conclusion

After all steps complete:
- The `ValueError: not enough values to unpack` bug is fixed — `parse_defs`
  returns a structured `list[tuple]` instead of serialized bytes.
- All `lib.py` functions have clear typed signatures — no more string-based
  dispatch.
- `update.py` receives all dependencies explicitly — no global state, no
  `lib.configure()`.
- The code is more maintainable and type-checkable.

When reporting completion, highlight what was done in each step and any issues
encountered (including how they were solved).
