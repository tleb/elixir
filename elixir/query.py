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

import os
import re
from collections import OrderedDict
from io import BytesIO

import duckdb
import numpy as np

from . import lib
from .lib import decode, script, scriptLines


class SymbolInstance(object):
    def __init__(self, path, line, type=None):
        self.path = path
        self.line = line
        self.type = type

    def __repr__(self):
        type_repr = ""
        if self.type:
            type_repr = f" , type: {self.type}"

        return f"Symbol in path: {self.path}, line: {self.line}" + type_repr

    def __str__(self):
        return self.__repr__()


# Returns a Query class instance or None if project data directory does not exist
# basedir: absolute path to parent directory of all project data directories, ex. "/srv/elixir-data/"
# project: name of the project, directory in basedir, ex. "linux"
def get_query(basedir, project):
    datadir = basedir + "/" + project + "/data"
    repodir = basedir + "/" + project + "/repo"

    if not os.path.exists(datadir) or not os.path.exists(repodir):
        return None

    return Query(datadir, repodir)


class Query:
    def __init__(self, data_dir, repo_dir):
        self.repo_dir = repo_dir
        self.data_dir = data_dir
        self.dts_comp_support = int(self.script("dts-comp"))
        self.ddb = duckdb.connect(os.path.join(data_dir, "data.db"), read_only=True)
        self.file_cache = {}

    def close(self):
        self.ddb.close()

    def script(self, *args):
        return script(*args, env=self.getEnv())

    def scriptLines(self, *args):
        return scriptLines(*args, env=self.getEnv())

    def getEnv(self):
        return {
            **os.environ,
            "LXR_REPO_DIR": self.repo_dir,
            "LXR_DATA_DIR": self.data_dir,
        }

    # Check if a dts compatible string exists
    def dts_comp_exists(self, ident):
        if self.dts_comp_support:
            raise NotImplementedError
        else:
            return False

    # Returns True if file exists
    def file_exists(self, version, path):

        # TODO: make this take an array of (version, path) pairs. It is used by
        # elixir/filters/makefile*.py, they should accumulate everything and do a single
        # query.

        if version not in self.file_cache:
            versionid = self.maybe_versionname_to_versionid(version)
            if versionid is None:
                self.file_cache[version] = set()
                return False

            version_cache = set()
            rows = self.ddb.execute(
                "SELECT filepath FROM version_objects WHERE versionid = ?",
                [versionid],
            ).fetchall()
            for (filepath,) in rows:
                version_cache.add(filepath)
                version_cache.add(os.path.dirname(filepath))

            self.file_cache[version] = version_cache

        return path.strip("/") in self.file_cache[version]

    # Returns the contents of the specified file
    # Tokens are marked for further processing
    # Example: v3.1-rc10 /Makefile
    #
    # TODO: we could do better than this now. We can do a lookup by blobid for all defs/refs.
    def get_tokenized_file(self, version, path):
        filename = os.path.basename(path)
        family = lib.getFileFamily(filename)

        if family is None:
            return decode(self.script("get-file", version, path))

        even = True
        prefix = b"CONFIG_" if family == "K" else b""
        tokens = []

        for tok in self.scriptLines("tokenize-file", version, path, family):
            even = not even
            tokens.append((tok, prefix + tok, even))

        defnames = {tok2.decode() for _, tok2, even in tokens if even}
        defnames = np.unique(list(defnames))
        defs = self.ddb.sql("""SELECT DISTINCT defname FROM defs
                               WHERE defname IN (SELECT * FROM defnames)""")
        defs = set(defs.df()["defname"])

        buffer = BytesIO()
        for tok, tok2, even in tokens:
            if even and tok2.decode() in defs:
                buffer.write(b"\033[31m" + tok2 + b"\033[0m")
            else:
                buffer.write(lib.unescape(tok))
        return decode(buffer.getvalue())

    # Returns the contents (trees or blobs) of the specified directory
    # Example: v3.1-rc10 /arch
    def get_dir_contents(self, version, path):
        tag = self.version_to_tag(version)
        entries_str = decode(self.script("get-dir", tag, path))
        return entries_str.split("\n")[:-1]

    # Returns indexed versions, as a tree of OrderedDict.
    # It has a depth of 3, for example: v3 v3.1 v3.1-rc10.
    def get_versions(self):
        versions = OrderedDict()

        for _, version, _ in self.versions_cmd():
            m = re.match(r"^(v\d+)\.\d+", version)
            topmenu = m.group(1)
            submenu = m.group(0)

            if topmenu not in versions:
                versions[topmenu] = OrderedDict()
            if submenu not in versions[topmenu]:
                versions[topmenu][submenu] = []
            versions[topmenu][submenu].append(version)

        return versions

    def version_to_tag(self, version):
        # TODO: can we avoid?
        return self.ddb.execute(
            "SELECT versiontag FROM versions WHERE versionname = ?", (version,)
        ).fetchone()[0]

    # Returns the type (blob or tree) associated to
    # the given path. Example:
    # > ./query.py type v3.1-rc10 /Makefile
    # blob
    # > ./query.py type v3.1-rc10 /arch
    # tree
    def get_file_type(self, version, path):
        return decode(
            self.script("get-type", self.version_to_tag(version), path)
        ).strip()

    # Returns identifier search results
    def search_ident(self, version, ident, family):
        # DT bindings compatible strings are handled differently
        if family == "B":
            return self.get_idents_comps(version, ident)
        else:
            return self.get_idents_defs(version, ident, family)

    def versions_cmd(self):
        for line in self.scriptLines("versions"):
            line = decode(line)
            # unpack to trigger error on invalid format
            tag, version, is_rc = line.split("\t")
            yield (tag, version, bool(is_rc))

    # Returns the latest tag that is included in the database.
    # This excludes release candidates if `rc` is False.
    def get_latest_tag(self, rc=False):
        if rc:
            query = "SELECT versionname FROM versions ORDER BY versionid"
        else:
            query = "SELECT versionname FROM versions WHERE is_rc = false ORDER BY versionid"

        return self.ddb.sql(query).fetchone()[0]

    def get_file_raw(self, version, path):
        return decode(self.script("get-file", self.version_to_tag(version), path))

    def get_idents_comps(self, version, ident):
        raise NotImplementedError

    def def_exists_in_db(self, defname):
        assert isinstance(defname, str)  # bytes wouldn't work
        QUERY = "SELECT 1 FROM defs WHERE defname = ? LIMIT 1;"
        return self.ddb.execute(QUERY, (defname,)).fetchone() is not None

    def maybe_versionname_to_versionid(self, versionname):
        assert isinstance(versionname, str)  # bytes wouldn't work
        QUERY = "SELECT versionid FROM versions WHERE versionname = ?;"
        versionid = self.ddb.execute(QUERY, (versionname,)).fetchone()
        return None if versionid is None else versionid[0]

    def get_idents_defs(self, versionname, ident, blobfamily):
        if not self.def_exists_in_db(ident):
            return symbol_definitions, symbol_references, symbol_doccomments, False
        versionid = self.maybe_versionname_to_versionid(versionname)
        if versionid is None:  # version doesn't exist
            return symbol_definitions, symbol_references, symbol_doccomments, False

        # TODO: move that to a static SQL query to avoid building it as a string?
        if blobfamily == "A":
            fam_filter = ""  # no blobfamily filtering
        elif blobfamily == "D":
            # The previous behavior was different: if we searched for DTSI and the def had
            # macros defined in C code, we returned all defs. We move away from that;
            # instead we return only entries that are macros defined in C.
            fam_filter = (
                "AND (blobfamily == 'D' OR (blobfamily == 'C' AND deftype == 'macro'))"
            )
        else:
            fam_filter = "AND blobfamily == $f"

        QUERY = (
            """
        SELECT filepath, defline, deftype FROM defs
        INNER JOIN version_objects ON defs.blobid = version_objects.blobid
        WHERE defname = $d AND versionid = $v """
            + fam_filter
        )
        defs = self.ddb.execute(
            QUERY, parameters={"d": ident, "v": versionid, "f": blobfamily}
        )
        defs = [
            SymbolInstance(x.filepath, x.defline, x.deftype)
            for x in defs.df().itertuples()
        ]

        if blobfamily == "A":
            fam_filter = ""
        elif blobfamily == "C":
            fam_filter = "AND (blobfamily = $f OR blobfamily = 'K')"
        elif blobfamily in ["K", "D", "M"]:
            fam_filter = "AND blobfamily = $f"

        QUERY = (
            """
        SELECT filepath, refline FROM refs
        INNER JOIN version_objects ON refs.blobid = version_objects.blobid
        WHERE refname = $r AND versionid = $v """
            + fam_filter
        )
        refs = self.ddb.execute(
            QUERY, parameters={"r": ident, "v": versionid, "f": blobfamily}
        )
        refs = [SymbolInstance(x.filepath, x.refline) for x in refs.df().itertuples()]

        # TODO: deal with doccomments!
        # TODO: previous code sorts defs and refs, we might want to replicate that.

        return defs, refs, [], True
