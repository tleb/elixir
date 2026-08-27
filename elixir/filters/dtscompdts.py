import re
from .utils import Filter, FilterContext, extension_matches

# Filter for DT compatible strings in DTS (D family) files
# compatible = "device"
# Example: u-boot/v2023.10/source/arch/arm/dts/ac5-98dx35xx-rd.dts#L37
class DtsCompDtsFilter(Filter):
    def check_if_applies(self, ctx) -> bool:
        return super().check_if_applies(ctx) and \
            ctx.query.dts_comp_support and \
            extension_matches(ctx.filepath, {'dts', 'dtsi'})

    def transform_raw_code(self, ctx, code: str) -> str:
        def sub_func(m):
            match = m.group(0)
            strings = re.findall("\"(.+?)\"", m.group(1))

            for string in strings:
                match = match.replace(string, '__KEEPDTSCOMPD__' + self.keep(string))

            return match

        return re.sub('\s*compatible(.*?)$', sub_func, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_dtscompD(m):
            i = self.kept(m.group(1))

            return f'<a class="ident" href="{ ctx.get_ident_url(i, "B") }">{ i }</a>'

        return re.sub('__KEEPDTSCOMPD__([A-J]+)', replace_dtscompD, html, flags=re.MULTILINE)

