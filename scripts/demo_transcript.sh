#!/usr/bin/env sh
# The demo transcript. A shell entry point for the Python that does the work.
#
#   ./scripts/demo_transcript.sh
#   ./scripts/demo_transcript.sh --skip-mcp
#
# The body lives in scripts/demo_transcript.py deliberately: this repository is developed on Windows
# as well as POSIX, and a transcript that has to survive two shells' quoting rules is a transcript
# that eventually differs between them. One implementation, two entry points, identical output.
#
# Read-only: places, cancels and closes nothing. Sections 2 and 3 need no Alpaca credential at
# all; section 1 starts Alpaca's own MCP server and does, so use --skip-mcp without one.
set -eu
exec "${PYTHON:-python}" "$(dirname "$0")/demo_transcript.py" "$@"
