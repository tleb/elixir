# Enable DT bindings compatible strings support
dts_comp_support=1

shiny_versions()
{
    #  - Recent tags are fitting the kernel convention.
    #  - Older versions are more messy, being prefixed with 'U-Boot-', except
    #    for one prefixed with 'U_BOOT_'. There were no RC at that time.
    #  - Many tags exist for "date" releases, prefixed with 'LABEL_' or 'DENX-',
    #    but we ignore them. There are tagged releases at the same time.
    #  - There is a 'v2023.07.01-dont-use' tag.
    git tag --sort=-creatordate | awk '
    /-dont-use$/ || /^LABEL_/ || /^DENX-/ {next}
    match($0, /^v[0-9]+(\.[0-9]+){1,2}(-rc[0-9]+)?$/, arr) {printf "%s\t%s\t%d\n", $0, $0, !!arr[2]; next}
    match($0, /(U-Boot-|U_BOOT_)([0-9]+_[0-9]+_[0-9]+)/, arr) {v = arr[2]; gsub(/_/, ".", v); printf "%s\tv%s\t0\n", $0, v; next}
    {exit 1}'
}
