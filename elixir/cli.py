#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Mikaël Bouillot <mikael.bouillot@bootlin.com>
#  and contributors.
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

# The `elixir` console command. Git-like addressing: a global -C changes
# the working directory first, then a subcommand works on a project
# living at <cwd>/<project>/{data,repo}. Adding a subcommand = one
# register_<name>(subparsers) call in main() plus its handler(s).

import argparse
import os

from elixir.query import Query

# --- query: ported from utils/query.py, same output format ---

def q_stats(q, args):
    db = q.db
    print("Versions: ", db.execute('SELECT count(*) FROM versions').fetchone()[0])
    print("Blobs: ", db.execute('SELECT count(*) FROM blobs').fetchone()[0])
    print("Definitions: ", db.execute(
        "SELECT count(DISTINCT identid) FROM defs WHERE deftype <> 'compatible'").fetchone()[0])
    print("References: ", db.execute(
        'SELECT count(DISTINCT identid) FROM refs').fetchone()[0])

def q_versions(q, args):
    for major in q.get_versions().values():
        for minor in major.values():
            for v in minor:
                print(v)

def q_ident(q, args):
    symbol_definitions, symbol_references, symbol_doccomments, _ = q.search_ident(
        args.version, args.ident, args.family)
    print("Symbol Definitions:")
    for symbol_definition in symbol_definitions:
        print(symbol_definition)

    print("\nSymbol References:")
    for symbol_reference in symbol_references:
        print(symbol_reference)

    print("\nDocumented in:")
    for symbol_doccomment in symbol_doccomments:
        print(symbol_doccomment)

def q_file(q, args):
    code = q.get_tokenized_file(args.version, args.path)
    print(code)

def run_query(args):
    project = os.path.join(os.getcwd(), args.project)
    q = Query(os.path.join(project, 'data'), os.path.join(project, 'repo'))
    args.query_cmd(q, args)

def register_query(subparsers):
    parser = subparsers.add_parser('query', help="Query an indexed project")
    parser.add_argument('project', help="The project name, under the current root")
    parser.set_defaults(handler=run_query)

    query = parser.add_subparsers(dest='query_cmd', required=True)

    subparser = query.add_parser('stats', help="Get basic database stats")
    subparser.set_defaults(query_cmd=q_stats)

    subparser = query.add_parser('versions', help="Get list of versions in the project")
    subparser.set_defaults(query_cmd=q_versions)

    subparser = query.add_parser('ident', help="Get definitions and references of an identifier")
    subparser.add_argument("version", help="The version of the project", type=str, default="latest")
    subparser.add_argument('ident', type=str, help="The name of the identifier")
    subparser.add_argument('family', type=str, help="The file family requested")
    subparser.set_defaults(query_cmd=q_ident)

    subparser = query.add_parser('file', help="Get a source file")
    subparser.add_argument("version", help="The version of the project", type=str, default="latest")
    subparser.add_argument('path', type=str, help="The path of the source file")
    subparser.set_defaults(query_cmd=q_file)

# Subcommand registry; later tasks append index/update/remote/serve
subcommands = [register_query]

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='elixir', description="Elixir, the source code cross-referencer")
    parser.add_argument('-C', '--cwd', metavar='DIR',
                        help="Change to DIR before doing anything else")
    subparsers = parser.add_subparsers(required=True)

    for register in subcommands:
        register(subparsers)

    args = parser.parse_args(argv)

    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as e:
            parser.error(f"cannot change to '{args.cwd}': {e.strerror}")

    args.handler(args)
