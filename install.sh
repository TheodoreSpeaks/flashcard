#!/usr/bin/env bash
# Set up fc: create local packs from packs/*.json and add an `fc` alias to your shell rc.
# Safe to re-run: existing packs (your review history) are never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "fc needs python 3.9+")'
chmod +x "$ROOT/fc.py"

for meta in "$ROOT"/packs/*.json; do
  name="$(basename "$meta" .json)"
  if [ -e "$ROOT/packs/$name.anki2" ]; then
    echo "pack $name already exists, keeping it"
  else
    "$ROOT/fc.py" new "$name" "$meta"
  fi
done

# `fc` is a zsh/bash builtin, so a binary on PATH would be shadowed. An alias wins.
case "$(basename "${SHELL:?SHELL is not set}")" in
  zsh) rc="$HOME/.zshrc" ;;
  bash) rc="$HOME/.bashrc" ;;
  *) echo "unsupported shell $SHELL: add  alias fc=$ROOT/fc.py  to your shell config yourself" >&2; exit 1 ;;
esac
line="alias fc=$ROOT/fc.py"
if grep -q '^alias fc=' "$rc" 2>/dev/null; then
  echo "an fc alias is already in $rc: $(grep '^alias fc=' "$rc")"
else
  printf '\n%s\n' "$line" >> "$rc"
  echo "added alias to $rc. open a new shell or run: source $rc"
fi

echo "done. start with: fc lesson <pack>   (packs: $(cd "$ROOT/packs" && ls *.json | sed 's/\.json$//' | paste -sd ' ' -))"
