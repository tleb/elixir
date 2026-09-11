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

# Read path over the DuckDB storage (elixir/data_duckdb.py). The API is
# the one the BDB layer served (web.py, api.py, autocomplete.py and the
# filters are its consumers); every serialization quirk of the merge-scan
# era is reproduced on purpose and commented where it hides:
#
# - defs attach to a blob's FIRST path in a version (the old merge scan
#   consumed a blob's definitions at its first PathList entry, which was
#   sorted by (blob id, path)), while refs and docs appear under EVERY
#   path the blob occurs at.
# - display family filtering follows lib.compatibleFamily /
#   compatibleMacro, which see family COMPATIBILITY, not equality (a C
#   query shows C and K references; an M query shows K references).
# - /** doc comments of one ident in one file were multiple RefList
#   entries appended in DESCENDING line order, and the scan kept only
#   the first — so the doc line shown is the HIGHEST line of the file.
# - the API/web line fields of refs and compatibles are the BDB RefList
#   comma-joined line strings; defs lines are ints (per the templates).

from .lib import decode, tokenizeFile
from . import lib
from . import repo
from . import data_duckdb as dd
import os
from collections import OrderedDict
from urllib import parse

from io import BytesIO

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
# basedir: absolute path to parent directory of all project data directories, ex: "/srv/elixir-data/"
# project: name of the project, directory in basedir, ex. "linux"
def get_query(basedir, project):
    datadir = basedir + '/' + project + '/data'
    repodir = basedir + '/' + project + '/repo'

    if not os.path.exists(datadir) or not os.path.exists(repodir):
        return None

    return Query(datadir, repodir)

