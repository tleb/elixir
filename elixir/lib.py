#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017  Mikaël Bouillot
#  <mikael.bouillot@bootlin.com>
#
#  Elixir is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Elixir is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#
#  You should have received a copy of the GNU Affero General Public License
#  along with Elixir.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import re
import subprocess
import sys
import tempfile

from . import projects

logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + "/../")

_data_dir = None
_repo_dir = None
_project = None


def configure(data_dir, repo_dir, project):
    global _data_dir, _repo_dir, _project
    _data_dir = data_dir
    _repo_dir = repo_dir
    _project = project


def getDataDir():
    if _data_dir is not None:
        return _data_dir
    try:
        return os.environ["LXR_DATA_DIR"]
    except KeyError:
        print(sys.argv[0] + ": LXR_DATA_DIR needs to be set")
        exit(1)


def getRepoDir():
    if _repo_dir is not None:
        return _repo_dir
    try:
        return os.environ["LXR_REPO_DIR"]
    except KeyError:
        print(sys.argv[0] + ": LXR_REPO_DIR needs to be set")
        exit(1)


def getProject():
    if _project is not None:
        return _project
    return os.path.basename(os.path.dirname(getDataDir()))


def currentProject():
    return getProject()


def _denormalize(path):
    return path[1:]


def _git(repo_dir, *args):
    return subprocess.run(
        ["git", "-C", repo_dir] + list(args),
        capture_output=True,
    )


def _versions(repo_dir, project):
    config = projects.PROJECTS[project]
    versions = config.get_versions(repo_dir)
    lines = [f"{tag}\t{version}\t{int(is_rc)}" for tag, version, is_rc in versions]
    return "\n".join(lines).encode() + b"\n"


def _list_blobs(opts, repo_dir):
    if opts[0] == "-p":
        version = opts[1]
        mode = "path"
    elif opts[0] == "-f":
        version = opts[1]
        mode = "file"
    else:
        version = opts[0]
        mode = "hash"

    out = _git(repo_dir, "ls-tree", "-r", version)
    if out.returncode != 0:
        return b""
    result = []
    for line in out.stdout.split(b"\n"):
        if not line:
            continue
        parts = line.split(b"\t", 1)
        if len(parts) != 2:
            continue
        meta, path = parts
        meta_parts = meta.split()
        if len(meta_parts) < 3:
            continue
        obj_type = meta_parts[1]
        blob_hash = meta_parts[2]
        if obj_type != b"blob":
            continue
        if mode == "path":
            result.append(blob_hash + b" " + path)
        elif mode == "file":
            filename = path.rsplit(b"/", 1)[-1]
            result.append(blob_hash + b" " + filename)
        else:
            result.append(blob_hash)
    return b"\n".join(result) + b"\n"


_TOKENIZE_RE = re.compile(
    r"((/\*.*?\*/|//.*?\x01|[^'\"]"
    r"(\\.|.)*?\""
    r"|# *include *<.*?>|\W)+)"
    r"(\w+)?"
)
_TOKENIZE_RE_D = re.compile(
    r"((/\*.*?\*/|//.*?\x01|[^'\"]"
    r"(\\.|.)*?\""
    r"|# *include *<.*?>|[^\w-])+)"
    r"([\w-]+)?"
)


def _tokenize_file(opts, repo_dir):
    if opts[0] == "-b":
        ref = opts[1]
    else:
        version = opts[0]
        path = opts[1]
        ref = f"{version}:{_denormalize(path)}"

    family = opts[2] if len(opts) > 2 else "C"

    out = _git(repo_dir, "cat-file", "blob", ref)
    if out.returncode != 0:
        return b""

    content = out.stdout.replace(b"\n", b"\x01")

    if family == "D":
        pat = _TOKENIZE_RE_D
    else:
        pat = _TOKENIZE_RE

    decoded = decode(content)
    result = pat.sub(r"\1\n\4\n", decoded)

    lines = result.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]

    return "\n".join(lines).encode() + b"\n"


_ENTRY_RE = re.compile(r"^\s*ENTRY\((\w+)\)")
_SYSCALL_RE = re.compile(r"^SYSCALL_DEFINE\d\(\s*(\w+)\W")


