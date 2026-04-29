shiny_versions()
{
    # The four tag name styles currently (latest is v2.4):
    #  - intel-gpu-tools-1.3   => replace prefix by 'v'
    #  - igt-gpu-tools-1.23    => replace prefix by 'v'
    #  - 1.0                   => add 'v' prefix
    #  - v1.27                 => good

    git tag --sort=-creatordate | awk '
    /^(intel|igt)-gpu-tools-/ {tag = $0; sub(/^(intel|igt)-gpu-tools-/, "v"); printf "%s\t%s\t0\n", tag, $0; next}
    /^v?[0-9]+(\.[0-9]+){1,2}$/ {tag = $0; sub(/^v/, ""); printf "%s\tv%s\t0\n", tag, $0; next}
    {exit 1}'
}
