# Plan 002: Strict Tag Validation in `get_versions`

## Preamble

**Project:** Elixir — a source code cross-referencer that indexes C/C++ projects (Linux, musl, QEMU, etc.) and provides identifier search, source browsing, and cross-reference navigation.

**Context:** `elixir/projects.py` defines 26 project configurations, each with a `get_versions` function that lists git tags to index. These functions parse a repo's tag list and return `(tag, version_string, is_rc)` tuples. Currently, many of these functions silently ignore tags that don't match their expected patterns — either by `break`ing out of the loop (ignoring all older tags) or by filtering in list comprehensions. This means new or changed tag naming conventions go unnoticed, potentially missing releases or silently dropping them.

**Goal:** Make every `get_versions` function validate all tags: each tag must be classified as either a version tag (returned) or a known non-version tag (explicitly skipped). Any unclassified tag causes the function to collect it and raise a `ValueError` listing all unexpected tags. This acts as a safeguard: if a project changes its tagging scheme, we get a clear error instead of silently dropping releases.

**Key decisions:**
- **Collect all, then report:** Rather than failing on the first unexpected tag, functions collect all unexpected tags and report them in a single error. This gives a complete picture of what needs fixing.
- **opensbi also strict:** Though opensbi currently uses `_default_get_versions` (accepts everything), the user confirmed it should also have a specific version pattern.
- **Two-phase approach:** First discover what's being ignored, then write exhaustive skip patterns. This avoids guesswork.
- **Keep `_get_tags_sorted` as-is:** Tags are fetched via `git tag --sort=-creatordate`. The sorting is kept for consistency, but we no longer rely on the sort order for correctness (no more `break` on first non-match).
- **Fail loudly is acceptable:** `ValueError` from `get_versions` will crash the caller (indexer, CLI, web app). This is intentional — a silent drop is worse than a loud failure. When a project adds unexpected tags, the error tells you exactly which ones, and you update the skip patterns.

**Prerequisite:** Steps 1 and 3 require populated bare git repos under `data/{project}/repo/`. These are maintained by the project's `./elixir fetch` command. If missing, run `./elixir fetch data` first.

## Execution rules

- Run steps sequentially, one at a time
- Each step runs in a subagent (use the task tool)
- Commit after each step completes
- A step is only started when the previous one has committed successfully

## Step 1: Discovery — enumerate all silently-ignored tags per project

### Before

Each `get_versions` function silently drops some tags. We don't know which tags are being dropped for each project, so we can't write exhaustive skip patterns.

### Todo

Write and run a diagnostic script at `<repo-root>/_discover_ignored.py` (i.e. `/home/tleb/prog/public/elixir/_discover_ignored.py`) that:

1. Imports `PROJECTS` from `elixir.projects` and `_get_tags_sorted` from `elixir.projects`.
2. For each project in `PROJECTS` (alphabetical order):
   a. Build `repo_dir = f"data/{project}/repo"`.
   b. Skip if `repo_dir` doesn't exist (print a warning).
   c. Get all tags via `_get_tags_sorted(repo_dir)`.
   d. Get version tags by calling `config.get_versions(repo_dir)` and extracting the first element of each tuple.
   e. Compute `ignored = set(all_tags) - set(version_tags)`.
   f. Print project name, total tag count, version tag count, ignored count, and list each ignored tag.
3. **Save the full output to `.opencode/plans/002-discovery-results.md`** as a markdown file with one section per project. This file is the artifact that Step 2's subagent reads to write skip patterns.
4. Do NOT commit the diagnostic script — it's one-off. Delete it after saving results. Do commit the discovery results file.

Note: `musl` and `uclibc-ng` share `_musl_uclibc_get_versions`, so the discovery data for both repos must be examined together when writing skip patterns for that function.

Note: opensbi uses `_default_get_versions` which accepts every tag — so its "ignored" set will be empty. Instead, the full tag list itself tells us its naming convention.

### After

File `.opencode/plans/002-discovery-results.md` exists, committed, containing per-project lists of all ignored tags. This is the input for Steps 2a and 2b.

## Step 2a: Rewrite `break`-based functions (9 functions across 10 projects)

### Before

The following functions use the `break` pattern — they iterate tags sorted by date and stop at the first non-match, silently ignoring everything after it:

| Function | Projects |
|---|---|
| `_musl_uclibc_get_versions` | musl, uclibc-ng (shared) |
| `_barebox_get_versions` | barebox |
| `_glibc_get_versions` | glibc |
| `_igt_get_versions` | igt |
| `_llvm_get_versions` | llvm |
| `_mesa_get_versions` | mesa |
| `_optee_get_versions` | op-tee |
| `_uboot_get_versions` | u-boot |
| `_vpp_get_versions` | vpp |

These are the highest-risk rewrites because they currently never see most tags.

### Todo

1. **Add helper** at the top of the function definitions section (after `_tag_pattern_versions`):
   ```python
   def _fail_on_unexpected_tags(project, tags):
       raise ValueError(f"{project}: unexpected tags: {tags}")
   ```

