import re
from .utils import Filter, FilterContext, filename_without_ext_matches

# Filters for Kconfig includes
# Replaces KConfig includes (source keyword) with links to included files
# `source "path/file"`
# Example: u-boot/v2023.10/source/Kconfig#L10
class KconfigFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                filename_without_ext_matches(ctx.filepath, {'Kconfig'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_kconfig(m):
            return f'{ m.group(1) }{ m.group(2) }{ m.group(3) }"__KEEPKCONFIG__{ self.keep(m.group(4)) }"'

        return re.sub('^(\s*)(source)(\s*)\"([\w/_\.-]+)\"', keep_kconfig, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_kconfig(m):
            w = self.kept(m.group(1))
            return f'<a href="{ ctx.get_absolute_source_url(w) }">{ w }</a>'

        return re.sub('__KEEPKCONFIG__([A-J]+)', replace_kconfig, html, flags=re.MULTILINE)

