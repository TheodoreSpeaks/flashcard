# fc

Plain-text flashcards in the terminal. Packs are stored in Anki's collection format (SQLite) and scheduled with SM-2. Lessons are JSON files that unlock as you learn.

## Install

```sh
./install.sh
```

The script asks which packs you want (or pass them: `./install.sh bengali zhvocab`) and adds `alias fc=…/fc.py` to your `~/.zshrc` or `~/.bashrc`. It needs Python 3.9+ and nothing else (stdlib only). Your review history lives in `packs/<name>.anki2`, which is gitignored.

Packs are per-machine: install only the ones you're studying. `fc` with no arguments reviews every installed pack.

```sh
fc list              # installed (✓) and available packs
fc install <pack>    # start a pack
fc uninstall <pack>  # delete a pack's cards and history (asks first)
```

## Use

```sh
fc lesson <pack>     # read the next lesson and add its cards
fc                   # review every pack (enter flips, rate 1-5)
fc stats <pack>      # progress and lesson unlocks
fc help              # everything else
```

## Packs

| pack | what |
|---|---|
| `bengali` | spoken Kolkata Bengali, romanized, informal (tumi) |
| `chinese` | reading simplified characters (characters → pinyin/meaning) |
| `zhvocab` | adult Mandarin vocab (English → Chinese) |

## Layout

- `packs/<name>.json`: pack definition (fields, card layout, daily new-card limit, writing guide).
- `lessons/<name>/NN-slug.json`: lessons with `title`, `focus`, `body` (markdown) and `cards`.
