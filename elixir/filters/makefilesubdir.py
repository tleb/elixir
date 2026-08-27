from os.path import dirname
import re
from .utils import Filter, FilterContext, filename_without_ext_matches

# Filters for Makefile directory includes as follows:
# subdir-y += dir
# Example: u-boot/v2023.10/source/examples/Makefile#L9
class MakefileSubdirFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
            filename_without_ext_matches(ctx.filepath, {'Makefile'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_makefilesubdir(m):
            n = self.keep(m.group(5))
            return f'{ m.group(1) }{ m.group(2) }{ m.group(3) }{ m.group(4) }__KEEPMAKESUBDIR__{ n }{ m.group(6) }'

        return re.sub('(subdir-y)(\s+)(\+=|:=)(\s+)([-\w]+)(\s*|$)', keep_makefilesubdir, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_makefilesubdir(m):
            w = self.kept(m.group(1))
            filedir = dirname(ctx.filepath)

            if filedir != '/':
                filedir += '/'

            npath = f'{ filedir }{ w }/Makefile'
            return f'<a href="{ ctx.get_absolute_source_url(npath) }">{ w }</a>'

        return re.sub('__KEEPMAKESUBDIR__([A-J]+)', replace_makefilesubdir, html, flags=re.MULTILINE)

