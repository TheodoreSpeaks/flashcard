# fc

Flashcards that run in your browser, served from your own machine (no internet needed). Packs are stored in Anki's collection format (SQLite) and scheduled with SM-2. Lessons are JSON files that unlock as you learn.

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
fc sync <pack>       # after git pull: update cards you already have from edited lessons
```

## Use

```sh
fc                   # opens the browser: every pack, what's due, lessons (ctrl-c to stop)
fc <pack>            # review one pack (space flips, 1-5 rates, esc goes home)
fc lesson <pack>     # read the next lesson and add its cards
fc stats <pack>      # progress and lesson unlocks
fc help              # everything else
```

## Packs

| pack | what |
|---|---|
| `bengali` | spoken Kolkata Bengali, romanized, informal (tumi) |
| `chinese` | HSK 1–3 in frequency order. Each word has a reading card (characters → pinyin/meaning) and a writing card (pinyin/meaning → write it on paper); untick words you can already read |
| `zhvocab` | adult Mandarin vocab (English → Chinese) |
| `bnread` | reading Bengali script, a few letters at a time, then words |
| `bnvocab` | adult Bengali vocab for heritage speakers (English → Bengali) |

## Layout

- `packs/<name>.json`: pack definition (fields, card layout, daily new-card limit, writing guide).
- `lessons/<name>/NN-slug.json`: lessons with `title`, `focus`, `body` (markdown) and `cards`.

A pack can define `reverse` (`front`, `back`, `prompt`) to give every word a second card. The two cards never come up on the same day, and only the main card counts toward unlocking lessons. After adding `reverse` to `packs/<pack>.json`, run `fc sync <pack>` to create the missing cards.
