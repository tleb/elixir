shiny_versions()
{
    # They are good citizens, thanks vpp.
    # They have RC versions following kernel naming convention.
    git tag --sort=-creatordate | awk '
    match($0, /^v[0-9]+(\.[0-9]+){1,2}(-rc[0-9]+)?$/, arr) {printf "%s\t%s\t%d\n", $0, $0, !!arr[2]; next}
    {exit 1}'
}
