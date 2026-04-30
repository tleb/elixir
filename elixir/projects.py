import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ProjectConfig:
    remotes: list[str]
    dts_comp_support: bool = False
    get_versions: Callable[[str], list[tuple[str, str, bool]]] = None


def _get_tags_sorted(repo_dir: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", repo_dir, "tag", "--sort=-creatordate"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    return [tag for tag in out.stdout.splitlines() if tag]


def _default_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    return [(tag, tag, False) for tag in _get_tags_sorted(repo_dir)]


def _tag_pattern_versions(repo_dir: str, pattern: str) -> list[tuple[str, str, bool]]:
    regex = re.compile(pattern)
    return [
        (tag, tag, False) for tag in _get_tags_sorted(repo_dir) if regex.search(tag)
    ]


def _musl_uclibc_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    pattern = re.compile(r"^v\d+(\.\d+){2}$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if pattern.match(tag):
            result.append((tag, tag, False))
        else:
            break
    return result


def _barebox_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    skip = re.compile(
        r"^v2\.0\.0-rc\d+$"
        r"|^freescale-mx35-3-stack-20092611-1$"
        r"|^v2011\.04\.0-phytec-pcm049$"
    )
    pattern = re.compile(r"^v\d+(\.\d+){2}$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if skip.match(tag):
            continue
        if pattern.match(tag):
            result.append((tag, tag, False))
        else:
            break
    return result


def _glibc_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    skip = re.compile(
        r"^(cvs|fedora)/"
        r"|^changelog-ends-here$"
        r"|^glibc-(\d+\.){2}(90|9000)$"
    )
    pattern = re.compile(r"^glibc-\d+(\.\d+){1,2}")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if skip.match(tag):
            continue
        if pattern.match(tag):
            result.append((tag, "v" + tag[6:], False))
        else:
            break
    return result


def _igt_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    p1 = re.compile(r"^(intel|igt)-gpu-tools-(.+)")
    p2 = re.compile(r"^v?(\d+(\.\d+){1,2})$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p1.match(tag)
        if m:
            result.append((tag, "v" + m.group(2), False))
            continue
        m = p2.match(tag)
        if m:
            result.append((tag, "v" + m.group(1), False))
            continue
        break
    return result


def _llvm_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    p1 = re.compile(r"^llvmorg-(\d+(\.\d+){1,2}(-rc\d+)?)$")
    p2 = re.compile(r"^llvmorg-(\d+)-init$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p1.match(tag)
        if m:
            result.append((tag, "v" + m.group(1), bool(m.group(3))))
            continue
        m = p2.match(tag)
        if m:
            result.append((tag, f"v{m.group(1)}.0-init", True))
            continue
        break
    return result


def _mesa_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    skip_starts = (
        "chadv/",
        "texman_",
        "texmem_",
        "cros-mesa-",
        "arc-mesa-",
        "skl-fast-clear-",
        "vulkan-",
        "android-",
        "embedded-",
    )
    skip_set = {
        "7.8-rc1",
        "7.8-rc2",
        "before_upgrade_03_01_05",
        "blended_fountain",
        "core-context-v2",
        "gles3-fmt-v1",
        "gliding_penguin",
        "i965-primitive-restart-v2",
        "instanced_arrays-v2",
        "intel-2012q4.1",
        "intel_2009q1_rc1",
        "intel_2009q1_rc2",
        "intel_2009q1_rc3",
        "intel_2009q2_rc3",
        "jump_and_click",
        "kw-mesa-1",
        "mesa_texman_20060210",
        "noisy_cube",
        "post-merge-glsl-compiler-1",
        "pre-merge-glsl-compiler-1",
        "R300_DRIVER_0",
        "red_tinted_cube",
        "rgb10_a2ui-v3",
        "rotating_gears",
        "shimmering_gears",
        "snb-magic",
        "start",
        "the_perfect_frag",
        "trunk_20040329",
        "unichrome-last-xinerama",
        "useful",
        "vtx-0-2-21112003-freeze",
        "vtx-0-2-24112003",
        "mesa-6_5-20060712",
    }
    skip_date = re.compile(r"^mesa_\d{8}$")
    p1 = re.compile(r"^mesa-(\d+((\.|-)\d+){1,2}(-rc\d+)?(-(\d+\.)?\d+)?)$")
    p2 = re.compile(r"^mesa_(\d+(_\d+){1,2}(_rc\d+)?(_\d+)?)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if tag.endswith("-branchpoint"):
            continue
        if tag.startswith(skip_starts):
            continue
        if skip_date.match(tag):
            continue
        if tag in skip_set:
            continue
        if tag == "mesa-10.1-devel":
            result.append((tag, "v10.1-devel", True))
            continue
        if tag == "mesa_3_1_beta_3":
            result.append((tag, "v3.1-beta-3", True))
            continue
        if tag == "mesa_3_2_beta_1":
            result.append((tag, "v3.2-beta-1", True))
            continue
        m = p1.match(tag)
        if m:
            result.append((tag, "v" + m.group(1), bool(m.group(4))))
            continue
        m = p2.match(tag)
        if m:
            result.append((tag, "v" + m.group(1).replace("_", "."), bool(m.group(3))))
            continue
        break
    return result


def _optee_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    pattern = re.compile(r"^\d+\.\d+\.\d+(-rc\d+)?$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if tag == "20160825-for-lmg":
            continue
        m = pattern.match(tag)
        if m:
            result.append((tag, "v" + tag, bool(m.group(1))))
            continue
        break
    return result


def _uboot_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    p1 = re.compile(r"^v\d+(\.\d+){1,2}(-rc\d+)?$")
    p2 = re.compile(r"(U-Boot-|U_BOOT_)(\d+_\d+_\d+)")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if (
            tag.endswith("-dont-use")
            or tag.startswith("LABEL_")
            or tag.startswith("DENX-")
        ):
            continue
        m = p1.match(tag)
        if m:
            result.append((tag, tag, bool(m.group(2))))
            continue
        m = p2.search(tag)
        if m:
            result.append((tag, "v" + m.group(2).replace("_", "."), False))
            continue
        break
    return result


def _vpp_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    pattern = re.compile(r"^v\d+(\.\d+){1,2}(-rc\d+)?$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = pattern.match(tag)
        if m:
            result.append((tag, tag, bool(m.group(2))))
            continue
        break
    return result


def _amazon_freertos_get_versions(repo_dir):
    p_ym = re.compile(r"^(\d{4})(\d{2})\.(\d+)$")
    p_v = re.compile(r"^v\d+\.\d+\.\d+$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p_ym.match(tag)
        if m:
            result.append(
                (tag, f"v{m.group(1)}.{int(m.group(2))}.{int(m.group(3))}", False)
            )
            continue
        if p_v.match(tag):
            result.append((tag, tag, False))
    return result


def _arm_trusted_firmware_get_versions(repo_dir):
    p_v = re.compile(r"^v\d+\.\d+")
    p_for = re.compile(r"^for-v0\.4(.*)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if tag.startswith("sandbox/"):
            continue
        if tag.startswith("lts-v"):
            continue
        m = p_for.match(tag)
        if m:
            suffix = m.group(1)
            result.append((tag, "v0.4" + suffix, "-rc" in suffix))
            continue
        if p_v.match(tag):
            result.append((tag, tag, "-rc" in tag))
    return result


def _bluez_get_versions(repo_dir):
    p = re.compile(r"^\d+\.\d+$")
    return [
        (tag, "v" + tag, False) for tag in _get_tags_sorted(repo_dir) if p.match(tag)
    ]


def _busybox_get_versions(repo_dir):
    p = re.compile(r"^\d+_\d+(_\d+)?$")
    return [
        (tag, "v" + tag.replace("_", "."), False)
        for tag in _get_tags_sorted(repo_dir)
        if p.match(tag)
    ]


def _coreboot_get_versions(repo_dir):
    p = re.compile(r"^\d+\.\d+(\.\d+)?$")
    return [
        (tag, "v" + tag, False) for tag in _get_tags_sorted(repo_dir) if p.match(tag)
    ]


def _dpdk_get_versions(repo_dir):
    p = re.compile(r"^v\d+\.\d+")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if p.match(tag):
            result.append((tag, tag, "-rc" in tag))
    return result


def _freebsd_get_versions(repo_dir):
    p = re.compile(r"^release/(\d+\.\d+\.\d+)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p.match(tag)
        if m:
            result.append((tag, "v" + m.group(1), False))
    return result


def _grub_get_versions(repo_dir):
    p = re.compile(r"^(grub-)?(\d+\.\d+.*)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p.match(tag)
        if m:
            result.append((tag, "v" + m.group(2), "-rc" in tag))
    return result


def _linux_get_versions(repo_dir):
    p = re.compile(r"^v\d+\.\d+")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if p.match(tag):
            result.append((tag, tag, "-rc" in tag))
    return result


def _ofono_get_versions(repo_dir):
    p = re.compile(r"^\d+\.\d+$")
    return [
        (tag, "v" + tag, False) for tag in _get_tags_sorted(repo_dir) if p.match(tag)
    ]


def _qemu_get_versions(repo_dir):
    p_v = re.compile(r"^v\d+\.\d+")
    p_rel = re.compile(r"^release_(\d+(?:_\d+)+)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if p_v.match(tag):
            result.append((tag, tag, "-rc" in tag))
            continue
        m = p_rel.match(tag)
        if m:
            result.append((tag, "v" + m.group(1).replace("_", "."), False))
    return result


def _toybox_get_versions(repo_dir):
    p = re.compile(r"^\d+\.\d+(\.\d+)?$")
    return [
        (tag, "v" + tag, False) for tag in _get_tags_sorted(repo_dir) if p.match(tag)
    ]


def _xen_get_versions(repo_dir):
    p = re.compile(r"^RELEASE-(\d+\.\d+.*)$")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        m = p.match(tag)
        if m:
            result.append((tag, "v" + m.group(1), False))
    return result


def _zephyr_get_versions(repo_dir):
    p = re.compile(r"^v\d+\.\d+")
    result = []
    for tag in _get_tags_sorted(repo_dir):
        if tag.startswith("zephyr-v"):
            continue
        if p.match(tag):
            result.append((tag, tag, "-rc" in tag))
    return result


def _iproute2_get_versions(repo_dir):
    p = re.compile(r"^v\d+\.\d+")
    return [(tag, tag, False) for tag in _get_tags_sorted(repo_dir) if p.match(tag)]


PROJECTS: dict[str, ProjectConfig] = {
    "amazon-freertos": ProjectConfig(
        remotes=["https://github.com/aws/amazon-freertos.git"],
        get_versions=_amazon_freertos_get_versions,
    ),
    "arm-trusted-firmware": ProjectConfig(
        remotes=["https://github.com/ARM-software/arm-trusted-firmware"],
        dts_comp_support=True,
        get_versions=_arm_trusted_firmware_get_versions,
    ),
    "barebox": ProjectConfig(
        remotes=["https://git.pengutronix.de/git/barebox"],
        dts_comp_support=True,
        get_versions=_barebox_get_versions,
    ),
    "bluez": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/bluetooth/bluez.git"],
        get_versions=_bluez_get_versions,
    ),
    "busybox": ProjectConfig(
        remotes=["https://git.busybox.net/busybox"],
        get_versions=_busybox_get_versions,
    ),
    "coreboot": ProjectConfig(
        remotes=["https://review.coreboot.org/coreboot.git"],
        get_versions=_coreboot_get_versions,
    ),
    "dpdk": ProjectConfig(
        remotes=[
            "https://dpdk.org/git/dpdk",
            "https://dpdk.org/git/dpdk-stable",
        ],
        get_versions=_dpdk_get_versions,
    ),
    "freebsd": ProjectConfig(
        remotes=["https://git.freebsd.org/src.git"],
        get_versions=_freebsd_get_versions,
    ),
    "glibc": ProjectConfig(
        remotes=["https://sourceware.org/git/glibc.git"],
        get_versions=_glibc_get_versions,
    ),
    "grub": ProjectConfig(
        remotes=["https://git.savannah.gnu.org/git/grub.git"],
        get_versions=_grub_get_versions,
    ),
    "igt": ProjectConfig(
        remotes=["https://gitlab.freedesktop.org/drm/igt-gpu-tools.git"],
        get_versions=_igt_get_versions,
    ),
    "iproute2": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/network/iproute2/iproute2.git"],
        get_versions=_iproute2_get_versions,
    ),
    "linux": ProjectConfig(
        remotes=[
            "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
            "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
            "https://github.com/bootlin/linux-history.git",
        ],
        dts_comp_support=True,
        get_versions=_linux_get_versions,
    ),
    "llvm": ProjectConfig(
        remotes=["https://github.com/llvm/llvm-project.git"],
        get_versions=_llvm_get_versions,
    ),
    "mesa": ProjectConfig(
        remotes=["https://gitlab.freedesktop.org/mesa/mesa.git"],
        get_versions=_mesa_get_versions,
    ),
    "musl": ProjectConfig(
        remotes=["https://git.musl-libc.org/git/musl"],
        get_versions=_musl_uclibc_get_versions,
    ),
    "ofono": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/network/ofono/ofono.git"],
        get_versions=_ofono_get_versions,
    ),
    "op-tee": ProjectConfig(
        remotes=["https://github.com/OP-TEE/optee_os.git"],
        get_versions=_optee_get_versions,
    ),
    "opensbi": ProjectConfig(
        remotes=["https://github.com/riscv-software-src/opensbi"],
        get_versions=_default_get_versions,
    ),
    "qemu": ProjectConfig(
        remotes=["https://gitlab.com/qemu-project/qemu.git"],
        get_versions=_qemu_get_versions,
    ),
    "toybox": ProjectConfig(
        remotes=["https://github.com/landley/toybox.git"],
        get_versions=_toybox_get_versions,
    ),
    "u-boot": ProjectConfig(
        remotes=["https://source.denx.de/u-boot/u-boot.git"],
        dts_comp_support=True,
        get_versions=_uboot_get_versions,
    ),
    "uclibc-ng": ProjectConfig(
        remotes=["https://cgit.uclibc-ng.org/cgi/cgit/uclibc-ng.git"],
        get_versions=_musl_uclibc_get_versions,
    ),
    "vpp": ProjectConfig(
        remotes=["https://gerrit.fd.io/r/vpp"],
        get_versions=_vpp_get_versions,
    ),
    "xen": ProjectConfig(
        remotes=["https://xenbits.xen.org/git-http/xen.git"],
        get_versions=_xen_get_versions,
    ),
    "zephyr": ProjectConfig(
        remotes=["https://github.com/zephyrproject-rtos/zephyr"],
        dts_comp_support=True,
        get_versions=_zephyr_get_versions,
    ),
}
