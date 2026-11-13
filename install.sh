#!/usr/bin/env bash
# Set up fc: install the packs you choose (from packs/*.json) and add an `fc` alias to your shell rc.
# Safe to re-run: existing packs (your review history) are never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "fc needs python 3.9+")'
chmod +x "$ROOT/fc.py"

# Choose packs: pass names (./install.sh bengali zhvocab), or answer y/n for each.
available=()
for meta in "$ROOT"/packs/*.json; do available+=("$(basename "$meta" .json)"); done

if [ $# -gt 0 ]; then
  chosen=("$@")
else
  chosen=()
  for name in "${available[@]}"; do
    if [ -e "$ROOT/packs/$name.anki2" ]; then
      echo "$name: already installed"
      continue
    fi
    read -r -p "install $name? [y/N] " answer
    case "$answer" in y|Y|yes) chosen+=("$name") ;; esac
  done
fi

for name in "${chosen[@]+"${chosen[@]}"}"; do
  if [ -e "$ROOT/packs/$name.anki2" ]; then
    echo "$name: already installed"
  else
    "$ROOT/fc.py" install "$name"
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

echo "done. installed packs:"
"$ROOT/fc.py" list
echo "add or remove packs later with: fc install <pack> / fc uninstall <pack>"
