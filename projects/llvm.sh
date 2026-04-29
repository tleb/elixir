shiny_versions()
{
    #  - Pretty well behaved tagging convention.
    #  - Only special tags are 'llvmorg-23-init'. We name them 'v23.0-init' so
    #    that we still get a major/minor for the version menu. Tagged as RCs.
    git tag --sort=-creatordate | awk '
    match($0, /^llvmorg-([0-9]+(\.[0-9]+){1,2}(-rc[0-9]+)?)$/, arr) {printf "%s\tv%s\t%d\n", $0, arr[1], !!arr[3]; next}
    match($0, /^llvmorg-([0-9]+)-init$/, arr) {printf "%s\tv%s.0-init\t1\n", $0, arr[1]; next}
    {exit 1}'
}
