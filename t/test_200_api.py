# Port of t/200-api.t: test the elixir REST API against the files in t/tree.
# 200-api.t ran `pytest t` as a subprocess to pick up t/api_test.py; that
# file's tests are folded in here and driven through the falcon test client
# in-process, like the web tests.
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



def get(client, ident, query_string):
    return client.simulate_get(f'/api/ident/testproj/{ident}',
                               query_string=query_string)


def test_identifier_not_found(client):
    result = get(client, 'SOME_NONEXISTENT_IDENTIFIER',
                 query_string='version=latest&family=C')

    assert result.status_code == 200
    assert result.json == {'definitions': [], 'references': [], 'documentations': []}


def test_missing_version(client):
    # A get request without a version query string
    result = get(client, 'of_i2c_get_board_info', query_string='')

    assert result.status_code == 400


def test_existing_identifier(client):
    expected_json = {
        'definitions': [
            {'path': 'include/linux/i2c.h', 'line': 941, 'type': 'prototype'},
            {'path': 'drivers/i2c/i2c-core-of.c', 'line': 22, 'type': 'function'},
            {'path': 'include/linux/i2c.h', 'line': 968, 'type': 'function'},
        ],
        'references': [
            {'path': 'drivers/i2c/i2c-core-of.c', 'line': '62,73', 'type': None},
        ],
        'documentations': [],
    }

    for version in ('v5.4', 'latest'):
        result = get(client, 'of_i2c_get_board_info',
                     query_string=f'version={version}&family=C')
        assert result.status_code == 200
        assert result.json == expected_json


def test_dts_identifier(client):
    result = get(client, 'led0', query_string='version=v5.4&family=D')

    assert result.status_code == 200
    assert result.json == {
        'definitions': [
            {'path': 'arch/arm/boot/dts/testproj-board.dts', 'line': 12, 'type': 'label'},
        ],
        'references': [
            {'path': 'arch/arm/boot/dts/testproj-board.dtsi', 'line': '13', 'type': None},
        ],
        'documentations': [],
    }


def test_compatible_string(client):
    # Family B: defined by .compatible = "..." in C, documented in the
    # DT bindings; note the definition line is a string here, like all
    # comps lines
    result = get(client, 'vendor,thing', query_string='version=v5.4&family=B')

    assert result.status_code == 200
    assert result.json == {
        'definitions': [
            {'path': 'drivers/i2c/i2c-boardinfo.c', 'line': '107', 'type': 'compatible'},
        ],
        'references': [],
        'documentations': [
            {'path': 'Documentation/devicetree/bindings/vendor,thing.yaml', 'line': '4,19', 'type': None},
        ],
    }
