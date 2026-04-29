shiny_versions()
{
    # They are good citizens, thanks musl.
    git tag --sort=-creatordate | awk '
    /^v[0-9]+(\.[0-9]+){2}$/ {printf "%s\t%s\t0\n", $0, $0; next}
    {exit 1}'
}
