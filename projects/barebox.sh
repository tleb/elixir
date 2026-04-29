# Enable DT bindings compatible strings support
dts_comp_support=1

shiny_versions()
{
    #  - Two oddly specific releases: 'v2011.04.0-phytec-pcm049' and
    #    'freescale-mx35-3-stack-20092611-1'.
    #  - For some reason v2.0.0 had RC from 1 to 10, including some
    #    custom '-rc10-ptx...'. Let's ignore them all.
    git tag --sort=-creatordate | awk '
    /v2.0.0-rc[0-9]+/ {next}
    /freescale-mx35-3-stack-20092611-1/ {next}
    /v2011.04.0-phytec-pcm049/ {next}
    /^v[0-9]+(\.[0-9]+){2}$/ {printf "%s\t%s\t0\n", $0, $0; next}
    {exit 1}'
}
