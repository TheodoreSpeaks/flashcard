#!/usr/bin/env python3
"""fc: plain-text spaced-repetition flashcards.

Each pack is a SQLite file in packs/ using Anki's collection schema
(col / notes / cards / revlog / graves). Pack metadata lives in col.models
(note type: fields, front, back) and col.decks (deck: settings, writing guide,
lesson state). Scheduling is SM-2 with a 1-5 familiarity score.

Lessons: lessons/<pack>/NN-slug.json files, written ahead of time. Each holds a
title, a markdown body and its cards; cards added from lesson N are tagged
unit::N. Lesson N+1 unlocks when 80% of lesson N's cards are learned (6+ day
interval), with at most 2 unmastered lessons open; a lesson is mastered (✓)
when 80% of its cards reach 7+ days. `fc unlock` opens lessons early.

fc never generates content itself: cards and lessons are written elsewhere
(e.g. by Claude) and brought in with `fc import` or as lesson files.
"""

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import readline  # noqa: F401  (line editing for input())

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "packs"
LESSONS_DIR = ROOT / "lessons"
COMMANDS = {"new", "edit", "lesson", "unlock", "import", "cards", "add", "stats", "list", "help"}
DAY_ROLLOVER_HOURS = 4  # like Anki: a "day" starts at 4am
REQUEUE_GAP = 5  # failed cards come back this many cards later
KNOWN_IVL = 21  # cards added with --known start as mature reviews
LEARNED_IVL = 6  # a card counts as learned once it reaches SM-2's second step (right a day after learning)
MASTERY_IVL = 7  # a card counts as mastered at this interval (days): right after a ~week gap
UNIT_RATIO = 0.8  # share of a lesson's cards that must be learned (to unlock the next) or mastered (✓)
MAX_OPEN_UNITS = 2  # unmastered lessons allowed open at once, so new material can't race ahead

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"

SCHEMA = """
CREATE TABLE col (
    id integer primary key, crt integer not null, mod integer not null,
    scm integer not null, ver integer not null, dty integer not null,
    usn integer not null, ls integer not null, conf text not null,
    models text not null, decks text not null, dconf text not null, tags text not null
);
CREATE TABLE notes (
    id integer primary key, guid text not null, mid integer not null,
    mod integer not null, usn integer not null, tags text not null,
    flds text not null, sfld text not null, csum integer not null,
    flags integer not null, data text not null
);
CREATE TABLE cards (
    id integer primary key, nid integer not null, did integer not null,
    ord integer not null, mod integer not null, usn integer not null,
    type integer not null, queue integer not null, due integer not null,
    ivl integer not null, factor integer not null, reps integer not null,
    lapses integer not null, left integer not null, odue integer not null,
    odid integer not null, flags integer not null, data text not null
);
CREATE TABLE revlog (
    id integer primary key, cid integer not null, usn integer not null,
    ease integer not null, ivl integer not null, lastIvl integer not null,
    factor integer not null, time integer not null, type integer not null
);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE INDEX ix_notes_csum on notes (csum);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
"""

# card.type / card.queue
NEW, REVIEW = 0, 2
# revlog.type
LOG_LEARN, LOG_REVIEW, LOG_RELEARN = 0, 1, 2

TEMPLATE_META = {
    "name": "",
    "fields": ["Front", "Back", "Notes"],
    "front": ["Front"],
    "back": ["Back", "Notes"],
    "new_per_day": 20,
    "guide": {
        "instructions": "What this pack is for and how its cards should be written.",
        "level": "beginner",
    },
    "unlocked_through": 1,
    "opened": [],
}


# ---------- time ----------

def day_of(ts: float) -> date:
    return (datetime.fromtimestamp(ts) - timedelta(hours=DAY_ROLLOVER_HOURS)).date()


def today_num(crt: int) -> int:
    return (day_of(time.time()) - day_of(crt)).days


def today_start_ms() -> int:
    d = day_of(time.time())
    start = datetime(d.year, d.month, d.day) + timedelta(hours=DAY_ROLLOVER_HOURS)
    return int(start.timestamp() * 1000)


_last_id = 0


def new_id() -> int:
    """Anki-style millisecond-timestamp ids, made unique within the process."""
    global _last_id
    _last_id = max(int(time.time() * 1000), _last_id + 1)
    return _last_id


# ---------- pack storage ----------

def pack_path(name: str) -> Path:
    return PACKS_DIR / f"{name}.anki2"


