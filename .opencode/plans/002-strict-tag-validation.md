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

## Execution rules

- Run steps sequentially, one at a time
- Each step runs in a subagent (use the task tool)
- Commit after each step completes
- A step is only started when the previous one has committed successfully

## Step 1: Discovery — enumerate all silently-ignored tags per project

### Before

Each `get_versions` function silently drops some tags. We don't know which tags are being dropped for each project, so we can't write exhaustive skip patterns.

### Todo

Write and run a diagnostic script (`_discover_ignored.py`) at the project root that:

1. Imports `PROJECTS` from `elixir.projects` and `_get_tags_sorted` from `elixir.projects`.
2. For each project in `PROJECTS`:
   a. Build `repo_dir = f"data/{project}/repo"`.
   b. Skip if `repo_dir` doesn't exist (print a warning).
   c. Get all tags via `_get_tags_sorted(repo_dir)`.
   d. Get version tags by calling `config.get_versions(repo_dir)` and extracting the first element of each tuple.
   e. Compute `ignored = set(all_tags) - set(version_tags)`.
   f. Print project name, total tag count, version tag count, ignored count, and list each ignored tag.
3. Do NOT commit the script — it's a one-off diagnostic. Just report the output.

The output tells us exactly which tags each project is currently ignoring. We use this to build exhaustive skip patterns in Step 2.

Also note opensbi's tag format — it uses `_default_get_versions` which accepts everything, so we need to learn its tag naming convention from the full tag list.

### After

We have a complete mapping: for each project, the set of tags currently being silently ignored. This data feeds directly into Step 2.

## Step 2: Add `_unexpected_tags` helper and rewrite all `get_versions` functions

### Before

26 `get_versions` functions exist in `elixir/projects.py`. They use a mix of `break` on first non-match, list comprehension filters, and `_default_get_versions` (accept-all). Many silently ignore tags.

### Todo

Rewrite `elixir/projects.py`:

1. **Add helper at the top of the function definitions section:**
   ```python
   def _unexpected_tags(project, tags):
       raise ValueError(f"{project}: unexpected tags: {tags}")
   ```

2. **Rewrite every `get_versions` function** to follow this pattern:
   ```python
   def _PROJECT_get_versions(repo_dir):
       version_pattern = re.compile(r"...")
       skip_patterns = [...]  # or skip_set = {...}, skip_prefixes = (...)
       result = []
       unexpected = []
       for tag in _get_tags_sorted(repo_dir):
           m = version_pattern.match(tag)
           if m:
               result.append((tag, ..., ...))
               continue
           if _matches_any_skip(tag, skip_patterns):  # or explicit checks
               continue
           unexpected.append(tag)
       if unexpected:
           _unexpected_tags("project", unexpected)
       return result
   ```

   Key rules for each function:
   - **Remove all `break` statements** — must iterate every tag
   - **Every tag must hit either `result.append()` or `continue` (skip) or `unexpected.append()`**
   - Skip patterns must be **exhaustive** — cover every tag discovered in Step 1 that is not a version tag
   - Prefer regex patterns over hardcoded tag names where possible (more resilient to new tags)
   - For tags that appear in skip sets (like mesa), keep them but ensure they're accounted for
   - For opensbi: replace `_default_get_versions` reference with a new `_opensbi_get_versions` that has an actual version pattern (discovered in Step 1)

3. **Remove `_default_get_versions`** if no project uses it anymore.
4. **Remove `_tag_pattern_versions`** if unused.
5. **Update the `opensbi` entry in `PROJECTS`** to use the new function.

**Skip pattern guidelines:**
- If a group of tags shares a prefix/suffix pattern, use a regex: `re.compile(r"^(prefix1|prefix2)/")`
- If tags are isolated names with no pattern, add them to a `skip_set = {"tag1", "tag2", ...}`
- Document briefly why each skip group exists (e.g., `# CI/build tags`, `# old naming scheme`)

### After

All 26 `get_versions` functions are strict. Every tag is classified as version or skip. Any unclassified tag raises `ValueError`. No `break` statements remain. `_default_get_versions` and `_tag_pattern_versions` are removed if unused.

## Step 3: Validate against all 26 projects

### Before

All functions have been rewritten with strict validation, but we haven't tested them against actual repo data.

### Todo

1. Run `python3 -m elixir.check_versions` for all projects to verify version format validation still passes.
2. Run each project's `get_versions` function individually to ensure no unexpected tags are found. A simple loop:
   ```python
   from elixir.projects import PROJECTS
   for name, config in PROJECTS.items():
       repo_dir = f"data/{name}/repo"
       if not os.path.isdir(repo_dir):
           print(f"SKIP {name}: no repo")
           continue
       versions = config.get_versions(repo_dir)
       print(f"OK {name}: {len(versions)} versions")
   ```
3. If any project raises `ValueError`, examine the unexpected tags, determine if they're version tags (fix the version pattern) or non-version tags (add to skip list), and update the function accordingly.
4. Re-run until all 26 projects pass.

### After

All 26 projects pass strict validation. Every tag in every repo is accounted for.

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
