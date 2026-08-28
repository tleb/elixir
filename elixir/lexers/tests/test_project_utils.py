import unittest

from elixir.project_utils import get_lexer
from elixir.lexers.lexers import CLexer, MakefileLexer, DTSLexer, GasLexer, KconfigLexer


def lexer_class(path, project):
    '''The class get_lexer() would instantiate for path, or None'''
    lexer = get_lexer(path, project)
    if lexer is None:
        return None
    return type(lexer(''))


class GetLexerTest(unittest.TestCase):
    def test_c_family(self):
        for path in ['init/main.c', 'drivers/i2c/i2c-core.h',
                     'drivers/gpu/drm/amd/foo.cpp',
                     'drivers/net/ethernet/foo.cxx']:
            self.assertIs(lexer_class(path, 'linux'), CLexer, path)

    def test_dts(self):
        for path in ['arch/arm64/boot/dts/foo.dts',
                     'arch/arm64/boot/dts/foo.dtsi',
                     'foo/bar.dtsi']:
            self.assertIs(lexer_class(path, 'linux'), DTSLexer, path)

    def test_kconfig_by_basename_anywhere(self):
        # getFileFamily() and the lexer dispatch both work on the
        # basename: Kconfig files in subdirectories must get the lexer
        for path in ['Kconfig', 'Kconfig.debug', 'drivers/Kconfig',
                     'drivers/i2c/Kconfig', 'arch/x86/Kconfig.cpu']:
            self.assertIs(lexer_class(path, 'linux'), KconfigLexer, path)

    def test_makefile_by_basename_anywhere(self):
        for path in ['Makefile', 'makefile', 'Makefile.lib',
                     'drivers/Makefile', 'scripts/Makefile.host',
                     'arch/arm/Makefile']:
            self.assertIs(lexer_class(path, 'linux'), MakefileLexer, path)

    def test_mk_files(self):
        for path in ['foo.mk', 'drivers/net/foo.mk']:
            self.assertIs(lexer_class(path, 'linux'), MakefileLexer, path)

    def test_gas_arch_selection(self):
        # .s files in arch/ subdirectories get the architecture lexer
        cases = {
            'arch/arm/kernel/head.S': 'arm32',
            'arch/x86/kernel/head_64.S': 'x86',
            'arch/sparc/lib/memset.S': 'sparc',
            'arch/m68k/ifpsp060/src/fplsp.S': 'm68k',
            'arch/sh/kernel/head_32.S': 'sh',
            'arch/um/sys-x86_64/foo.S': 'x86',
        }
        for path, arch in cases.items():
            lexer = get_lexer(path, 'linux')('')
            self.assertIsInstance(lexer, GasLexer, path)
            self.assertEqual(lexer.comment_chars,
                             GasLexer.gasm_comment_chars_map[arch], path)

    def test_gas_arch_not_confused_by_prefix(self):
        # arch/arm64 is not arch/arm
        lexer = get_lexer('arch/arm64/kernel/head.S', 'linux')('')
        self.assertEqual(lexer.comment_chars,
                         GasLexer.gasm_comment_chars_map['generic'])

    def test_gas_outside_arch(self):
        for path in ['lib/foo.S', 'mm/foo.s', 'foo/bar.S']:
            lexer = get_lexer(path, 'linux')('')
            self.assertEqual(lexer.comment_chars,
                             GasLexer.gasm_comment_chars_map['generic'], path)

    def test_uboot_project(self):
        self.assertIs(lexer_class('arch/riscv/kernel/foo.S', 'u-boot'), GasLexer)
        lexer = get_lexer('arch/riscv/kernel/foo.S', 'u-boot')('')
        self.assertEqual(lexer.comment_chars,
                         GasLexer.gasm_comment_chars_map['riscv'])
        # linux rules must not leak into other projects
        lexer = get_lexer('arch/arm/kernel/foo.S', 'u-boot')('')
        self.assertEqual(lexer.comment_chars,
                         GasLexer.gasm_comment_chars_map['arm32'])

    def test_unknown_project_uses_defaults(self):
        self.assertIs(lexer_class('init/main.c', 'musl'), CLexer)
        self.assertIs(lexer_class('drivers/Makefile', 'musl'), MakefileLexer)
        self.assertIs(lexer_class('drivers/Kconfig', 'musl'), KconfigLexer)

    def test_no_match(self):
        for path in ['README', 'Documentation/devicetree/bindings/foo.rst',
                     'foo/bar.txt', 'Kconfig.rst']:
            self.assertIsNone(get_lexer(path, 'linux'), path)

    def test_path_case_insensitive(self):
        self.assertIs(lexer_class('drivers/I2C/KCONFIG', 'linux'), KconfigLexer)
        self.assertIs(lexer_class('drivers/Makefile.LIB', 'linux'), MakefileLexer)
