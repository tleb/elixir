import re
from .utils import Filter, FilterContext

# Filter for kconfig identifier links
# Replaces KConfig identifiers with links to definitions and references
# `config OPTION`
# Example: u-boot/v2023.10/source/Kconfig#L17
# Note: Prepends identifier with CONFIG_
class KconfigIdentsFilter(Filter):
    def transform_raw_code(self, ctx, code: str) -> str:
      def keep_kconfigidents(m):
          return f'__KEEPKCONFIGIDENTS__{ self.keep(m.group(1)) }'

      return re.sub('\033\[31m(?=CONFIG_)(.*?)\033\[0m', keep_kconfigidents, code, flags=re.MULTILINE)

    def untransform_formatted_code(self, ctx: FilterContext, html: str) -> str:
        def replace_kconfigidents(m):
            i = self.kept(m.group(2))

            n = i
            #Remove the CONFIG_ when we are in a Kconfig file
            if ctx.family == 'K':
                n = n[7:]

            return f'{ m.group(1) or "" }<a class="ident" href="{ ctx.get_ident_url(i, "K") }">{ n }</a>'

        return re.sub('__(<.+?>)?KEEPKCONFIGIDENTS__([A-J]+)', replace_kconfigidents, html, flags=re.MULTILINE)

