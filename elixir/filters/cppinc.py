import re
from .utils import Filter, FilterContext, extension_matches

# Filters for cpp includes like these:
# #include "file"
# Example: musl/v1.2.5/source/src/dirent/dirfd.c#L2
# #include "__dirent.h"
class CppIncFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                extension_matches(ctx.filepath, {'dts', 'dtsi', 'c', 'cc', 'cpp', 'c++', 'cxx', 'h', 's'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_cppinc(m):
            return f'{ m.group(1) }#include{ m.group(2) }"__KEEPCPPINC__{ self.keep(m.group(3)) }"'

        return re.sub('^(\s*)#include(\s*)\"(.*?)\"', keep_cppinc, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_cppinc(m):
            w = self.kept(m.group(1))
            url = ctx.get_relative_source_url(w)
            return f'<a href="{ url }">{ w }</a>'

        return re.sub('__KEEPCPPINC__([A-J]+)', replace_cppinc, html, flags=re.MULTILINE)

