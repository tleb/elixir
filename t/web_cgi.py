#!/usr/bin/env python3

# Run the Elixir web application as a CGI script, for the test suite.
#
# This file is part of Elixir, a source code cross-referencer.
#
# Elixir is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# The test harness (t/TestEnvironment.pm) runs the web interface as a
# program, giving it the request URL through the REQUEST_URI environment
# variable and reading a CGI response (headers + body) from stdout, like
# the CGI front end Elixir used to have. The web interface is now a WSGI
# (Falcon) application, so this script adapts one to the other.

import os
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wsgiref.handlers import CGIHandler

from elixir.web import application

# REQUEST_URI (Apache-ism) holds the raw, still-quoted request URL, as
# expected by elixir/web.py. Build the standard CGI variables Falcon
# routes on from it.
parts = urlsplit(os.environ.get('REQUEST_URI', '/'))

os.environ['REQUEST_METHOD'] = 'GET'
os.environ['PATH_INFO'] = unquote(parts.path)
os.environ['QUERY_STRING'] = parts.query

CGIHandler().run(application)
