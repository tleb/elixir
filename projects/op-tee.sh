shiny_versions()
{
    # Clean. Only one weird tag.
    git tag --sort=-creatordate | awk '
    $0 == "20160825-for-lmg" {next}
    match($0, /^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$/, arr) {printf "%s\tv%s\t%d\n", $0, $0, !!arr[1]; next}
    {exit 1}'
}
