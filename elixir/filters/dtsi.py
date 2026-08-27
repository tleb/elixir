import re
from .utils import Filter, FilterContext, extension_matches

# Filters for dts includes as follows:
# Replaces include directives in dts/dtsi files with links to source
# /include/ "file"
# Example: u-boot/v2023.10/source/arch/powerpc/dts/t1023si-post.dtsi#L12
class DtsiFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
                extension_matches(ctx.filepath, {'dts', 'dtsi'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_dtsi(m):
            return f'{ m.group(1) }/include/{ m.group(2) }"__KEEPDTSI__{ self.keep(m.group(3)) }"'

        return re.sub('^(\s*)/include/(\s*)\"(.*?)\"', keep_dtsi, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_dtsi(m):
            w = self.kept(m.group(1))
            return f'<a href="{ ctx.get_relative_source_url(w) }">{ w }</a>'

        return re.sub('__KEEPDTSI__([A-J]+)', replace_dtsi, html, flags=re.MULTILINE)