def _parse_defs(opts, repo_dir):
    blob_hash = opts[0]
    filename = opts[1]
    family = opts[2]

    out = _git(repo_dir, "cat-file", "blob", blob_hash)
    if out.returncode != 0:
        return b""

    tmpdir = tempfile.mkdtemp()
    filepath = os.path.join(tmpdir, os.path.basename(filename))

    with open(filepath, "wb") as f:
        f.write(out.stdout)

    lines = []

    if family == "C":
        ctags_out = subprocess.run(
            [
                "ctags",
                "-x",
                "--kinds-c=+p+x",
                "--extras=-{anonymous}",
                filepath,
            ],
            capture_output=True,
            text=True,
        )
        for line in ctags_out.stdout.splitlines():
            if line.startswith("operator ") or line.startswith("CONFIG_"):
                continue
            parts = line.split()
            if len(parts) >= 3:
                lines.append(f"{parts[0]} {parts[1]} {parts[2]}")

        content = decode(out.stdout)
        for lineno, cline in enumerate(content.splitlines(), 1):
            m = _ENTRY_RE.match(cline)
            if m:
                lines.append(f"{m.group(1)} function {lineno}")
            m = _SYSCALL_RE.match(cline)
            if m:
                lines.append(f"sys_{m.group(1)} function {lineno}")

    elif family == "K":
        ctags_out = subprocess.run(
            [
                "ctags",
                "-x",
                "--language-force=kconfig",
                "--kinds-kconfig=c",
                "--extras-kconfig=-{configPrefixed}",
                filepath,
            ],
            capture_output=True,
            text=True,
        )
        for line in ctags_out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                lines.append(f"CONFIG_{parts[0]} {parts[1]} {parts[2]}")

    elif family == "D":
        ctags_out = subprocess.run(
            ["ctags", "-x", "--language-force=dts", filepath],
            capture_output=True,
            text=True,
        )
        for line in ctags_out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                lines.append(f"{parts[0]} {parts[1]} {parts[2]}")

    os.remove(filepath)
    os.rmdir(tmpdir)

    return "\n".join(lines).encode() + b"\n"


def _parse_comps(opts, repo_dir):
    from .find_compatible_dts import FindCompatibleDTS

    blob_hash = opts[0]
    family = opts[1]

    out = _git(repo_dir, "cat-file", "blob", blob_hash)
    if out.returncode != 0:
        return b""

    file_lines = out.stdout.split(b"\n")
    finder = FindCompatibleDTS()
    results = finder.run(file_lines, family)

    return "\n".join(results).encode() + b"\n"


def _get_type(opts, repo_dir):
    version = opts[0]
    path = opts[1]
    ref = f"{version}:{_denormalize(path)}"
    out = _git(repo_dir, "cat-file", "-t", ref)
    if out.returncode != 0:
        return b""
    return out.stdout


def _get_file(opts, repo_dir):
    version = opts[0]
    path = opts[1]
    ref = f"{version}:{_denormalize(path)}"
    out = _git(repo_dir, "cat-file", "blob", ref)
    if out.returncode != 0:
        return b""
    return out.stdout


def _get_dir(opts, repo_dir):
    version = opts[0]
    path = opts[1]
    ref = f"{version}:{_denormalize(path)}"
    out = _git(repo_dir, "ls-tree", "-l", ref)
    if out.returncode != 0:
        return b""

    entries = []
    for line in out.stdout.split(b"\n"):
        if not line:
            continue
        parts = line.split(b"\t", 1)
        if len(parts) != 2:
            continue
        meta, filename = parts
        meta_parts = meta.split()
        if len(meta_parts) < 4:
            continue
        mode, obj_type, hash_val, size = (
            meta_parts[0],
            meta_parts[1],
            meta_parts[2],
            meta_parts[3],
        )

        if filename.startswith(b"."):
            continue

        entries.append((obj_type, filename, size, mode))

    entries.sort(
        key=lambda e: (0 if e[0] == b"tree" else 1, e[1]),
    )

    result = b"\n".join(
        b" ".join([obj_type, filename, size, mode])
        for obj_type, filename, size, mode in entries
    )
    if result:
        result += b"\n"
    return result


def _dts_comp(project):
    if project and project in projects.PROJECTS:
        return str(int(projects.PROJECTS[project].dts_comp_support)).encode() + b"\n"
    return b"0\n"


