#!/bin/bash
# <xbar.title>Claude Usage</xbar.title>
# <xbar.version>1.0</xbar.version>
# <xbar.author>allenmervia</xbar.author>
# <xbar.author.github>allenmervia</xbar.author.github>
# <xbar.desc>5-hour and weekly usage across multiple Claude accounts, no login swap.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# Menu-bar refresh cadence is the ".5m." in this filename — rename to .1m./.10m./etc to change it.
# Needs python3 and /usr/bin/security on PATH; Homebrew python lives in /opt/homebrew/bin.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PY="$(command -v python3 || echo /usr/bin/python3)"
# Resolve the real script dir even when this file is a symlink in xbar's plugin folder.
SRC="${BASH_SOURCE[0]}"
while [ -h "$SRC" ]; do DIR="$(cd -P "$(dirname "$SRC")" && pwd)"; SRC="$(readlink "$SRC")"; [[ $SRC != /* ]] && SRC="$DIR/$SRC"; done
DIR="$(cd -P "$(dirname "$SRC")" && pwd)"
exec "$PY" "$DIR/claude-usage.py" --xbar