def open_pack(name: str) -> sqlite3.Connection:
    path = pack_path(name)
    if not path.exists():
        raise SystemExit(f"no pack '{name}' at {path}. create it with: fc new {name}")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def validate_meta(meta: dict) -> None:
    expected = {"name", "fields", "front", "back", "new_per_day", "guide", "unlocked_through", "opened"}
    if set(meta) != expected:
        raise SystemExit(f"pack metadata keys must be {sorted(expected)}, got {sorted(meta)}")
    fields = meta["fields"]
    if len(fields) < 2 or len(set(fields)) != len(fields):
        raise SystemExit("'fields' needs at least 2 unique names")
    for side in ("front", "back"):
        if not meta[side]:
            raise SystemExit(f"'{side}' must list at least one field")
        unknown = set(meta[side]) - set(fields)
        if unknown:
            raise SystemExit(f"'{side}' references unknown fields: {sorted(unknown)}")
    if set(meta["guide"]) != {"instructions", "level"}:
        raise SystemExit("'guide' must have exactly 'instructions' and 'level'")
    if not isinstance(meta["unlocked_through"], int) or meta["unlocked_through"] < 1:
        raise SystemExit("'unlocked_through' must be an integer ≥ 1")
    if not all(isinstance(n, int) for n in meta["opened"]):
        raise SystemExit("'opened' must be a list of lesson numbers")


def read_meta(db: sqlite3.Connection) -> dict:
    row = db.execute("select models, decks from col").fetchone()
    (model,) = json.loads(row["models"]).values()
    (deck,) = json.loads(row["decks"]).values()
    return {
        "name": deck["name"],
        "fields": [f["name"] for f in sorted(model["flds"], key=lambda f: f["ord"])],
        "front": model["front"],
        "back": model["back"],
        "new_per_day": deck["new_per_day"],
        "guide": deck["guide"],
        "unlocked_through": deck["unlocked_through"],
        "opened": deck["opened"],
        "_mid": model["id"],
        "_did": deck["id"],
    }


def write_meta(db: sqlite3.Connection, meta: dict, mid: int, did: int) -> None:
    validate_meta(meta)
    model = {
        "id": mid,
        "name": meta["name"],
        "flds": [{"name": f, "ord": i} for i, f in enumerate(meta["fields"])],
        "sortf": meta["fields"].index(meta["front"][0]),
        "front": meta["front"],
        "back": meta["back"],
    }
    deck = {
        "id": did,
        "name": meta["name"],
        "new_per_day": meta["new_per_day"],
        "guide": meta["guide"],
        "unlocked_through": meta["unlocked_through"],
        "opened": sorted(set(meta["opened"])),
    }
    db.execute(
        "update col set models = ?, decks = ?, mod = ?",
        (json.dumps({str(mid): model}), json.dumps({str(did): deck}), int(time.time())),
    )