def _dispatch(cmd, opts, repo_dir, project):
    if cmd == "versions":
        return _versions(repo_dir, project)
    elif cmd == "list-blobs":
        return _list_blobs(opts, repo_dir)
    elif cmd == "tokenize-file":
        return _tokenize_file(opts, repo_dir)
    elif cmd == "parse-defs":
        return _parse_defs(opts, repo_dir)
    elif cmd == "parse-comps":
        return _parse_comps(opts, repo_dir)
    elif cmd == "get-type":
        return _get_type(opts, repo_dir)
    elif cmd == "get-file":
        return _get_file(opts, repo_dir)
    elif cmd == "get-dir":
        return _get_dir(opts, repo_dir)
    elif cmd == "dts-comp":
        return _dts_comp(project)
    else:
        raise RuntimeError(f"Unknown script command: {cmd}")


def script(*args, repo_dir=None, project=None, env=None):
    if repo_dir is None:
        repo_dir = getRepoDir()
    if project is None:
        project = getProject()
    cmd = args[0]
    opts = list(args[1:])
    return _dispatch(cmd, opts, repo_dir, project)


def run_cmd(*args, env=None):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if len(p.stderr) != 0:
        logger.error(
            "command %s printed to stderr: \n%s", str(args), p.stderr.decode("utf-8")
        )
    return p.stdout, p.returncode


def scriptLines(*args, repo_dir=None, project=None, env=None):
    p = script(*args, repo_dir=repo_dir, project=project, env=env)
    p = p.split(b"\n")
    del p[-1]
    return p


def scriptVersions():
    res = []
    for line in scriptLines("versions"):
        tag, version, is_rc = decode(line).split("\t")
        res.append((tag, version, int(is_rc) != 0))
    return res


def unescape(bstr):
    return bstr.replace(b"\1", b"\n")


def decode(byte_object):
    try:
        return byte_object.decode("utf-8")
    except UnicodeDecodeError:
        return byte_object.decode("iso-8859-1")


blacklist = (
    b"NULL",
    b"__",
    b"adapter",
    b"addr",
    b"arg",
    b"attr",
    b"base",
    b"bp",
    b"buf",
    b"buffer",
    b"c",
    b"card",
    b"char",
    b"chip",
    b"cmd",
    b"codec",
    b"const",
    b"count",
    b"cpu",
    b"ctx",
    b"data",
    b"default",
    b"define",
    b"desc",
    b"dev",
    b"driver",
    b"else",
    b"end",
    b"endif",
    b"entry",
    b"err",
    b"error",
    b"event",
    b"extern",
    b"failed",
    b"flags",
    b"h",
    b"host",
    b"hw",
    b"i",
    b"id",
    b"idx",
    b"if",
    b"index",
    b"info",
    b"inline",
    b"int",
    b"irq",
    b"j",
    b"len",
    b"length",
    b"list",
    b"lock",
    b"long",
    b"mask",
    b"mode",
    b"msg",
    b"n",
    b"name",
    b"net",
    b"next",
    b"offset",
    b"ops",
    b"out",
    b"p",
    b"pdev",
    b"port",
    b"priv",
    b"ptr",
    b"q",
    b"r",
    b"rc",
    b"rdev",
    b"reg",
    b"regs",
    b"req",
    b"res",
    b"result",
    b"ret",
    b"return",
    b"retval",
    b"root",
    b"s",
    b"sb",
    b"size",
    b"sizeof",
    b"sk",
    b"skb",
    b"spec",
    b"start",
    b"state",
    b"static",
    b"status",
    b"struct",
    b"t",
    b"tmp",
    b"tp",
    b"type",
    b"val",
    b"value",
    b"vcpu",
    b"x",
)


def isIdent(bstr):
    if len(bstr) < 2 or bstr in blacklist or bstr.startswith(b"~"):
        return False
    else:
        return True


def autoBytes(arg):
    if type(arg) is str:
        arg = arg.encode()
    elif type(arg) is int:
        arg = str(arg).encode()
    return arg


def getFileFamily(filename):
    assert isinstance(filename, str)
    name, ext = os.path.splitext(filename)
    name, ext = name.lower(), ext.lower()

    if ext in [".c", ".cc", ".cpp", ".c++", ".cxx", ".h", ".s"]:
        return "C"
    elif ext in [".dts", ".dtsi"]:
        return "D"
    elif name.startswith("kconfig") and ext != ".rst":
        return "K"
    elif name.startswith("makefile") and ext != ".rst":
        return "M"
    else:
        return None
