from os.path import dirname
import re
from .utils import Filter, FilterContext, filename_without_ext_matches

# Filters for Makefile file includes like these:
# dtb-y += file.dtb
# Example: u-boot/v2023.10/source/Makefile#L992
class MakefileDtbFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                filename_without_ext_matches(ctx.filepath, {'Makefile'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_makefiledtb(m):
            return f'__KEEPMAKEFILEDTB__{ self.keep(m.group(1)) }.dtb'

        return re.sub('(?<=\s)([-\w/+\.]+)\.dtb', keep_makefiledtb, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_makefiledtb(m):
            w = self.kept(m.group(1))
            filedir = dirname(ctx.filepath)

            if filedir != '/':
                filedir += '/'

            npath = f'{ filedir }{ w }.dts'
            return f'<a href="{ ctx.get_absolute_source_url(npath) }">{ w }.dtb</a>'

        return re.sub('__KEEPMAKEFILEDTB__([A-J]+)\.dtb', replace_makefiledtb, html, flags=re.MULTILINE)

