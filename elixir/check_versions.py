import argparse
import re
import subprocess
import sys

from .projects import PROJECTS

_VERSION_RE = re.compile(r"^v\d+\.\d+")


def _get_real_tags(repo_dir: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", repo_dir, "tag"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return set()
    return set(out.stdout.splitlines())


def check_versions(data_path: str, project_names: list[str] | None = None) -> bool:
    if project_names is None:
        project_names = sorted(PROJECTS.keys())

    ok = True
    for name in project_names:
        if name not in PROJECTS:
            print(f"unknown project: {name}", file=sys.stderr)
            ok = False
            continue

        config = PROJECTS[name]
        repo_dir = f"{data_path}/{name}/repo"
        real_tags = _get_real_tags(repo_dir)
        if not real_tags:
            print(f"{name}: no tags found in {repo_dir}", file=sys.stderr)
            ok = False
            continue

        try:
            versions = config.get_versions(repo_dir)
        except Exception as e:
            print(f"{name}: get_versions failed: {e}", file=sys.stderr)
            ok = False
            continue

        for i, entry in enumerate(versions):
            if len(entry) != 3:
                print(f"{name}[{i}]: expected 3-tuple, got {entry!r}", file=sys.stderr)
                ok = False
                continue

            tag, version, is_rc = entry

            if tag not in real_tags:
                print(f"{name}[{i}]: tag {tag!r} not in repo", file=sys.stderr)
                ok = False

            if not _VERSION_RE.match(version):
                print(
                    f"{name}[{i}]: version {version!r} doesn't match ^v\\d+\\.\\d+",
                    file=sys.stderr,
                )
                ok = False

            if not isinstance(is_rc, bool):
                print(f"{name}[{i}]: is_rc {is_rc!r} is not bool", file=sys.stderr)
                ok = False

    return ok


def main():
    parser = argparse.ArgumentParser(description="Validate project version listings")
    parser.add_argument("data_path", help="Root data directory")
    parser.add_argument(
        "projects", nargs="*", help="Project names to check (default: all)"
    )
    args = parser.parse_args()

    ok = check_versions(args.data_path, args.projects or None)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
