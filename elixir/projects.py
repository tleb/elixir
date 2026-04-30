import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ProjectConfig:
    remotes: list[str]
    dts_comp_support: bool = False
    get_versions: Callable[[str], list[tuple[str, str, bool]]] = None


def _default_get_versions(repo_dir: str) -> list[tuple[str, str, bool]]:
    out = subprocess.run(
        ["git", "-C", repo_dir, "tag", "--sort=-creatordate"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    return [(tag, tag, False) for tag in out.stdout.splitlines() if tag]


def _tag_pattern_versions(repo_dir: str, pattern: str) -> list[tuple[str, str, bool]]:
    out = subprocess.run(
        ["git", "-C", repo_dir, "tag", "--sort=-creatordate"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    regex = re.compile(pattern)
    return [
        (tag, tag, False)
        for tag in out.stdout.splitlines()
        if tag and regex.search(tag)
    ]


PROJECTS: dict[str, ProjectConfig] = {
    "amazon-freertos": ProjectConfig(
        remotes=["https://github.com/aws/amazon-freertos.git"],
    ),
    "arm-trusted-firmware": ProjectConfig(
        remotes=["https://github.com/ARM-software/arm-trusted-firmware"],
        dts_comp_support=True,
    ),
    "barebox": ProjectConfig(
        remotes=["https://git.pengutronix.de/git/barebox"],
        dts_comp_support=True,
    ),
    "bluez": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/bluetooth/bluez.git"],
    ),
    "busybox": ProjectConfig(
        remotes=["https://git.busybox.net/busybox"],
    ),
    "coreboot": ProjectConfig(
        remotes=["https://review.coreboot.org/coreboot.git"],
    ),
    "dpdk": ProjectConfig(
        remotes=[
            "https://dpdk.org/git/dpdk",
            "https://dpdk.org/git/dpdk-stable",
        ],
    ),
    "freebsd": ProjectConfig(
        remotes=["https://git.freebsd.org/src.git"],
    ),
    "glibc": ProjectConfig(
        remotes=["https://sourceware.org/git/glibc.git"],
    ),
    "grub": ProjectConfig(
        remotes=["https://git.savannah.gnu.org/git/grub.git"],
    ),
    "igt": ProjectConfig(
        remotes=["https://gitlab.freedesktop.org/drm/igt-gpu-tools.git"],
    ),
    "iproute2": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/network/iproute2/iproute2.git"],
    ),
    "linux": ProjectConfig(
        remotes=[
            "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
            "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
            "https://github.com/bootlin/linux-history.git",
        ],
        dts_comp_support=True,
    ),
    "llvm": ProjectConfig(
        remotes=["https://github.com/llvm/llvm-project.git"],
    ),
    "mesa": ProjectConfig(
        remotes=["https://gitlab.freedesktop.org/mesa/mesa.git"],
    ),
    "musl": ProjectConfig(
        remotes=["https://git.musl-libc.org/git/musl"],
    ),
    "ofono": ProjectConfig(
        remotes=["https://git.kernel.org/pub/scm/network/ofono/ofono.git"],
    ),
    "op-tee": ProjectConfig(
        remotes=["https://github.com/OP-TEE/optee_os.git"],
    ),
    "opensbi": ProjectConfig(
        remotes=["https://github.com/riscv-software-src/opensbi"],
    ),
    "qemu": ProjectConfig(
        remotes=["https://gitlab.com/qemu-project/qemu.git"],
    ),
    "toybox": ProjectConfig(
        remotes=["https://github.com/landley/toybox.git"],
    ),
    "u-boot": ProjectConfig(
        remotes=["https://source.denx.de/u-boot/u-boot.git"],
        dts_comp_support=True,
    ),
    "uclibc-ng": ProjectConfig(
        remotes=["https://cgit.uclibc-ng.org/cgi/cgit/uclibc-ng.git"],
    ),
    "vpp": ProjectConfig(
        remotes=["https://gerrit.fd.io/r/vpp"],
    ),
    "xen": ProjectConfig(
        remotes=["https://xenbits.xen.org/git-http/xen.git"],
    ),
    "zephyr": ProjectConfig(
        remotes=["https://github.com/zephyrproject-rtos/zephyr"],
        dts_comp_support=True,
    ),
}

for _p in PROJECTS.values():
    if _p.get_versions is None:
        _p.get_versions = _default_get_versions
