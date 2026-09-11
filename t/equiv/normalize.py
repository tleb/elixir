#!/usr/bin/env python3

#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2025 Mikaël Bouillot <mikael.bouillot@bootlin.com>
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
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The exactly-two normalizations of the equivalence harness (R3B §3).

Byte-exact comparison after dropping the HTTP Date header and scrubbing
the error-page timestamp. These are the only run-to-run variances in the
whole app [R3B, measured]; anything else that varies is a harness bug to
fix here (and to document), never a reason to widen the diff.
"""

import re

# (a) The HTTP Date header (the only header-level variance; falcon's
# TestClient does not even set one, real servers do)
DATE_HEADER = 'date'

# (b) generate_error_details() stamps datetime.now() into every error
# page, twice: plain in the <pre> block and parse.quote()d in the
# bug-report URL. str(datetime) omits the microseconds when they are 0,
# so the fraction is optional.
#   Request date: 2025-09-10 12:34:56.789012
_TS_PLAIN = rb'Request date: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?'
_TS_PLAIN_REPL = b'Request date: <TS>'
#   Request%20date%3A%202025-09-10%2012%3A34%3A56.789012
_TS_QUOTED = (rb'Request%20date%3A%20'
              rb'\d{4}-\d{2}-\d{2}'
              rb'%20\d{2}%3A\d{2}%3A\d{2}'
              rb'(?:\.\d{1,6})?')
_TS_QUOTED_REPL = b'Request%20date%3A%20<TS>'

_plain_re = re.compile(_TS_PLAIN)
_quoted_re = re.compile(_TS_QUOTED)


def normalize_headers(headers: dict) -> dict:
    """Response headers minus Date, names lowercased, deterministic order"""
    return {k.lower(): v for k, v in sorted(headers.items())
            if k.lower() != DATE_HEADER}


def normalize_body(body: bytes) -> bytes:
    """Scrub the two timestamp forms; byte-exact otherwise"""
    body = _quoted_re.sub(_TS_QUOTED_REPL, body)
    body = _plain_re.sub(_TS_PLAIN_REPL, body)
    return body
