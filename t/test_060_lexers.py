# Port of t/060-lexers.t: run the lexers pytest suite (elixir/lexers/tests).
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

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_lexers_suite():
    # The suite is run as a subprocess, like the perl test did, so it
    # cannot import pollution from this process (e.g. the LXR_* variables)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', 'elixir/lexers/tests'],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    if 'No module named pytest' in result.stdout:
        pytest.skip('pytest not available')
    assert result.returncode == 0, result.stdout
