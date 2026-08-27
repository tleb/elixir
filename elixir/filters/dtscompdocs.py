import re
from urllib.parse import quote
from .utils import Filter, FilterContext

# Filter for DT compatible strings in documentation (B family) files
# syscon
# Example: linux/v6.9.4/source/Documentation/devicetree/bindings/thermal/brcm,avs-ro-thermal.yaml#L17
# Note that this also finds strings in comments, descriptions and other potentially unrelated properties
class DtsCompDocsFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
            ctx.query.dts_comp_support and \
            ctx.filepath.startswith('/Documentation/devicetree/bindings')

    def transform_raw_code(self, ctx, code: str) -> str:
        def keep_dtscompB(m):
            text = m.group(1)

            if ctx.query.dts_comp_exists(quote(text)):
                return f'__KEEPDTSCOMPB__{ self.keep(text) }'
            else:
                return m.group(0)

        return re.sub('([\w-]+,?[\w-]+)', keep_dtscompB, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_dtscompB(m):
            i = self.kept(m.group(1))

            return f'<a class="ident" href="{ ctx.get_ident_url(i, "B") }">{ i }</a>'

        return re.sub('__KEEPDTSCOMPB__([A-J]+)', replace_dtscompB, html, flags=re.MULTILINE)

