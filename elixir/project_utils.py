import os
import re
from collections import OrderedDict

from . import lib
from .lexers import default_lexers
from .lexers.lexers import CLexer, MakefileLexer, DTSLexer, GasLexer, KconfigLexer

# Per-project lexer configuration, keyed by project name.
# Projects not present in this dictionary only use default_lexers.
#
# Patterns are matched, in order, against the file basename and against
# the full repository-relative path (no leading slash), lowercased.
# Patterns containing a slash (the architecture-specific gas rules
# below) therefore only ever match on the path. The rule order mirrors
# lib.getFileFamily(): extensions first, then the kconfig and makefile
# name prefixes. Files whose basename has no family at all (per
# lib.getFileFamily) are never passed here.
project_lexers = {
    'linux': OrderedDict({
        r'.*\.(c|h|cpp|hpp|c\+\+|cxx|cc)$': CLexer,
        r'.*\.dts(i)?$': DTSLexer,

        # .s/.S files belong to the C family; the line comment
        # character depends on the architecture
        r'arch/alpha/.*\.s$': (GasLexer, {"arch": "alpha"}),
        r'arch/arc/.*\.s$': (GasLexer, {"arch": "arc"}),
        r'arch/arm/.*\.s$': (GasLexer, {"arch": "arm32"}),
        r'arch/csky/.*\.s$': (GasLexer, {"arch": "csky"}),
        r'arch/m68k/.*\.s$': (GasLexer, {"arch": "m68k"}),
        r'arch/microblaze/.*\.s$': (GasLexer, {"arch": "microblaze"}),
        r'arch/mips/.*\.s$': (GasLexer, {"arch": "mips"}),
        r'arch/openrisc/.*\.s$': (GasLexer, {"arch": "openrisc"}),
        r'arch/parisc/.*\.s$': (GasLexer, {"arch": "parisc"}),
        r'arch/s390/.*\.s$': (GasLexer, {"arch": "s390"}),
        r'arch/sh/.*\.s$': (GasLexer, {"arch": "sh"}),
        r'arch/sparc/.*\.s$': (GasLexer, {"arch": "sparc"}),
        r'arch/um/.*\.s$': (GasLexer, {"arch": "x86"}),
        r'arch/x86/.*\.s$': (GasLexer, {"arch": "x86"}),
        r'arch/xtensa/.*\.s$': (GasLexer, {"arch": "xtensa"}),
        r'.*\.s$': GasLexer,

        r'kconfig.*': KconfigLexer,
        r'makefile.*': MakefileLexer,
        r'.*\.mk$': MakefileLexer,
    }),
    'u-boot': OrderedDict({
        r'.*\.(c|h|cpp|hpp|c\+\+|cxx|cc)$': CLexer,
        r'.*\.dts(i)?$': DTSLexer,

        r'arch/arc/.*\.s$': (GasLexer, {"arch": "arc"}),
        r'arch/arm/.*\.s$': (GasLexer, {"arch": "arm32"}),
        r'arch/microblaze/.*\.s$': (GasLexer, {"arch": "microblaze"}),
        r'arch/mips/.*\.s$': (GasLexer, {"arch": "mips"}),
        r'arch/riscv/.*\.s$': (GasLexer, {"arch": "riscv"}),
        r'arch/sh/.*\.s$': (GasLexer, {"arch": "sh"}),
        r'arch/x86/.*\.s$': (GasLexer, {"arch": "x86"}),
        r'arch/sandbox/.*\.s$': (GasLexer, {"arch": "x86"}),
        r'arch/xtensa/.*\.s$': (GasLexer, {"arch": "xtensa"}),
        r'.*\.s$': GasLexer,

        r'kconfig.*': KconfigLexer,
        r'makefile.*': MakefileLexer,
        r'.*\.mk$': MakefileLexer,
    }),
}

# Returns the lexer a path resolves to, or None if no lexer matches:
# the (class or (class, kwargs)) table entry itself, so callers can
# dispatch on it. Only files that have a family per lib.getFileFamily
# (basename) can get a lexer; the kconfig/makefile name rules in the
# tables above therefore never see .rst files, which getFileFamily
# excludes.
def get_lexer_class(path: str, project_name: str):
    if lib.getFileFamily(os.path.basename(path)) is None:
        return None

    lexers = project_lexers.get(project_name, default_lexers)

    path = path.lower()
    name = path.rsplit('/', 1)[-1]
    for regex, lexer in lexers.items():
        if re.match(regex, name) or re.match(regex, path):
            return lexer
    return None

# Returns a lexer for given file path under a given project, or None
# if no lexer matches.
def get_lexer(path: str, project_name: str):
    lexer = get_lexer_class(path, project_name)
    if lexer is None:
        return None
    if type(lexer) == tuple:
        lexer_cls, kwargs = lexer
        return lambda code: lexer_cls(code, **kwargs)
    return lambda code: lexer(code)
