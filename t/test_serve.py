# Tests for `elixir serve`: the dev server wraps elixir.web.application
# in a WSGI app that injects LXR_PROJ_DIR into each request's environ --
# what Apache's SetEnv does for mod_wsgi. Tested in-process (no real
# HTTP): a minimal environ + start_response, the way wsgiref would call.
#
# This file is part of Elixir, a source code cross-referencer.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Elixir is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Elixir.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import io


def make_environ(path):
    """The WSGI environ wsgiref would hand the app for GET <path>"""
    return {
        'REQUEST_METHOD': 'GET',
        'PATH_INFO': path,
        'QUERY_STRING': '',
        'SERVER_NAME': 'localhost',
        'SERVER_PORT': '8000',
        'HTTP_HOST': 'localhost:8000',
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': 'http',
        'wsgi.input': io.BytesIO(b''),
        'wsgi.errors': io.StringIO(),
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
    }


def test_serve_wrapper_injects_lxr_proj_dir(testenv, monkeypatch):
    # Drop the os.environ fallback so only the wrapper's injection can
    # tell web.py where the projects live
    monkeypatch.delenv('LXR_PROJ_DIR', raising=False)

    from elixir.cli import make_lxr_app
    app = make_lxr_app(testenv.proj_dir)

    environ = make_environ('/testproj/v5.4/source')
    response = {}

    def start_response(status, headers, *_):
        response['status'] = status
        response['headers'] = headers

    body = b''.join(app(environ, start_response))

    assert environ['LXR_PROJ_DIR'] == testenv.proj_dir
    assert response['status'] == '200 OK'
    assert b'issue102.c' in body
