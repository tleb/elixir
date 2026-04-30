#!/usr/bin/env python3
#  This file is part of Elixir, a source code cross-referencer.
#
#  Copyright (C) 2017--2020 Maxime Chretien <maxime.chretien@bootlin.com>
#                           and contributors.
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

import falcon

from .query import get_query
from .web_utils import validate_ident, validate_project


class AutocompleteResource:
    def on_get(self, req, resp):
        ident_prefix = req.get_param("q")
        project = req.get_param("p")

        ident_prefix = validate_ident(ident_prefix)
        if ident_prefix is None:
            raise falcon.HTTPInvalidParam("", "ident")

        project = validate_project(project)
        if project is None:
            raise falcon.HTTPInvalidParam("", "project")

        query = get_query(req.context.config.project_dir, project)
        if not query:
            resp.status = falcon.HTTP_NOT_FOUND
            return

        prefix = ident_prefix + "%"
        results = query.ddb.execute(
            "SELECT DISTINCT defname FROM defs WHERE defname LIKE ? ORDER BY defname LIMIT 10",
            [prefix],
        ).fetchall()

        response = [row[0] for row in results]

        resp.status = falcon.HTTP_200
        resp.content_type = falcon.MEDIA_JSON
        resp.media = response

        query.close()
