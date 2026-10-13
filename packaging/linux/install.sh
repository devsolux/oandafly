#!/bin/sh
# Optional per-user menu integration for an extracted portable bundle; no sudo.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
[ -x "$here/oandafly" ] || { echo 'Keep install.sh beside the extracted oandafly executable.' >&2; exit 1; }
# Desktop-entry quoting is not shell quoting; reject characters requiring an
# ambiguous Exec/template interpretation rather than producing a broken launcher.
case "$here" in *'"'*|*'\'*|*'$'*|*'`'*|*'%'*|*'
'*) echo 'Use an extraction path without quote, backslash, dollar, backtick, percent or newline characters.' >&2; exit 1;; esac
menu="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$menu"
icon="$here/_internal/oandafly/resources/icons/oandafly-256.png"
[ -f "$icon" ] || { echo 'Bundled icon is missing.' >&2; exit 1; }
file="$menu/com.devsolux.oandafly.desktop"
[ ! -L "$file" ] || { echo 'Refusing to replace a linked desktop entry.' >&2; exit 1; }
temporary=$(mktemp "$menu/.oandafly.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
{
  printf '[Desktop Entry]\nType=Application\nName=Oandafly\n'
  printf 'Comment=Full MaleCNS policy workspace\nExec="%s/oandafly"\nIcon=%s\n' "$here" "$icon"
  printf 'Terminal=false\nCategories=Office;Finance;\nStartupNotify=true\n'
} > "$temporary"
chmod 644 "$temporary"
mv -f -- "$temporary" "$file"
printf 'Menu entry installed. Keep the portable folder at %s\n' "$here"
printf 'Remove the menu entry with: rm -- "%s"\n' "$file"
