from .utils import regex_concat, regex_or

# Regexes shared between lexers

whitespace = r'\s+'

# Building block for comments that start with a character and go until the end of the line.
#
# The body alternation looks ambiguous (a backslash matches both branches),
# but it cannot blow up: every branch taken at a backslash either ends the
# match at the very next \n (the pattern's terminator) or dies there, and the
# whole match can only fail when no \n lies ahead — in which case the
# continuation branch never applies either.  No input puts these choices on
# a failing path, so backtracking stays linear.  Same for the body of
# c_preproc_warning_and_error below.
singleline_comment_with_escapes_base = r'(\\\s*\n|[^\n])*\n'

# A comment body character is either not a '*' or a '*' not followed by '/':
# the alternatives are disjoint, so each position has exactly one way to be
# consumed and the scan is linear.  The loop still cannot cross a '*/', so
# it stops at the FIRST terminator, same language and same leftmost match as
# the lazy original.
#
# The previous body, (.|\s)*?, overlapped on every space and tab ('.' and
# '\s' both match them): duplicate decompositions of the same span made
# backtracking exponential on unterminated comments — doubling the work per
# ambiguous whitespace character.  linux 2.1.25 drivers/net/cs89x0.c ends in
# a ~30-space unterminated /* trailer: >10 minutes with the old pattern
# (wedging the indexer at tag 486), ~0.2ms for the whole file with this one.
slash_star_multline_comment = r'/\*(?:[^*]|\*(?!/))*?\*/'
double_slash_singleline_comment = r'//' + singleline_comment_with_escapes_base
common_slash_comment = regex_or(slash_star_multline_comment, double_slash_singleline_comment)

common_decimal_integer = r'[0-9][0-9\']*'
common_hexidecimal_integer = r'0[xX][0-9a-fA-F][0-9a-fA-F\']*'
common_octal_integer = r'0[0-7][0-7\']*'
common_binary_integer = r'0[bB][01][01\']*'

c_preproc_include = r'#\s*include\s*(<.*?>|".*?")'
# match warning and error directives with the error string
# Body as safe as singleline_comment_with_escapes_base above: only the
# backslash branches overlap, and none of them can sit on a failing path.
c_preproc_warning_and_error = r'#\s*(warning|error)\s(\\\s*\n|[^\n])*\n'
# match other preprocessor directives, but don't consume the whole line
c_preproc_other = r'#\s*[a-z]+'
c_preproc_ignore = regex_or(c_preproc_include, c_preproc_warning_and_error, c_preproc_other)

# A string body unit is one plain character, an escaped character (backslash
# plus one non-newline character), or a backslash line continuation.  The
# continuation is kept verbatim: \s also matches newlines, so it swallows
# backslash + any whitespace run + newline, runs of several newlines
# included — not expressible with the other units.  Only the old third unit
# was ambiguous: \\(.|\s) overlapped the continuation on \<newline> (same
# span, two decompositions) and itself ('.' and '\s' both match horizontal
# whitespace), the same exponential-backtracking disease as the comment
# regex above — an unterminated string of "\ <space>" escapes doubled the
# work per space.  Every remaining choice point resolves within one unit:
# the escape branch at \<horizontal ws> dies at the next newline, which
# only the continuation can cross.  Same language, same shortest (lazy)
# match: \\[^\n] plus \\\s*\n together consume exactly what
# \\[^\n], \\\n and \\(.|\s) did.
double_quote_string_with_escapes = r'"(?:[^\\"\n]|\\[^\n]|\\\s*\n)*?"'
single_quote_string_with_escapes = r"'(?:[^\\'\n]|\\[^\n]|\\\s*\n)*?'"

common_string_and_char = regex_or(double_quote_string_with_escapes, single_quote_string_with_escapes)

c_exponent = r'([eE][+-]?[0-9][0-9\']*)'
c_hexidecimal_exponent = r'([pP][+-]?[0-9][0-9\']*)'

c_decimal_double_part = r'\.[0-9\']*' + c_exponent + '?'
c_octal_double_part = r'\.[0-7\']*' + c_exponent + '?'
c_hexidecimal_double_part = r'\.[0-9a-fA-F\']*' + c_hexidecimal_exponent  + '?'

c_decimal = f'{ common_decimal_integer }({ c_decimal_double_part })?'
c_hexidecimal = f'{ common_hexidecimal_integer }({ c_hexidecimal_double_part })?'
c_octal = f'{ common_octal_integer }({ c_octal_double_part  })?'

# not entirely correct... accepts way more than the standard allows
c_number_suffix = r'([uU]|[lL]|(wb|WB)|[fF]|[zZ]){0,5}'

c_number = regex_concat(regex_or(c_hexidecimal, common_binary_integer, c_decimal, c_octal), c_number_suffix)

