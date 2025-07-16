#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#  and contributors
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

import re

deflist_regex = re.compile(b"(\d*)(\w)(\d*)(\w),?")
deflist_macro_regex = re.compile("\dM\d+(\w)")

##################################################################################

defTypeR = {
    "c": "config",
    "d": "define",
    "e": "enum",
    "E": "enumerator",
    "f": "function",
    "l": "label",
    "M": "macro",
    "m": "member",
    "p": "prototype",
    "s": "struct",
    "t": "typedef",
    "u": "union",
    "v": "variable",
    "x": "externvar",
}

defTypeD = {v: k for k, v in defTypeR.items()}

##################################################################################

maxId = 999999999


class DefList:
    """Stores associations between a blob ID, a type (e.g., "function"),
    a line number and a file family.
    Also stores in which families the ident exists for faster tests."""

    def __init__(self, data=b"#"):
        self.data, self.families = data.split(b"#")

    def iter(self, dummy=False):
        # Get all element in a list of sublists and sort them
        entries = deflist_regex.findall(self.data)
        entries.sort(key=lambda x: int(x[0]))
        for id, type, line, family in entries:
            id = int(id)
            type = defTypeR[type.decode()]
            line = int(line)
            family = family.decode()
            yield id, type, line, family
        if dummy:
            yield maxId, None, None, None

    def append(self, id, type, line, family):
        if type not in defTypeD:
            return
        p = str(id) + defTypeD[type] + str(line) + family
        if self.data != b"":
            p = "," + p
        self.data += p.encode()
        self.add_family(family)

    def pack(self):
        return self.data + b"#" + self.families

    def add_family(self, family):
        family = family.encode()
        if family not in self.families.split(b","):
            if self.families != b"":
                family = b"," + family
            self.families += family

    def get_families(self):
        return self.families.decode().split(",")

    def get_macros(self):
        return deflist_macro_regex.findall(self.data.decode()) or ""


class PathList:
    """Stores associations between a blob ID and a file path.
    Inserted by update.py sorted by blob ID."""

    def __init__(self, data=b""):
        self.data = data

    def iter(self, dummy=False):
        for p in self.data.split(b"\n")[:-1]:
            id, path = p.split(b" ", maxsplit=1)
            id = int(id)
            path = path.decode()
            yield id, path
        if dummy:
            yield maxId, None

    def append(self, id, path):
        p = str(id).encode() + b" " + path + b"\n"
        self.data += p

    def pack(self):
        return self.data


class RefList:
    """Stores a mapping from blob ID to list of lines
    and the corresponding family."""

    def __init__(self, data=b""):
        self.data = data

    def iter(self, dummy=False):
        # Split all elements in a list of sublists and sort them
        entries = [x.split(b":") for x in self.data.split(b"\n")[:-1]]
        entries.sort(key=lambda x: int(x[0]))
        for b, c, d in entries:
            b = int(b.decode())
            c = c.decode()
            d = d.decode()
            yield b, c, d
        if dummy:
            yield maxId, None, None

    def append(self, id, lines, family):
        p = str(id) + ":" + lines + ":" + family + "\n"
        self.data += p.encode()

    def pack(self):
        return self.data
