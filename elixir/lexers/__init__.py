from .lexers import *

# Order and shape of the patterns follow elixir/project_utils.py:
# matched against the basename and the full path, extensions first.
default_lexers = {
    r'.*\.(c|h|cpp|hpp|c\+\+|cxx|cc)$': CLexer,
    r'.*\.s$': GasLexer,
    r'.*\.dts(i)?$': DTSLexer,
    r'kconfig.*': KconfigLexer,
    r'makefile.*': MakefileLexer,
    r'.*\.mk$': MakefileLexer,
}