def public_meta(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def save_meta(db: sqlite3.Connection, meta: dict) -> None:
    write_meta(db, public_meta(meta), meta["_mid"], meta["_did"])
    db.commit()


def create_pack(name: str, meta: dict) -> None:
    path = pack_path(name)
    if path.exists():
        raise SystemExit(f"pack '{name}' already exists at {path}")
    meta = {**meta, "name": name}
    validate_meta(meta)
    PACKS_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    now = int(time.time())
    db.execute(
        "insert into col values (1, ?, ?, ?, 11, 0, 0, 0, '{}', '{}', '{}', '{}', '{}')",
        (now, now, now * 1000),
    )
    write_meta(db, meta, mid=new_id(), did=new_id())
    db.commit()
    db.close()


def edit_json_in_editor(data: dict) -> dict:
    editor = os.environ.get("EDITOR")
    if not editor:
        raise SystemExit("set $EDITOR to edit pack metadata")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    subprocess.run([*editor.split(), tmp], check=True)
    text = Path(tmp).read_text()
    os.unlink(tmp)
    return json.loads(text)


def read_json_arg(source: str):
    """Load JSON from a file path, or from stdin when source is '-'."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text()
    return json.loads(text)


# ---------- notes & cards ----------

def field_checksum(text: str) -> int:
    return int(hashlib.sha1(text.strip().encode()).hexdigest()[:8], 16)


def norm(text: str) -> str:
    """Comparison key for duplicate detection: lowercase, no (hints), no punctuation."""
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    text = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text)
    return " ".join(text.split())


def dup_key(meta: dict, values: dict) -> tuple[str, str]:
    """A card is a duplicate when both its prompt and its answer match an existing card."""
    return norm(values[meta["front"][0]]), norm(values[meta["back"][0]])


def note_tags(tags: str) -> list[str]:
    return tags.split()


def unit_of(tags: str) -> int | None:
    units = [int(t.split("::")[1]) for t in note_tags(tags) if t.startswith("unit::")]
    if len(units) > 1:
        raise SystemExit(f"note tagged with several units: {tags!r}")
    return units[0] if units else None


def validate_card(meta: dict, card: dict, where: str) -> None:
    if not isinstance(card, dict) or set(card) != set(meta["fields"]):
        got = sorted(card) if isinstance(card, dict) else type(card).__name__
        raise SystemExit(f"{where}: card fields {got} don't match pack fields {meta['fields']}")
    for f, v in card.items():
        if not isinstance(v, str):
            raise SystemExit(f"{where}: field '{f}' must be a string, got {v!r}")
    if not card[meta["front"][0]].strip() or not card[meta["back"][0]].strip():
        raise SystemExit(f"{where}: '{meta['front'][0]}' and '{meta['back'][0]}' can't be empty: {card}")


def add_note(db: sqlite3.Connection, meta: dict, values: dict, tags: list[str], known: bool = False) -> bool:
    """Insert a note and its single card. Returns False if it duplicates an existing card.

    known=True skips the new queue: the card starts as a review with a mature interval,
    due at a random point within that interval so a batch doesn't come due on one day.
    """
    validate_card(meta, values, "add")
    key = dup_key(meta, values)
    if any(dup_key(meta, note_fields(meta, r["flds"])) == key for r in db.execute("select flds from notes")):
        return False
    sfld = values[meta["front"][0]].strip()
    now = int(time.time())
    nid = new_id()
    flds = "\x1f".join(values[f].strip() for f in meta["fields"])
    db.execute(
        "insert into notes values (?, ?, ?, ?, -1, ?, ?, ?, ?, 0, '')",
        (nid, secrets.token_urlsafe(8), meta["_mid"], now, f" {' '.join(tags)} " if tags else "",
         flds, sfld, field_checksum(sfld)),
    )
    if known:
        crt = db.execute("select crt from col").fetchone()[0]
        state, due, ivl = REVIEW, today_num(crt) + random.randint(1, KNOWN_IVL), KNOWN_IVL
    else:
        position = db.execute("select coalesce(max(due), 0) + 1 from cards where type = ?", (NEW,)).fetchone()[0]
        state, due, ivl = NEW, position, 0
    db.execute(
        "insert into cards values (?, ?, ?, 0, ?, -1, ?, ?, ?, ?, 2500, 0, 0, 0, 0, 0, 0, '')",
        (new_id(), nid, meta["_did"], now, state, state, due, ivl),
    )
    return True


def note_fields(meta: dict, flds: str) -> dict:
    values = flds.split("\x1f")
    if len(values) != len(meta["fields"]):
        raise SystemExit(f"note has {len(values)} fields, pack defines {len(meta['fields'])}")
    return dict(zip(meta["fields"], values))


def classify_cards(db: sqlite3.Connection, meta: dict, cards: list[dict]) -> list[tuple[str, str]]:
    """Per candidate card: (kind, note), kind in new / similar / dup.

    dup: prompt and answer both match an existing card, or an earlier card in this batch.
    similar: only one side matches an existing card or an earlier card in this batch.
    """
    f, b = meta["front"][0], meta["back"][0]
    existing = [note_fields(meta, r["flds"]) for r in db.execute("select flds from notes")]
    exact = {dup_key(meta, v) for v in existing}
    by_front = {norm(v[f]): v for v in existing}
    by_back = {norm(v[b]): v for v in existing}

    status = []
    seen = set()
    batch_front, batch_back = {}, {}
    for i, c in enumerate(cards, 1):
        key = dup_key(meta, c)
        if key in exact:
            status.append(("dup", "already in pack"))
        elif key in seen:
            status.append(("dup", "repeated in this batch"))
        elif key[0] in by_front or key[1] in by_back:
            other = by_front.get(key[0]) or by_back[key[1]]
            status.append(("similar", f'≈ similar to "{other[f]}" = {other[b]}'))
        elif key[0] in batch_front or key[1] in batch_back:
            j = batch_front.get(key[0]) or batch_back[key[1]]
            status.append(("similar", f"≈ similar to #{j} in this batch"))
        else:
            status.append(("new", ""))
        seen.add(key)
        batch_front.setdefault(key[0], i)
        batch_back.setdefault(key[1], i)
    return status


def parse_selection(raw: str, count: int) -> list[int] | None:
    """'1,3,5-7' -> [1, 3, 5, 6, 7]; None if malformed or out of range."""
    picked = []
    for part in raw.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        lo, hi = int(m[1]), int(m[2] or m[1])
        if not 1 <= lo <= hi <= count:
            return None
        picked += range(lo, hi + 1)
    return picked


def print_candidates(meta: dict, cards: list[dict], status: list[tuple[str, str]]) -> None:
    f, b = meta["front"][0], meta["back"][0]
    print()
    for i, (c, (kind, note)) in enumerate(zip(cards, status), 1):
        rest = "  ".join(c[x] for x in meta["back"][1:2] if c[x])
        if kind == "dup":
            print(f"  {DIM}{i:>2}  {c[f]}  {c[b]}  · {note}{RESET}")
            continue
        print(f"  {i:>2}  {BOLD}{c[f]}{RESET}  {CYAN}{c[b]}{RESET}  {rest}")
        extra = [c[x] for x in meta["back"][2:] if c[x]]
        if extra:
            print(f"      {DIM}{'  '.join(extra)}{RESET}")
        if kind == "similar":
            print(f"      {YELLOW}{note}{RESET}")


def confirm_cards(db: sqlite3.Connection, meta: dict, cards: list[dict]) -> list[dict]:
    """Preview candidate cards and return the ones the user picks.

    Exact duplicates can't be picked. Similar cards are flagged ≈ and left out of 'y',
    but can be picked by number.
    """
    status = classify_cards(db, meta, cards)
    print_candidates(meta, cards, status)
    pickable = [i for i, (kind, _) in enumerate(status, 1) if kind != "dup"]
    if not pickable:
        print("\nnothing new to add")
        return []
    default = [i for i, (kind, _) in enumerate(status, 1) if kind == "new"]
    while True:
        raw = input(f"\n  add? [y = {len(default)} new / n / e.g. 1,3,5-7] > ").strip().lower()
        if raw == "n":
            return []
        picked = default if raw == "y" else parse_selection(raw, len(cards))
        if picked is not None and all(i in pickable for i in picked):
            return [cards[i - 1] for i in picked]
        print(f"  {RED}pick from: {', '.join(map(str, pickable))}{RESET}")


def save_cards(db: sqlite3.Connection, meta: dict, cards: list[dict], tags: list[str], known: bool = False) -> None:
    added = sum(add_note(db, meta, c, tags, known=known) for c in cards)
    db.commit()
    print(f"added {added}" + (f" ({' '.join(tags)})" if tags else ""))


def card_status(row: sqlite3.Row) -> str:
    if row["type"] == NEW:
        return "new"
    if row["lapses"] > 0 or row["factor"] < 2300 or row["ivl"] == 0:
        return "struggling"
    if row["ivl"] >= 21:
        return "known"
    return "learning"


# ---------- lessons ----------

def lesson_dir(name: str) -> Path:
    return LESSONS_DIR / name


def load_lessons(name: str, meta: dict) -> list[dict]:
    """All lesson files for a pack, validated and numbered 1..N with no gaps."""
    lessons = []
    for path in sorted(lesson_dir(name).glob("*.json")):
        m = re.fullmatch(r"(\d{2})-[a-z0-9-]+\.json", path.name)
        if not m:
            raise SystemExit(f"{path}: lesson files must be named NN-slug.json")
        data = json.loads(path.read_text())
        if set(data) != {"title", "focus", "body", "cards"}:
            raise SystemExit(f"{path}: lesson keys must be title, focus, body, cards; got {sorted(data)}")
        for i, card in enumerate(data["cards"], 1):
            validate_card(meta, card, f"{path.name} card {i}")
        lessons.append({"n": int(m[1]), "path": path, **data})
    numbers = [lesson["n"] for lesson in lessons]
    if numbers != list(range(1, len(lessons) + 1)):
        raise SystemExit(f"lessons/{name}: lesson numbers must run 1..N with no gaps, got {numbers}")
    return lessons


def unit_progress(db: sqlite3.Connection, meta: dict, lessons: list[dict]) -> list[dict]:
    """Per lesson: learned / mastered ratios, opened, mastered (done), unlocked.

    Lessons unlock in order: the next opens when the newest unlocked lesson has been
    opened and is 80% learned, as long as that leaves at most MAX_OPEN_UNITS unmastered
    lessons open. `unlocked_through` in the pack metadata unlocks lessons early.
    """
    units = [{"n": l["n"], "title": l["title"], "cards": 0, "learned": 0, "mastered": 0} for l in lessons]
    for r in db.execute("select n.tags, c.ivl from cards c join notes n on n.id = c.nid"):
        n = unit_of(r["tags"])
        if n is None:
            continue
        if not 1 <= n <= len(units):
            raise SystemExit(f"card tagged unit::{n} but pack has {len(units)} lessons")
        units[n - 1]["cards"] += 1
        units[n - 1]["learned"] += r["ivl"] >= LEARNED_IVL
        units[n - 1]["mastered"] += r["ivl"] >= MASTERY_IVL
    for u in units:
        # a lesson only counts once it's been opened, so stray tagged cards can't skip it
        u["opened"] = u["n"] in meta["opened"]
        u["learned_ratio"] = u["learned"] / u["cards"] if u["cards"] else 0.0
        u["ratio"] = u["mastered"] / u["cards"] if u["cards"] else 0.0
        u["done"] = u["opened"] and u["cards"] > 0 and u["ratio"] >= UNIT_RATIO
    frontier = 1
    while frontier < len(units):
        newest = units[frontier - 1]
        if not newest["opened"] or newest["learned_ratio"] < UNIT_RATIO:
            break
        if sum(not u["done"] for u in units[: frontier + 1]) > MAX_OPEN_UNITS:
            break
        frontier += 1
    for u in units:
        u["unlocked"] = u["opened"] or u["n"] <= max(frontier, meta["unlocked_through"])
    return units


def next_lesson(units: list[dict]) -> dict | None:
    """The first unlocked lesson not opened yet."""
    return next((u for u in units if u["unlocked"] and not u["opened"]), None)


def unit_status(unit: dict) -> str:
    return f"{unit['cards']} cards · {unit['learned_ratio']:.0%} learned · {unit['ratio']:.0%} mastered"


def lock_reason(units: list[dict]) -> str:
    """Why the lesson after the newest unlocked one is still locked."""
    newest = [u for u in units if u["unlocked"]][-1]
    if not newest["opened"]:
        return f"open lesson {newest['n']}"
    if newest["learned_ratio"] < UNIT_RATIO:
        return f"learn lesson {newest['n']} ({newest['learned_ratio']:.0%} learned / {UNIT_RATIO:.0%})"
    oldest_open = next(u for u in units if not u["done"])
    return f"master lesson {oldest_open['n']} ({oldest_open['ratio']:.0%} mastered / {UNIT_RATIO:.0%})"


def page(text: str) -> None:
    subprocess.run(["less", "-R"], input=text, text=True, env={**os.environ, "LESSCHARSET": "utf-8"}, check=True)


# ---------- review ----------

def sm2(ivl: int, factor: int, q: int) -> tuple[int, int]:
    """Return (new interval in days, new factor in permille). q is 1-5; q < 3 is a lapse (ivl 0)."""
    ef = factor / 1000
    ef = max(1.3, ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
    if q < 3:
        return 0, round(ef * 1000)
    if ivl == 0:
        new_ivl = 1
    elif ivl == 1:
        new_ivl = 6
    else:
        new_ivl = round(ivl * ef)
    return new_ivl, round(ef * 1000)


def answer(db: sqlite3.Connection, card: sqlite3.Row, q: int, today: int, ms_taken: int) -> int:
    ivl, factor = sm2(card["ivl"], card["factor"], q)
    lapsed = q < 3 and card["type"] == REVIEW and card["ivl"] > 0
    if card["type"] == NEW:
        log_type = LOG_LEARN
    elif card["ivl"] == 0:
        log_type = LOG_RELEARN
    else:
        log_type = LOG_REVIEW
    db.execute(
        "update cards set type = ?, queue = ?, due = ?, ivl = ?, factor = ?, reps = reps + 1, "
        "lapses = lapses + ?, mod = ?, usn = -1 where id = ?",
        (REVIEW, REVIEW, today + ivl, ivl, factor, int(lapsed), int(time.time()), card["id"]),
    )
    db.execute(
        "insert into revlog values (?, ?, -1, ?, ?, ?, ?, ?, ?)",
        (new_id(), card["id"], q, ivl, card["ivl"], factor, ms_taken, log_type),
    )
    db.commit()
    return ivl


def cmd_review(name: str) -> None:
    db = open_pack(name)
    meta = read_meta(db)
    crt = db.execute("select crt from col").fetchone()[0]
    today = today_num(crt)
    lessons = load_lessons(name, meta)

    introduced = db.execute(
        "select count(distinct cid) from revlog where type = ? and id >= ?", (LOG_LEARN, today_start_ms())
    ).fetchone()[0]
    new_limit = max(0, meta["new_per_day"] - introduced)
    due = db.execute(
        "select id from cards where queue = ? and due <= ? order by due", (REVIEW, today)
    ).fetchall()
    new = db.execute(
        "select id from cards where queue = ? order by due limit ?", (NEW, new_limit)
    ).fetchall()
    queue = [r["id"] for r in due] + [r["id"] for r in new]
    random.shuffle(queue)

    print(f"{BOLD}[{name}]{RESET} {len(new)} new · {len(due)} due")
    unseen = db.execute("select count(*) from cards where queue = ?", (NEW,)).fetchone()[0]
    if unseen > len(new):
        print(f"{DIM}{unseen - len(new)} more new cards waiting (limit {meta['new_per_day']} new/day; "
              f"change with fc edit {name}){RESET}")
    reviewed = failed = 0
    while queue:
        cid = queue.pop(0)
        card = db.execute("select * from cards where id = ?", (cid,)).fetchone()
        note = db.execute("select flds from notes where id = ?", (card["nid"],)).fetchone()
        values = note_fields(meta, note["flds"])

        print(f"\n{DIM}{'─' * 40}  {len(queue) + 1} left{RESET}\n")
        for i, f in enumerate(meta["front"]):
            print(f"  {BOLD if i == 0 else ''}{values[f]}{RESET}")
        started = time.time()
        if input(f"\n{DIM}  (enter){RESET} ").strip().lower() == "q":
            break
        print(f"  {DIM}{'─' * 20}{RESET}")
        for i, f in enumerate(meta["back"]):
            if not values[f]:
                continue
            style = BOLD + CYAN if i == 0 else ("" if i == 1 else DIM)
            print(f"  {style}{values[f]}{RESET}")

        while True:
            raw = input(f"\n  {DIM}1 no idea · 2 vague · 3 hard · 4 good · 5 instant{RESET} > ").strip().lower()
            if raw == "q" or raw in {"1", "2", "3", "4", "5"}:
                break
        if raw == "q":
            break
        q = int(raw)
        ivl = answer(db, card, q, today, int((time.time() - started) * 1000))
        reviewed += 1
        if q < 3:
            failed += 1
            queue.insert(min(REQUEUE_GAP, len(queue)), cid)
            print(f"  {RED}again soon{RESET}")
        else:
            color = YELLOW if q == 3 else GREEN
            print(f"  {color}next in {ivl}d{RESET}")

    if reviewed:
        remaining_new = db.execute("select count(*) from cards where queue = ?", (NEW,)).fetchone()[0]
        print(f"\n{BOLD}done{RESET}: {reviewed} answered, {failed} missed. {remaining_new} unseen cards left in pack.")
    else:
        print("nothing to review.")
    if lessons:
        ready = next_lesson(unit_progress(db, meta, lessons))
        if ready:
            print(f"{GREEN}lesson {ready['n']} ready: {ready['title']}{RESET}. read it: fc lesson {name}")


# ---------- commands ----------

def cmd_new(name: str, args: list[str]) -> None:
    if len(args) > 1:
        raise SystemExit("usage: fc new <pack> [meta.json]")
    if name in COMMANDS:
        raise SystemExit(f"'{name}' is a command name, pick another pack name")
    meta = read_json_arg(args[0]) if args else edit_json_in_editor({**TEMPLATE_META, "name": name})
    create_pack(name, meta)
    print(f"created {pack_path(name)}. lessons go in {lesson_dir(name).relative_to(ROOT)}/NN-slug.json")


def cmd_edit(name: str) -> None:
    db = open_pack(name)
    meta = read_meta(db)
    edited = edit_json_in_editor(public_meta(meta))
    has_notes = db.execute("select count(*) from notes").fetchone()[0] > 0
    if has_notes and edited["fields"] != meta["fields"]:
        raise SystemExit("can't change 'fields' once the pack has notes")
    if edited["name"] != name:
        raise SystemExit("can't rename a pack via edit; rename the file in packs/ instead")
    write_meta(db, edited, meta["_mid"], meta["_did"])
    db.commit()
    print("saved")


def cmd_lesson(name: str, args: list[str]) -> None:
    if len(args) > 1 or (args and not args[0].isdigit()):
        raise SystemExit("usage: fc lesson <pack> [n]")
    db = open_pack(name)
    meta = read_meta(db)
    lessons = load_lessons(name, meta)
    if not lessons:
        raise SystemExit(f"no lessons in {lesson_dir(name).relative_to(ROOT)}/")
    units = unit_progress(db, meta, lessons)
    if args:
        n = int(args[0])
        if not 1 <= n <= len(units):
            raise SystemExit(f"lesson {n} doesn't exist; pack has {len(units)} lessons")
        if not units[n - 1]["unlocked"]:
            raise SystemExit(f"lesson {n} is locked until you {lock_reason(units)}")
    else:
        ready = next_lesson(units)
        if ready is None:
            if all(u["unlocked"] for u in units):
                raise SystemExit(f"all {len(units)} lessons opened. reread one with: fc lesson {name} <n>")
            raise SystemExit(f"no new lesson yet: lesson {len([u for u in units if u['unlocked']]) + 1} "
                             f"unlocks when you {lock_reason(units)}")
        n = ready["n"]
    unit, lesson = units[n - 1], lessons[n - 1]

    if unit["done"]:
        print(f"lesson {n} mastered ✓")
    elif unit["opened"]:
        print(f"lesson {n}: {unit_status(unit)}")
    else:
        print(f"lesson {n}: {lesson['title']}")
    input(f"{DIM}  (enter to read, q to leave the pager){RESET} ")
    page(lesson["body"])
    meta["opened"].append(n)
    save_meta(db, meta)
    print(f"{BOLD}cards for lesson {n}{RESET}")
    chosen = confirm_cards(db, meta, lesson["cards"])
    if chosen:
        save_cards(db, meta, chosen, [f"unit::{n}"])


def cmd_unlock(name: str, args: list[str]) -> None:
    if len(args) != 1 or not args[0].isdigit():
        raise SystemExit("usage: fc unlock <pack> <n>   (unlocks lessons 1..n)")
    db = open_pack(name)
    meta = read_meta(db)
    n = int(args[0])
    count = len(load_lessons(name, meta))
    if not 1 <= n <= count:
        raise SystemExit(f"pack has {count} lessons")
    meta["unlocked_through"] = n
    save_meta(db, meta)
    print(f"lessons 1-{n} unlocked. read the next one: fc lesson {name}")


def cmd_import(name: str, args: list[str]) -> None:
    """Add cards from a JSON array of card objects (fields as keys).

    Interactive by default (same picker as lessons). --yes adds every new card without
    asking, skips duplicates, and skips similar cards unless --include-similar.
    """
    usage = "usage: fc import <pack> <cards.json | -> [--tag T]... [--known] [--yes [--include-similar]]"
    flags = {"--known", "--yes", "--include-similar"}
    source, tags, opts = None, [], set()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tag":
            if i + 1 >= len(args) or not re.fullmatch(r"[\w:-]+", args[i + 1]):
                raise SystemExit("--tag needs a value without spaces")
            tags.append(args[i + 1])
            i += 2
            continue
        if a in flags:
            opts.add(a)
        elif source is None and not a.startswith("--"):
            source = a
        else:
            raise SystemExit(usage)
        i += 1
    if source is None:
        raise SystemExit(usage)
    if source == "-" and "--yes" not in opts:
        raise SystemExit("reading cards from stdin needs --yes (stdin can't also answer the picker)")
    if "--include-similar" in opts and "--yes" not in opts:
        raise SystemExit("--include-similar only applies with --yes")
    if any(t.startswith("unit::") for t in tags):
        raise SystemExit("unit:: tags come from lessons; use a lesson file instead")

    db = open_pack(name)
    meta = read_meta(db)
    cards = read_json_arg(source)
    if not isinstance(cards, list) or not cards:
        raise SystemExit("expected a non-empty JSON array of cards")
    for i, c in enumerate(cards, 1):
        validate_card(meta, c, f"card {i}")
    known = "--known" in opts

    if "--yes" not in opts:
        chosen = confirm_cards(db, meta, cards)
        if chosen:
            save_cards(db, meta, chosen, tags, known=known)
        return

    status = classify_cards(db, meta, cards)
    keep = {"new", "similar"} if "--include-similar" in opts else {"new"}
    f, b = meta["front"][0], meta["back"][0]
    for c, (kind, note) in zip(cards, status):
        if kind not in keep:
            print(f"  skipped  {c[f]} = {c[b]}  ({note})")
    save_cards(db, meta, [c for c, (kind, _) in zip(cards, status) if kind in keep], tags, known=known)


def cmd_cards(name: str, args: list[str]) -> None:
    """Every card with its review state: the learner's history, for whoever writes new cards."""
    if args not in ([], ["--json"]):
        raise SystemExit("usage: fc cards <pack> [--json]")
    db = open_pack(name)
    meta = read_meta(db)
    today = today_num(db.execute("select crt from col").fetchone()[0])
    rows = db.execute(
        "select n.flds, n.tags, c.type, c.ivl, c.factor, c.lapses, c.reps, c.due "
        "from cards c join notes n on n.id = c.nid order by n.id"
    ).fetchall()
    out = [{
        "fields": note_fields(meta, r["flds"]),
        "tags": note_tags(r["tags"]),
        "status": card_status(r),
        "interval_days": r["ivl"],
        "ease": r["factor"] / 1000,
        "reviews": r["reps"],
        "lapses": r["lapses"],
        "due_in_days": r["due"] - today if r["type"] == REVIEW else None,
    } for r in rows]
    if args:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    f, b = meta["front"][0], meta["back"][0]
    for c in out:
        print(f"{c['status']:<10} {c['interval_days']:>3}d  lapses {c['lapses']}  "
              f"{c['fields'][f]} = {c['fields'][b]}  {DIM}{' '.join(c['tags'])}{RESET}")


def cmd_add(name: str, args: list[str]) -> None:
    if args not in ([], ["--known"]):
        raise SystemExit("usage: fc add <pack> [--known]")
    known = args == ["--known"]
    db = open_pack(name)
    meta = read_meta(db)
    print(f"adding to {name}{' as known' if known else ''} (ctrl-d to stop)")
    while True:
        try:
            values = {f: input(f"{f}: ") for f in meta["fields"]}
        except EOFError:
            print()
            return
        if add_note(db, meta, values, ["mine"], known=known):
            db.commit()
            print(f"{GREEN}added{RESET}\n")
        else:
            print(f"{YELLOW}already in pack, skipped{RESET}\n")


def cmd_stats(name: str) -> None:
    db = open_pack(name)
    meta = read_meta(db)
    crt = db.execute("select crt from col").fetchone()[0]
    today = today_num(crt)
    c = db.execute(
        """select count(*) total,
                  sum(type = 0) unseen,
                  sum(type = 2 and due <= ?) due,
                  sum(type = 2 and ivl >= 21) mature,
                  sum(lapses) lapses
           from cards""",
        (today,),
    ).fetchone()
    reviews_today = db.execute("select count(*) from revlog where id >= ?", (today_start_ms(),)).fetchone()[0]
    print(f"{BOLD}{name}{RESET}  ({', '.join(meta['front'])} → {', '.join(meta['back'])})")
    print(f"  cards     {c['total']}")
    print(f"  unseen    {c['unseen'] or 0}")
    print(f"  due now   {c['due'] or 0}")
    print(f"  mature    {c['mature'] or 0}  (interval ≥ 21d)")
    print(f"  lapses    {c['lapses'] or 0}")
    print(f"  today     {reviews_today} answers")
    lessons = load_lessons(name, meta)
    if not lessons:
        return
    units = unit_progress(db, meta, lessons)
    ready = next_lesson(units)
    print(f"\n  {BOLD}lessons{RESET}  {DIM}learned = right after 1 day · mastered = right after ~a week{RESET}")
    print(f"  {DIM}next lesson unlocks at {UNIT_RATIO:.0%} learned; ✓ at {UNIT_RATIO:.0%} mastered{RESET}")
    for u in units:
        if u["done"]:
            mark = f"{GREEN}✓{RESET}"
        elif ready is not None and u["n"] == ready["n"]:
            mark = f"{CYAN}▸{RESET}"
        elif u["opened"]:
            mark = f"{YELLOW}○{RESET}"
        else:
            mark = " "
        line = f"  {mark} {u['n']:>2}  {u['title']:<38}"
        if u["cards"]:
            filled = round(u["ratio"] * 12)
            line += (f" {u['cards']:>3} cards  {'█' * filled}{DIM}{'░' * (12 - filled)}{RESET}"
                     f" {u['learned_ratio']:>4.0%} learned {u['ratio']:>4.0%} mastered")
        elif u["unlocked"] and not u["opened"]:
            line += " ready"
        print(line if u["unlocked"] else f"{DIM}{line}{RESET}")
    if ready is not None:
        print(f"\n  lesson {ready['n']} is ready: fc lesson {name}")
    elif not all(u["unlocked"] for u in units):
        print(f"\n  next lesson unlocks when you {lock_reason(units)}")


def cmd_list() -> None:
    packs = sorted(PACKS_DIR.glob("*.anki2")) if PACKS_DIR.exists() else []
    if not packs:
        print("no packs. create one with: fc new <name>")
    for p in packs:
        print(p.stem)


USAGE = """usage:
  fc [pack]                       review one pack, or every pack in turn if none given
  fc lesson <pack> [n]            read the next unlocked lesson (or lesson n) and pick its cards
  fc stats <pack>                 deck + lesson progress
  fc add <pack> [--known]         type a card in field by field
  fc unlock <pack> <n>            unlock lessons 1..n early

for writing content (e.g. Claude):
  fc cards <pack> [--json]        every card with its review state
  fc import <pack> <file|-> [--tag T]... [--known] [--yes [--include-similar]]
                                  add cards from a JSON array; duplicates are skipped
  fc new <pack> [meta.json]       create a pack (opens $EDITOR without a file)
  fc edit <pack>                  edit pack metadata in $EDITOR
  fc list"""


def main(argv: list[str]) -> None:
    if not argv:
        packs = sorted(PACKS_DIR.glob("*.anki2")) if PACKS_DIR.exists() else []
        if not packs:
            raise SystemExit(USAGE + "\n\npacks: none")
        for i, p in enumerate(packs):
            if i:
                print()
            cmd_review(p.stem)
        return
    head, rest = argv[0], argv[1:]
    if head not in COMMANDS:
        if rest:
            raise SystemExit(USAGE)
        return cmd_review(head)
    if head == "help":
        return print(USAGE)
    if head == "list":
        return cmd_list()
    if not rest:
        raise SystemExit(USAGE)
    name, args = rest[0], rest[1:]
    {
        "new": lambda: cmd_new(name, args),
        "edit": lambda: cmd_edit(name),
        "lesson": lambda: cmd_lesson(name, args),
        "unlock": lambda: cmd_unlock(name, args),
        "import": lambda: cmd_import(name, args),
        "cards": lambda: cmd_cards(name, args),
        "add": lambda: cmd_add(name, args),
        "stats": lambda: cmd_stats(name),
    }[head]()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (KeyboardInterrupt, EOFError):
        print()
