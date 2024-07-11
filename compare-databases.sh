#!/bin/sh

set -e

if test ! -d "$1" -o ! -d "$2"; then
    echo "usage: $0 <db_a>/ <db_b>/" >&2
    exit 1
fi

for db in blobs definitions doccomments filenames hashes references variables versions; do
    a="$1/$db.db"
    b="$2/$db.db"

    dest_a="/tmp/elixir_cmp_${db}_${$}_a"
    dest_b="/tmp/elixir_cmp_${db}_${$}_b"

    db_dump -p "$a" > "$dest_a"
    db_dump -p "$b" > "$dest_b"

    git --no-pager diff --no-index --stat "$dest_a" "$dest_b"

    rm "$dest_a" "$dest_b"
done

# Optional databases
# for db in compatibledts compatibledts_docs; do
#     a="$1/$db.db"
#     b="$2/$db.db"

#     if test \( -f "$a" -a ! -f "$b" \) -o \( ! -f "$a" -a -f "$b" \); then
#         echo "$db NAK" >&2
#         exit 1
#     fi

#     dest_a="/tmp/elixir_cmp_${db}_${$}_a"
#     dest_b="/tmp/elixir_cmp_${db}_${$}_b"

#     if test -f "$a"; then
#         db_dump -p "$a" > "$dest_a"
#         db_dump -p "$b" > "$dest_b"
#     fi
# done
