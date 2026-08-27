import re
from .utils import Filter, FilterContext, filename_without_ext_matches

# Filters for Config.in includes
# source "path/file"
# Example: uclibc-ng/v1.0.47/source/extra/Configs/Config.in#L176
class ConfigInFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                filename_without_ext_matches(ctx.filepath, {'Config'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_configin(m):
            return f'{ m.group(1) }{ m.group(2) }{ m.group(3) }"__KEEPCONFIGIN__{ self.keep(m.group(4)) }"'

        return re.sub('^(\s*)(source)(\s*)\"(.*)\"', keep_configin, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_configin(m):
            w = self.kept(m.group(1))
            return f'<a href="{ ctx.get_absolute_source_url(w) }">{ w }</a>'

        return re.sub('__KEEPCONFIGIN__([A-J]+)', replace_configin, html, flags=re.MULTILINE)

