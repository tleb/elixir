# conftest.py: session fixtures doing what t/TestEnvironment.pm did for the
# perl tests (build_repo, build_db, update_env), for the pytest port.
#
# The port runs the whole stack in one process where possible: query.py's
# assertions became direct elixir.query.Query calls, and the web/api tests
# drive the falcon app in-process. Only update.py stays a subprocess, since
# it is a script, not an importable module.
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

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

PROJECT = 'testproj'  # lib.currentProject() derives the project from the data dir's parent
TAG = 'v5.4'
TREE = Path(__file__).resolve().parent / 'tree'


class TestEnv:
    """Paths of one temporary project: <proj>/testproj/{repo,data}"""

    def __init__(self, proj_dir):
        self.proj_dir = str(proj_dir)
        self.repo_dir = str(proj_dir / PROJECT / 'repo')
        self.data_dir = str(proj_dir / PROJECT / 'data')

    def env(self):
        """LXR_* variables for subprocesses and the in-process app"""
        return {
            **os.environ,
            'LXR_PROJ_DIR': self.proj_dir,
            'LXR_REPO_DIR': self.repo_dir,
            'LXR_DATA_DIR': self.data_dir,
        }


def build_repo(env: TestEnv):
    """Create a git repo from t/tree, committed and tagged (build_repo in
    TestEnvironment.pm). The -c flags keep the run independent of the
    user's gitconfig."""
    repo_dir = Path(env.repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)

    git = ['git', '-C', str(repo_dir), '-c', 'init.defaultBranch=main',
           '-c', 'user.name=test@example.com', '-c', 'user.email=test']

    def run(*args):
        subprocess.run(git + list(args), check=True)

    run('init', '.')
    shutil.copytree(TREE, repo_dir, dirs_exist_ok=True)
    run('add', '.')
    run('commit', '-m', 'Initial commit')
    run('tag', TAG)


def build_db(env: TestEnv):
    """Index the repo with update.py (build_db in TestEnvironment.pm),
    through the same interpreter as the test run."""
    data_dir = Path(env.data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    # A failing run must fail the suite with its output visible
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / 'update.py')],
        env=env.env(), cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert result.returncode == 0, result.stdout
    assert any(data_dir.iterdir()), 'update.py left the data directory empty'


@pytest.fixture(scope='session')
def build_env():
    """Factory for one-off environments; tests that mutate a database use
    these instead of the shared session environment."""
    def _build(base_dir: Path) -> TestEnv:
        env = TestEnv(base_dir)
        build_repo(env)
        build_db(env)
        return env
    return _build


@pytest.fixture(scope='session')
def testenv(tmp_path_factory):
    """A fully indexed testproj (update_env in TestEnvironment.pm exported
    the LXR_* variables; the web app reads LXR_PROJ_DIR at request time)"""
    env = TestEnv(tmp_path_factory.mktemp('elixir'))
    build_repo(env)
    build_db(env)

    os.environ.update({
        'LXR_PROJ_DIR': env.proj_dir,
        'LXR_REPO_DIR': env.repo_dir,
        'LXR_DATA_DIR': env.data_dir,
    })
    return env


@pytest.fixture(scope='session')
def query(testenv):
    """A Query on the session database, like utils/query.py would create"""
    from elixir.query import Query
    q = Query(testenv.data_dir, testenv.repo_dir)
    yield q
    q.close()


@pytest.fixture(scope='session')
def client(testenv):
    """In-process falcon client for the web and API routes (replaces the
    t/web_cgi.py CGI shim the perl tests used)"""
    from falcon import testing
    from elixir.web import get_application
    return testing.TestClient(get_application())
