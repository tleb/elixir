shiny_versions()
{
    # This is a mess.
    git tag --sort=-creatordate | awk '
    /-branchpoint$/ || /^chadv\// || /^(texman|texmem)_/ {next}
    /^(cros-mesa|arc-mesa|skl-fast-clear|vulkan|android|embedded)-/ {next}
    /^mesa_[0-9]{8}$/ {next}

    $0 == "7.8-rc1" {next}
    $0 == "7.8-rc2" {next}
    $0 == "before_upgrade_03_01_05" {next}
    $0 == "blended_fountain" {next}
    $0 == "core-context-v2" {next}
    $0 == "gles3-fmt-v1" {next}
    $0 == "gliding_penguin" {next}
    $0 == "i965-primitive-restart-v2" {next}
    $0 == "instanced_arrays-v2" {next}
    $0 == "intel-2012q4.1" {next}
    $0 == "intel_2009q1_rc1" {next}
    $0 == "intel_2009q1_rc2" {next}
    $0 == "intel_2009q1_rc3" {next}
    $0 == "intel_2009q2_rc3" {next}
    $0 == "jump_and_click" {next}
    $0 == "kw-mesa-1" {next}
    $0 == "mesa_texman_20060210" {next}
    $0 == "noisy_cube" {next}
    $0 == "post-merge-glsl-compiler-1" {next}
    $0 == "pre-merge-glsl-compiler-1" {next}
    $0 == "R300_DRIVER_0" {next}
    $0 == "red_tinted_cube" {next}
    $0 == "rgb10_a2ui-v3" {next}
    $0 == "rotating_gears" {next}
    $0 == "shimmering_gears" {next}
    $0 == "snb-magic" {next}
    $0 == "start" {next}
    $0 == "the_perfect_frag" {next}
    $0 == "trunk_20040329" {next}
    $0 == "unichrome-last-xinerama" {next}
    $0 == "useful" {next}
    $0 == "vtx-0-2-21112003-freeze" {next}
    $0 == "vtx-0-2-24112003" {next}
    $0 == "mesa-6_5-20060712" {next}

    $0 == "mesa-10.1-devel" {printf "%s\tv10.1-devel\t1\n", $0; next}
    $0 == "mesa_3_1_beta_3" {printf "%s\t%s\t%d\n", $0, "v3.1-beta-3", 1; next}
    $0 == "mesa_3_2_beta_1" {printf "%s\t%s\t%d\n", $0, "v3.2-beta-1", 1; next}
    match($0, /^mesa-([0-9]+((\.|-)[0-9]+){1,2}(-rc[0-9]+)?(-([0-9]+\.)?[0-9]+)?)$/, arr) {
        printf "%s\tv%s\t%d\n", $0, arr[1], !!arr[4]; next}
    match($0, /^mesa_([0-9]+(_[0-9]+){1,2}(_rc[0-9]+)?(_[0-9]+)?)$/, arr) {
        gsub(/_/, ".", arr[1]); printf "%s\tv%s\t%d\n", $0, arr[1], !!arr[3]; next}
    {exit 1}'
}
