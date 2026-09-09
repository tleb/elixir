#!/bin/sh
# Capture golden outputs of the script.sh tag commands, one directory per
# project under t/goldens/, from the tags-only repos under the Elixir data
# directory (default: ../elixir-data relative to this repository).
#
# These are the regression goldens of the Python port: t/test_goldens.py
# asserts that elixir/repo.py (TAG_PIPELINES and the get-type port)
# reproduces them byte for byte. The port is C-locale (bytes), matching
# the LC_ALL=C pinned here.
#
# Each repo is expected at <data>/<project>/repo, as created by:
#   git init --bare <dir> &&
#   git -C <dir> remote add origin <url> &&
#   git -C <dir> fetch --depth 1 --filter=tree:0 origin '+refs/tags/*:refs/tags/*'
# busybox, freebsd and xen use --filter=blob:none instead (get-type needs to
# walk the tree of one tag; any missing blob is lazily fetched by cat-file).
#
# Usage (from the repository root): t/goldens/capture.sh [data_dir]
# Output is deterministic: LC_ALL is pinned, no timestamps are written.

set -u

LC_ALL=C
export LC_ALL

golden_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
elixir_dir=$(dirname "$(dirname "$golden_dir")")
script="$elixir_dir/script.sh"
data_dir=${1:-${ELIXIR_DATA_DIR:-"$elixir_dir/../elixir-data"}}

fail=0

# Map a display version back to its git tag (inverse of the plugin's
# version_rev()).
real_tag()
{
    case $1 in
    busybox) printf '%s\n' "$2" | tr '._' '_.' ;;
    freebsd)
        case $2 in
        v*.*.*) printf '%s\n' "$2" | sed -e 's,^v,release/,' ;;
        *) printf '%s\n' "$2" | sed -e 's,^v,release/,' -e 's,$,.0,' ;;
        esac
        ;;
    xen) printf '%s\n' "$2" | sed -e 's,^v,RELEASE-,' ;;
    esac
}

for repo in "$data_dir"/*/repo; do
    [ -d "$repo" ] || continue
    project=$(basename "$(dirname "$repo")")
    out="$golden_dir/$project"
    rm -rf "$out"
    mkdir -p "$out"

    export LXR_REPO_DIR="$repo"

    "$script" list-tags > "$out/list-tags.out" || fail=1
    "$script" list-tags -h > "$out/list-tags-h.out" || fail=1
    "$script" get-latest-tags > "$out/get-latest-tags.out" || fail=1
    "$script" dts-comp > "$out/dts-comp.out" || fail=1

    # Projects with real version translation: also capture get-type at the
    # newest display version (third column of list-tags -h), on the first
    # clean file of the matching tag, exactly the way query.py calls it.
    case $project in
    busybox | freebsd | xen)
        disp=$("$script" list-tags -h | awk 'NF == 3 { print $3; exit }')
        tag=$(real_tag "$project" "$disp")
        path=$(git -C "$repo" ls-tree -r "$tag" |
            awk -F'\t' '$1 ~ / blob / { p = $2 }
                        p != "" && p !~ /[ "]/ { print p; exit }')
        type=$("$script" get-type "$disp" "/$path")
        if [ -z "$disp" ] || [ -z "$tag" ] || [ -z "$path" ] || [ -z "$type" ]; then
            echo "$0: get-type capture failed for $project" \
                 "(disp='$disp' tag='$tag' path='$path' type='$type')" >&2
            fail=1
        fi
        {
            echo "# args: $disp /$path"
            printf '%s\n' "$type"
        } > "$out/get-type.out"
        ;;
    esac
done

exit $fail
