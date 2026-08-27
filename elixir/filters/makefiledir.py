from os.path import dirname
import re
from .utils import Filter, FilterContext, filename_without_ext_matches

# Filters for Makefile directory includes as follows:
# obj-$(VALUE) += dir/
# Example: u-boot/v2023.10/source/Makefile#L867
class MakefileDirFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                filename_without_ext_matches(ctx.filepath, {'Makefile'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_makefiledir(m):
            filedir = dirname(ctx.filepath)

            if filedir != '/':
                filedir += '/'

            if ctx.query.file_exists(ctx.tag, filedir + m.group(1) + '/Makefile'):
                return f'__KEEPMAKEFILEDIR__{ self.keep(m.group(1)) }/{ m.group(2) }'
            else:
                return m.group(0)

        return re.sub('(?<=\s)([-\w/]+)/(\s+|$)', keep_makefiledir, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_makefiledir(m):
            w = self.kept(m.group(1))
            filedir = dirname(ctx.filepath)

            if filedir != '/':
                filedir += '/'

            fpath = f'{ filedir }{ w }/Makefile'

            return f'<a href="{ ctx.get_absolute_source_url(fpath) }">{ w }/</a>'

        return re.sub('__KEEPMAKEFILEDIR__([A-J]+)/', replace_makefiledir, html, flags=re.MULTILINE)

