from .lexers import CLexer as CLexer
from .lexers import DTSLexer as DTSLexer
from .lexers import GasLexer as GasLexer
from .lexers import KconfigLexer as KconfigLexer
from .lexers import MakefileLexer as MakefileLexer
from .lexers import TokenType as TokenType

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

