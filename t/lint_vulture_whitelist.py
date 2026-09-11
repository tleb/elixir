# Vulture whitelist: names vulture cannot see being used.
#
# Run from the repo root:
#   vulture elixir wsgi.py t utils t/lint_vulture_whitelist.py
#
# Every entry here is either a framework hook (called by falcon or the
# lexer machinery, not by us) or a deliberately-kept URL-shape
# parameter. Anything NOT covered here that vulture reports is dead
# code and gets deleted, not whitelisted.

# falcon resource handlers and middleware hooks
on_post
process_request
convert

# falcon Response attributes we set for the framework to act on
content_type
location
media
downloadable_as
cache_control

# the WSGI entry point (imported by the server)
application

# pygments lexer option (assigned on the lexer object)
stripnl

# falcon route template field: accepted to document the URL shape,
# matched by name, must keep its exact spelling
subcmd
