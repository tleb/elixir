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
import sys

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

# --- update: ported from update.py, now elixir.update.run() ---

def run_update(args):
    # Deferred: update pulls pyarrow and the multiprocessing machinery
    # in, which the serving subcommands should not pay for
    from elixir import update

    failed = []
    for name in args.projects:
        project = os.path.join(os.getcwd(), name)
        try:
            update.run(os.path.join(project, 'repo'), os.path.join(project, 'data'))
        except update.UpdateError as e:
            print('elixir: update %s: %s' % (name, e), file=sys.stderr)
            failed.append(name)
    if failed:
        raise SystemExit(1)

def register_update(subparsers):
    parser = subparsers.add_parser('update', help="Index the new tags of projects")
    parser.add_argument('projects', nargs='+', metavar='project',
                        help="Project name(s), under the current root")
    parser.set_defaults(handler=run_update)

# --- index: ported from utils/index, now elixir.index ---

def run_index(args):
    # Deferred, like update: index pulls the whole update pipeline in
    from elixir import index

    if index.run(os.getcwd(), None if args.all else args.projects):
        raise SystemExit(1)

def register_index(subparsers):
    parser = subparsers.add_parser('index', help="Create and index projects")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('projects', nargs='*', metavar='project',
                       help="Project name(s), under the current root")
    group.add_argument('--all', action='store_true',
                       help="Bootstrap the known projects, then index every "
                            "project under the current root")
    parser.set_defaults(handler=run_index)

# --- remote: the explicit way to give a project extra remotes ---

def run_remote_add(args):
    from elixir import index

    proj_dir = os.path.join(os.getcwd(), args.project)
    index.project_init(proj_dir)
    index.project_add_remote(proj_dir, args.url)

def register_remote(subparsers):
    parser = subparsers.add_parser('remote', help="Manage project remotes")
    sub = parser.add_subparsers(required=True)

    subparser = sub.add_parser('add', help="Add a remote to a project's repository")
    subparser.add_argument('project', help="Project name, under the current root")
    subparser.add_argument('url', help="The remote's fetch URL")
    subparser.set_defaults(handler=run_remote_add)

# --- serve: development server for elixir.web ---

def make_lxr_app(proj_dir):
    """Wrap elixir.web.application in a WSGI app that injects
    LXR_PROJ_DIR=proj_dir into every request's environ -- what Apache's
    SetEnv does for mod_wsgi (see docker/000-default.conf)."""
    from elixir.web import application  # deferred: pulls falcon & jinja2

    def wsgi_app(environ, start_response):
        environ['LXR_PROJ_DIR'] = proj_dir
        return application(environ, start_response)

    return wsgi_app

def run_serve(args):
    from wsgiref.simple_server import make_server

    proj_dir = os.getcwd()
    httpd = make_server(args.host, args.port, make_lxr_app(proj_dir))
    print(f"Serving http://{args.host}:{httpd.server_port} "
          f"(projects under {proj_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()  # newline after the terminal's ^C
    finally:
        httpd.server_close()

def register_serve(subparsers):
    parser = subparsers.add_parser('serve', help="Serve the web interface "
                                    "(development server)")
    parser.add_argument('--host', default='127.0.0.1',
                        help="Interface to bind (default: %(default)s)")
    parser.add_argument('--port', type=int, default=8000,
                        help="Port to bind (default: %(default)s)")
    parser.set_defaults(handler=run_serve)

# Subcommand registry; later tasks append serve
subcommands = [register_query, register_update, register_index, register_remote,
               register_serve]

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
