#!/usr/bin/env perl
# t/060-lexers.t: run the lexers pytest suite (elixir/lexers/tests).
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

use 5.010_000;
use strict;
use warnings;
use FindBin '$Bin';
use lib $Bin;

use Test::More;

# The lexers come with a pytest suite. pytest is part of the Python
# environment in the Docker image (requirements.txt); on a host without
# it, skip rather than fail.

my $have_pytest = system('python3', '-m', 'pytest', '--version') == 0;

SKIP: {
    skip 'python3 -m pytest not available', 1 unless $have_pytest;

    chdir "$Bin/..";
    my $rc = system('python3', '-m', 'pytest', 'elixir/lexers/tests', '-q');
    is($rc, 0, 'lexers pytest suite passes');
}
done_testing();
