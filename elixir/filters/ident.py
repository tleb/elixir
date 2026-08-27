import re
from .utils import Filter, FilterContext

# Filter for identifier links
# Replaces identifiers marked by Query.get_tokenized_file() with links to ident page.
# If Query.get_tokenized_file() detects that a file belongs to a family that can contain
# indexed identifiers, it processes the file by adding unprintable markers
# ('\033[31m' + token + b'\033[0m') to tokens that have an entry in the definitions
# database. This filter replaces these marked tokens with links to their ident pages,
# unless the token starts with CONFIG_ - these tokens are handled by the Kconfig filter.
class IdentFilter(Filter):
    def transform_raw_code(self, ctx, code: str) -> str:
        def sub_func(m):
            return '__KEEPIDENTS__' + self.keep(m.group(1))

        return re.sub('\033\[31m(?!CONFIG_)(.*?)\033\[0m', sub_func, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def sub_func(m):
            i = self.kept(m.group(2))
            link = f'<a class="ident" href="{ ctx.get_ident_url(i) }">{ i }</a>'
            return str(m.group(1) or '') + link

        return re.sub('__(<.+?>)?KEEPIDENTS__([A-J]+)', sub_func, html, flags=re.MULTILINE)