2. **Rewrite each of the 9 functions** following this pattern:
   ```python
   def _PROJECT_get_versions(repo_dir):
       version_pattern = re.compile(r"...")
       skip_set = {...}
       skip_prefixes = (...)
       skip_re = re.compile(r"...")

       result = []
       unexpected = []
       for tag in _get_tags_sorted(repo_dir):
           # Check skip patterns BEFORE version patterns
           if tag in skip_set or tag.startswith(skip_prefixes) or skip_re.match(tag):
               continue
           m = version_pattern.match(tag)
           if m:
               result.append((tag, ..., ...))
               continue
           unexpected.append(tag)
       if unexpected:
           _fail_on_unexpected_tags("project", unexpected)
       return result
   ```

   Key rules:
   - **Remove all `break` statements** — must iterate every tag
   - **Check skip patterns BEFORE version patterns** — preserves existing behavior where tags like `v2.0.0-rc1` are intentionally excluded even though they match the version regex
   - **Every tag must hit either `result.append()` or `continue` (skip) or `unexpected.append()`**
   - **Skip patterns must be exhaustive** — read `.opencode/plans/002-discovery-results.md` to find all ignored tags for this project, then write patterns that match every one of them
   - For `_musl_uclibc_get_versions`: skip patterns must cover non-version tags from BOTH musl and uclibc-ng repos (union of both)
   - Prefer regex patterns over hardcoded tag names where possible (more resilient to new tags of the same family)
   - If a group of tags shares a prefix, use `tag.startswith()` or a regex; if isolated names, use a `skip_set`
   - Every version string (second tuple element) must match `^v\d+\.\d+` — this is enforced by `check_versions.py`
   - Do NOT add comments explaining skip groups

3. **Commit** with message like: `"Step 2a: Rewrite break-based get_versions functions with strict tag validation"`

### After

The 9 `break`-based functions are strict — they iterate all tags, classify each, and raise on unexpected ones. No `break` statements remain in these functions. The helper `_fail_on_unexpected_tags` is added. Other functions are untouched.

## Step 2b: Rewrite filter-based functions + opensbi + cleanup (16 functions across 16 projects)

### Before

The following functions use list comprehension or loop filters — they iterate all tags but silently drop non-matches:

| Function | Projects |
|---|---|
| `_amazon_freertos_get_versions` | amazon-freertos |
| `_arm_trusted_firmware_get_versions` | arm-trusted-firmware |
| `_bluez_get_versions` | bluez |
| `_busybox_get_versions` | busybox |
| `_coreboot_get_versions` | coreboot |
| `_dpdk_get_versions` | dpdk |
| `_freebsd_get_versions` | freebsd |
| `_grub_get_versions` | grub |
| `_linux_get_versions` | linux |
| `_ofono_get_versions` | ofono |
| `_qemu_get_versions` | qemu |
| `_toybox_get_versions` | toybox |
| `_xen_get_versions` | xen |
| `_zephyr_get_versions` | zephyr |
| `_iproute2_get_versions` | iproute2 |
| (new) `_opensbi_get_versions` | opensbi |

opensbi currently uses `_default_get_versions` which accepts every tag.

### Todo

1. **Rewrite each of the 15 existing filter-based functions** using the same pattern as Step 2a (see template there). Read `.opencode/plans/002-discovery-results.md` for each project's ignored tags and write exhaustive skip patterns.

2. **Create `_opensbi_get_versions`**: Read the full tag list for opensbi from the discovery results. Then:
   - If opensbi has a clean dominant version pattern → use it as the version pattern, skip the rest
   - If it's heterogeneous → write a multi-pattern version matcher (like `_igt_get_versions`) with explicit skip patterns
   - If it has very few tags → it's acceptable to enumerate known version tags explicitly
   - Update the `opensbi` entry in `PROJECTS` dict to use `get_versions=_opensbi_get_versions`

3. **Remove dead code:**
   - Remove `_default_get_versions` (no project uses it after opensbi switch)
   - Remove `_tag_pattern_versions` (not referenced by any project — confirmed unused)

4. **Commit** with message like: `"Step 2b: Rewrite filter-based get_versions functions with strict tag validation, add opensbi, remove dead code"`

### After

All 25 `get_versions` functions across 26 project entries are strict. Every tag is classified. No `break` statements remain anywhere. `_default_get_versions` and `_tag_pattern_versions` are removed. The `opensbi` entry uses `_opensbi_get_versions`.

**Edge case — zero tags:** If `_get_tags_sorted` returns an empty list (repo missing or git error), functions return an empty `result` with no error. This is acceptable — empty repos are handled upstream by the data-fetch logic, not by version validation.

## Step 3: Validate against all 26 projects

### Before

All functions have been rewritten with strict validation, but we haven't tested them against actual repo data.

### Todo

1. Run `python3 -m elixir.check_versions` for all projects to verify version format validation still passes.
2. Run each project's `get_versions` function individually to ensure no unexpected tags are found:
   ```python
   import os
   from elixir.projects import PROJECTS
   for name, config in PROJECTS.items():
       repo_dir = f"data/{name}/repo"
       if not os.path.isdir(repo_dir):
           print(f"SKIP {name}: no repo")
           continue
       versions = config.get_versions(repo_dir)
       print(f"OK {name}: {len(versions)} versions")
   ```
3. If any project raises `ValueError`, examine the unexpected tags:
   - If they're version tags → fix the version pattern
   - If they're non-version tags → add to skip patterns
   - Update the function and re-run
4. Iterate until all 26 projects pass.
5. **Commit** any fixes with message like: `"Step 3: Fix skip patterns after validation"` (only if fixes were needed).

### After

All 26 projects pass strict validation. Every tag in every repo is accounted for. `check_versions` passes.

## Conclusion

Once all steps are complete:

- **No silent tag drops:** Every `get_versions` function validates all tags. New or changed tag naming conventions surface as errors immediately.
- **opensbi has a proper version pattern:** It no longer blindly accepts every tag.
- **Dead code removed:** `_default_get_versions` and `_tag_pattern_versions` are gone.
- **Future maintenance:** When a project adds a new tag type, the error message tells you exactly which tags are unexpected, making it easy to update skip patterns.

---

**Upon completion, report:**
- Which projects needed skip patterns added or expanded
- Which projects (if any) had version patterns that needed fixing because previously-ignored tags turned out to be real versions
- Whether `_default_get_versions` and `_tag_pattern_versions` were removed
- Any issues encountered and how they were solved
