shiny_versions()
{
    # Notes:
    #  - No RC versions.
    #  - Skip the cvs/ and fedora/ prefixed tags.
    #  - Notice we allow trailing values after a pretty normal version number.
    #    As of today (latest is v2.43), suffixes are: glibc-2.16-ports-merge,
    #    glibc-2.16-ports-before-merge, glibc-2.16-tps, glibc-2.0.5b.
    #  - Ignore tags with minor 90 or 9000. They are pointing to the start of
    #    dev branches so they contain nothing substantial.

    git tag --sort=-creatordate | awk '
    /^(cvs|fedora)\// || /^changelog-ends-here$/ {next}
    /^glibc-([0-9]+\.){2}(90|9000)$/ {next}
    /^glibc-[0-9]+(\.[0-9]+){1,2}/ {tag = $0; sub(/^glibc-/, "v"); printf "%s\t%s\t0\n", tag, $0; next}
    {exit 1}'
}
