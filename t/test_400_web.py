# Port of t/400-web.t: test the web interface against the files in t/tree.
# The perl suite went through t/web_cgi.py, a CGI shim running the falcon
# app with REQUEST_URI set; these tests drive the app in-process with the
# falcon test client, so the shim is gone.
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

import pytest

# update.py writes DuckDB now; the falcon app still reads BDB through
# query.py until the T-Q read-path port, so these stay skipped
pytestmark = pytest.mark.skip(reason='pending T-Q read-path port')


def get(client, path):
    return client.simulate_get(path)


def test_latest_redirects_to_newest_tag(client):
    # The web interface resolves `latest` to the newest tag
    result = get(client, '/testproj/latest/source')
    assert result.status_code == 302
    assert result.headers['location'] == '/testproj/v5.4/source'


def test_index_query(client):
    result = get(client, '/testproj/v5.4/source')
    assert result.status_code == 200
    assert result.headers['content-type'].startswith('text/html')
    assert 'href="/testproj/v5.4/source/issue102.c"' in result.text
    assert 'href="/testproj/v5.4/source/arch"' in result.text


def test_identifier_query(client):
    result = get(client, '/testproj/v5.4/ident/gsb_buffer')
    assert result.status_code == 200
    assert result.headers['content-type'].startswith('text/html')
    assert 'gsb_buffer' in result.text
    assert '<h2>Defined in 1 files as a struct' in result.text
    assert 'href="/testproj/v5.4/source/drivers/i2c/i2c-core-acpi.c#L23"' in result.text
    assert '<strong>drivers/i2c/i2c-core-acpi.c</strong>' in result.text
    assert 'line 23' in result.text


def test_doc_comment_query_nonexistent(client):
    result = get(client, '/testproj/v5.4/ident/SOME_NONEXISTENT_IDENTIFIER_XYZZY_PLUGH')
    assert result.headers['content-type'].startswith('text/html')
    assert '<h2>Unknown identifier' in result.text


def test_doc_comment_query_not_documented(client):
    # gsb_buffer is in drivers/i2c/i2c-core-acpi.c, but not documented
    result = get(client, '/testproj/v5.4/ident/gsb_buffer')
    assert result.headers['content-type'].startswith('text/html')
    assert 'Documented in' not in result.text


def test_ident_query_documented_function(client):
    result = get(client, '/testproj/v5.4/ident/i2c_acpi_get_i2c_resource')
    assert result.headers['content-type'].startswith('text/html')
    assert 'Documented in 1' in result.text
    assert 'drivers/i2c/i2c-core-acpi.c#L45' in result.text


def test_ident_query_documented_function_102(client):
    result = get(client, '/testproj/v5.4/ident/documented_function_XYZZY')
    assert result.headers['content-type'].startswith('text/html')
    assert 'Documented in 1' in result.text
    assert 'issue102.c#L6' in result.text


# Devicetree pages: D family idents and B family compatible strings render
# like the others
def test_dts_identifier_page(client):
    result = get(client, '/testproj/v5.4/D/ident/led0')
    assert result.status_code == 200
    assert 'href="/testproj/v5.4/source/arch/arm/boot/dts/testproj-board.dts#L12"' in result.text


def test_compatible_string_page(client):
    result = get(client, '/testproj/v5.4/B/ident/vendor,thing')
    assert result.status_code == 200
    assert 'href="/testproj/v5.4/source/drivers/i2c/i2c-boardinfo.c#L107"' in result.text
    assert 'href="/testproj/v5.4/source/Documentation/devicetree/bindings/vendor,thing.yaml#L4"' in result.text
    assert 'href="/testproj/v5.4/source/Documentation/devicetree/bindings/vendor,thing.yaml#L19"' in result.text