class Query:
    def __init__(self, data_dir, repo_dir):
        self.repo_dir = repo_dir
        self.data_dir = data_dir
        # script.sh picked the project's plugin the same way, from the
        # repository directory's parent
        self.project = os.path.basename(os.path.dirname(repo_dir))
        self.dts_comp_support = int(self.project in repo.DTS_COMP_SUPPORT)
        self.db = dd.connect_ro(os.path.join(data_dir, 'data.duckdb'))
        self.file_cache = {}
        self._tag_cache = {}       # tag -> versionid or None
        self._tags = None          # set of the database's tags
        self._mark_sets = {}       # family -> token mark set (defs_cache-*)

    def close(self):
        self.db.close()

    def _tags_in_db(self):
        if self._tags is None:
            self._tags = set(row[0] for row in
                             self.db.execute('SELECT tag FROM versions').fetchall())
        return self._tags

    def _versionid(self, version):
        # None when the version is not in the database (the old
        # db.vers.exists)
        try:
            return self._tag_cache[version]
        except KeyError:
            row = self.db.execute(
                'SELECT versionid FROM versions WHERE tag = ?',
                [version]).fetchone()
            versionid = row[0] if row is not None else None
            self._tag_cache[version] = versionid
            return versionid

    # Check if a dts compatible string exists
    def dts_comp_exists(self, ident):
        if self.dts_comp_support:
            # ident arrives URL-quoted, the stored form (the old db.comps)
            return self.db.execute(
                "SELECT EXISTS (SELECT 1 FROM defs d JOIN idents i"
                " ON i.identid = d.identid"
                " WHERE i.name = ? AND d.deftype = 'compatible')",
                [ident]).fetchone()[0]
        else:
            return False

    # Returns True if file exists
    def file_exists(self, version, path):
        if version not in self.file_cache:
            version_cache = set()
            for (filepath,) in self.db.execute(
                    'SELECT vo.filepath FROM version_objects vo'
                    ' JOIN versions v ON v.versionid = vo.versionid'
                    ' WHERE v.tag = ?', [version]).fetchall():
                dirname, filename = os.path.split(filepath)
                version_cache.add(dirname)
                version_cache.add(filepath)

            self.file_cache[version] = version_cache

        return path.strip('/') in self.file_cache[version]

    # The defs_cache-* membership of the BDB layer: which idents have a
    # definition compatible with a file family (generate_defs_caches
    # applied lib.compatibleFamily/compatibleMacro to db.defs records).
    # Compatibles never qualified (they lived in db.comps, not db.defs).
    _DEFS_CACHE_SQL = {
        'C': "d.family IN ('C', 'K')",
        'K': "d.family = 'K'",
        'D': "(d.family = 'D' OR (d.family = 'C' AND d.deftype = 'macro'))",
        'M': "d.family = 'K'",
    }

    def _mark_set(self, family):
        marks = self._mark_sets.get(family)
        if marks is None:
            marks = set(row[0] for row in self.db.execute(
                'SELECT DISTINCT i.name FROM idents i JOIN defs d'
                ' ON d.identid = i.identid'
                " WHERE d.deftype <> 'compatible' AND "
                + self._DEFS_CACHE_SQL[family]).fetchall())
            self._mark_sets[family] = marks
        return marks

    # Returns the contents of the specified file
    # Tokens are marked for further processing
    # Example: v3.1-rc10 /Makefile
    def get_tokenized_file(self, version, path):
        filename = os.path.basename(path)
        family = lib.getFileFamily(filename)

        if family != None:
            assert family in lib.CACHED_DEFINITIONS_FAMILIES, f"family {family} must have its definitions cached"

            marks = self._mark_set(family)

            buffer = BytesIO()
            tokens = tokenizeFile(self.repo_dir, self.project, version, path, family)
            even = True

            prefix = b''
            if family == 'K':
                prefix = b'CONFIG_'

            for tok in tokens:
                even = not even
                tok2 = prefix + tok
                if even and decode(tok2) in marks:
                    tok = b'\033[31m' + tok2 + b'\033[0m'
                else:
                    tok = lib.unescape(tok)
                buffer.write(tok)
            return decode(buffer.getvalue())
        else:
            return decode(repo.get_file(self.repo_dir, self.project, version, path))

    # Returns the contents (trees or blobs) of the specified directory
    # Example: v3.1-rc10 /arch
    def get_dir_contents(self, version, path):
        entries_str = decode(repo.get_dir(self.repo_dir, self.project, version, path))
        return entries_str.split("\n")[:-1]

    # Returns indexed versions, as a tree of OrderedDict.
    # It has a depth of 3, for example: v3 v3.1 v3.1-rc10.
    def get_versions(self):
        versions = OrderedDict()

        tags_in_db = self._tags_in_db()
        for line in repo.list_tags_h(self.repo_dir, self.project):
            taginfo = decode(line).split(' ')
            num = len(taginfo)
            topmenu, submenu = 'FIXME', 'FIXME'

            if num == 1:
                tag, = taginfo
            elif num == 2:
                submenu, tag = taginfo
            elif num == 3:
                topmenu, submenu, tag = taginfo
            else:
                raise Exception("unexpected number of fields in taginfo")

            if tag in tags_in_db:
                if topmenu not in versions:
                    versions[topmenu] = OrderedDict()
                if submenu not in versions[topmenu]:
                    versions[topmenu][submenu] = []
                versions[topmenu][submenu].append(tag)

        return versions

    # Returns the type (blob or tree) associated to
    # the given path. Example:
    # > ./query.py type v3.1-rc10 /Makefile
    # blob
    # > ./query.py type v3.1-rc10 /arch
    # tree
    def get_file_type(self, version, path):
        return decode(repo.get_type(self.repo_dir, self.project, version, path)).strip()

    # Returns identifier search results
    def search_ident(self, version, ident, family):
        # DT bindings compatible strings are handled differently
        if family == 'B':
            return self.get_idents_comps(version, ident)
        else:
            return self.get_idents_defs(version, ident, family)

    # Returns the latest tag that is included in the database.
    # This excludes release candidates if `rc` is False.
    def get_latest_tag(self, rc):
        if rc:
            sorted_tags = list(reversed(repo.list_tags(self.repo_dir, self.project)))
        else:
            sorted_tags = repo.latest_tags(self.repo_dir, self.project)

        tags_in_db = self._tags_in_db()
        for tag in sorted_tags:
            if tag.decode() in tags_in_db:
                return tag.decode()

        # return the oldest tag, even if it does not exist in the database
        return sorted_tags[-1].decode()

    def get_file_raw(self, version, path):
        return decode(repo.get_file(self.repo_dir, self.project, version, path))

    # The families of ref rows a family query shows. The old call was
    # lib.compatibleFamily(family, ref_family) — arguments swapped
    # against the helper's (file_family, requested_family) signature —
    # so the effective test is: any item of the REF family's
    # compatibility list is a substring of the QUERY family char
    # (frozen upstream behavior):
    #   C query: C and K refs   K query: C, K and M refs
    #   D query: D refs         M query: no refs at all ('M' is in no
    #                            compatibility list)
    _REF_FAMS = {
        'A': None,                       # no filter
        'C': "r.family IN ('C', 'K')",
        'K': "r.family IN ('C', 'K', 'M')",
        'D': "r.family = 'D'",
        'M': 'FALSE',
    }

    def autocomplete_keys(self, ident_prefix, family):
        # The old /acp: a DB_SET_RANGE prefix scan in BDB's memcmp byte
        # order over the definitions keys (or the compatibles for family
        # B), at most 10 keys. DuckDB VARCHAR comparison is bytewise
        # (UTF-8 memcmp), which matches BDB for these ASCII keys; the
        # high-byte test in the suite pins it.
        if family == 'B':
            if not self.dts_comp_support:
                # Preserved quirk: the old data.DB had no comps database
                # for projects without dts support, and the /acp handler
                # died on query.db.comps — a 500 exactly like then.
                raise AttributeError('comps')
            process = parse.unquote
            cond = "d.deftype = 'compatible'"
        else:
            process = lambda x: x
            cond = "d.deftype <> 'compatible'"

        prefix = parse.quote(ident_prefix)
        rows = self.db.execute(
            'SELECT i.name FROM idents i WHERE starts_with(i.name, ?)'
            ' AND EXISTS (SELECT 1 FROM defs d WHERE d.identid = i.identid'
            ' AND ' + cond + ') ORDER BY i.name LIMIT 10',
            [prefix]).fetchall()
        return [process(name) for (name,) in rows]

    def get_idents_comps(self, version, ident):

        # DT bindings compatible strings are handled differently
        # They are defined in C files
        # Used in DT files
        # Documented in documentation files
        symbol_c = []
        symbol_dts = []
        symbol_docs = []

        # DT compatible strings are quoted in the database
        ident = parse.quote(ident)

        if not self.dts_comp_support:
            return symbol_c, symbol_dts, symbol_docs, False

        # db.comps.exists: compatibles are defs rows of deftype
        # 'compatible' (family C = defined in C, D = used in DT)
        row = self.db.execute(
            'SELECT i.identid FROM idents i WHERE i.name = ? AND'
            " EXISTS (SELECT 1 FROM defs d WHERE d.identid = i.identid"
            " AND d.deftype = 'compatible')", [ident]).fetchone()
        if row is None:
            return symbol_c, symbol_dts, symbol_docs, False
        identid = row[0]

        versionid = self._versionid(version)
        if versionid is None:
            # The old code called db.vers.get(version).iter() on None
            # here and died with AttributeError — same 500, kept.
            raise AttributeError(version)

        # Every path a blob occurs at lists its lines (the merge scan's
        # per-occurrence if), comma-joined like the RefList stored them
        for family, buf in (('C', symbol_c), ('D', symbol_dts)):
            rows = self.db.execute(
                "SELECT vo.filepath,"
                " string_agg(CAST(d.defline AS VARCHAR), ',' ORDER BY d.defline)"
                " FROM defs d JOIN version_objects vo"
                " ON vo.versionid = ? AND vo.blobid = d.blobid"
                " WHERE d.identid = ? AND d.deftype = 'compatible'"
                " AND d.family = '" + family + "'"
                " GROUP BY vo.blobid, vo.filepath ORDER BY vo.filepath",
                [versionid, identid]).fetchall()
            for path, lines in rows:
                if family == 'C':
                    buf.append(SymbolInstance(path, lines, 'compatible'))
                else:
                    buf.append(SymbolInstance(path, lines))

        # DT bindings documentation: docs rows of family B
        # (compatibledts_docs.db)
        rows = self.db.execute(
            "SELECT vo.filepath,"
            " string_agg(CAST(dc.line AS VARCHAR), ',' ORDER BY dc.line)"
            " FROM docs dc JOIN version_objects vo"
            ' ON vo.versionid = ? AND vo.blobid = dc.blobid'
            " WHERE dc.identid = ? AND dc.family = 'B'"
            ' GROUP BY vo.blobid, vo.filepath ORDER BY vo.filepath',
            [versionid, identid]).fetchall()
        for path, lines in rows:
            symbol_docs.append(SymbolInstance(path, lines))

        return symbol_c, symbol_dts, symbol_docs, True

    def get_idents_defs(self, version, ident, family):

        symbol_definitions = []
        symbol_references = []
        symbol_doccomments = []

        # db.defs.exists: a real definition (compatibles lived in
        # db.comps; docs-only and refs-only names were never defs keys)
        row = self.db.execute(
            'SELECT i.identid, i.macro_fams, EXISTS ('
            '  SELECT 1 FROM defs d WHERE d.identid = i.identid'
            "  AND d.deftype <> 'compatible')"
            ' FROM idents i WHERE i.name = ?', [ident]).fetchone()
        if row is None or not row[2]:
            return symbol_definitions, symbol_references, symbol_doccomments, False
        identid, macro_fams = row[0], row[1]

        versionid = self._versionid(version)
        if versionid is None:
            return symbol_definitions, symbol_references, symbol_doccomments, True

        # Which def rows a family query shows: def_family == family, or
        # any row at all when the ident has a macro the family is
        # compatible with (compatibleMacro is ident-level, not per row)
        if family == 'A':
            def_cond, def_params = 'TRUE', []
        elif family == 'D':
            has_c_macro = macro_fams is not None and bool(macro_fams & dd.FAM_BITS['C'])
            def_cond, def_params = '(d.family = ? OR ?)', ['D', has_c_macro]
        else:
            def_cond, def_params = 'd.family = ?', [family]

        # Definitions, under each blob's first path only: the PathList
        # was sorted by (blob id, path) and the merge scan consumed a
        # blob's definitions at its first entry. Ordering: type
        # reverse-alphabetical, then path, then line (dBuf.sort() plus
        # the stable type-descending sort).
        rows = self.db.execute(
            'SELECT vo.filepath, d.deftype, d.defline'
            ' FROM defs d JOIN ('
            '   SELECT blobid, min(filepath) AS filepath FROM version_objects'
            '   WHERE versionid = ? GROUP BY blobid'
            ' ) vo ON vo.blobid = d.blobid'
            ' WHERE d.identid = ? AND d.deftype <> \'compatible\''
            '   AND ' + def_cond +
            ' ORDER BY d.deftype DESC, vo.filepath, d.defline',
            [versionid, identid] + def_params).fetchall()
        for path, type, line in rows:
            symbol_definitions.append(SymbolInstance(path, line, type))

        # References, one comma-joined line string per occurrence
        ref_fams = self._REF_FAMS[family]
        rows = self.db.execute(
            'SELECT vo.filepath,'
            " string_agg(CAST(r.refline AS VARCHAR), ',' ORDER BY r.refline)"
            ' FROM refs r JOIN version_objects vo'
            ' ON vo.versionid = ? AND vo.blobid = r.blobid'
            ' WHERE r.identid = ?'
            + ('' if ref_fams is None else ' AND ' + ref_fams)
            + ' GROUP BY vo.blobid, vo.filepath ORDER BY vo.filepath',
            [versionid, identid]).fetchall()
        for path, lines in rows:
            symbol_references.append(SymbolInstance(path, lines))

        # /** doc comments (docs rows of the file's own family — B rows
        # are the bindings docs of get_idents_comps). The old RefList
        # held one entry per line in descending order and the scan kept
        # only the first: the highest line of the file is the one shown.
        rows = self.db.execute(
            'SELECT vo.filepath, CAST(max(dc.line) AS VARCHAR)'
            ' FROM docs dc JOIN version_objects vo'
            ' ON vo.versionid = ? AND vo.blobid = dc.blobid'
            " WHERE dc.identid = ? AND dc.family <> 'B'"
            ' GROUP BY vo.blobid, vo.filepath ORDER BY vo.filepath',
            [versionid, identid]).fetchall()
        for path, line in rows:
            symbol_doccomments.append(SymbolInstance(path, line))

        return symbol_definitions, symbol_references, symbol_doccomments, True
