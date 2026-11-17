"""Local browser UI for fc: a stdlib HTTP server on 127.0.0.1 plus one static page.

The page (web/index.html) talks to a small JSON API; all scheduling and lesson logic
lives in fc.py. Errors come back as {"error": ...} with a 4xx/5xx status and the page
shows them as-is.
"""

import json
import re
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fc

PAGE = Path(__file__).resolve().parent / "web" / "index.html"


def installed_packs() -> list[str]:
    return sorted(p.stem for p in fc.PACKS_DIR.glob("*.anki2"))


def pack_summary(name: str) -> dict:
    db = fc.open_pack(name)
    meta = fc.read_meta(db)
    q = fc.review_queue(db, meta)
    lessons = fc.load_lessons(name, meta)
    units = fc.unit_progress(db, meta, lessons) if lessons else []
    ready = fc.next_lesson(units) if units else None
    locked = units and not all(u["unlocked"] for u in units)
    return {
        "name": name,
        "due": q["due"],
        "new": q["new"],
        "waiting": q["waiting"],
        "new_per_day": meta["new_per_day"],
        "next_lesson": {"n": ready["n"], "title": ready["title"]} if ready else None,
        "lock_reason": fc.lock_reason(units) if locked and ready is None else None,
        "lessons": [{k: u[k] for k in ("n", "title", "cards", "learned_ratio", "ratio", "opened", "done", "unlocked")}
                    for u in units],
    }


def review(name: str) -> dict:
    db = fc.open_pack(name)
    meta = fc.read_meta(db)
    q = fc.review_queue(db, meta)
    cards = []
    for cid in q["queue"]:
        row = db.execute("select n.flds from cards c join notes n on n.id = c.nid where c.id = ?", (cid,)).fetchone()
        cards.append({"id": cid, "values": fc.note_fields(meta, row["flds"])})
    return {"name": name, "front": meta["front"], "back": meta["back"], "cards": cards,
            "due": q["due"], "new": q["new"], "waiting": q["waiting"], "new_per_day": meta["new_per_day"]}


def lesson(name: str, n: int) -> dict:
    db = fc.open_pack(name)
    meta = fc.read_meta(db)
    lessons = fc.load_lessons(name, meta)
    units = fc.unit_progress(db, meta, lessons)
    n = fc.pick_lesson(units, n)
    les = lessons[n - 1]
    status = fc.classify_cards(db, meta, les["cards"])
    return {
        "name": name, "n": n, "title": les["title"], "body": les["body"], "unit": units[n - 1],
        "front": meta["front"], "back": meta["back"], "total": len(lessons),
        "cards": [{"values": c, "kind": kind, "note": note} for c, (kind, note) in zip(les["cards"], status)],
    }


def add_lesson(name: str, n: int, picked: list[int]) -> dict:
    db = fc.open_pack(name)
    meta = fc.read_meta(db)
    lessons = fc.load_lessons(name, meta)
    n = fc.pick_lesson(fc.unit_progress(db, meta, lessons), n)
    return {"added": fc.add_lesson_cards(db, meta, lessons[n - 1], picked)}


def answer(name: str, body: dict) -> dict:
    db = fc.open_pack(name)
    # cap thinking time so a card left open while you're away doesn't skew the log
    ms = min(int(body["ms"]), 60_000)
    return {"ivl": fc.answer_card(db, int(body["card_id"]), int(body["q"]), ms)}


GET_ROUTES = [
    (r"/api/packs", lambda: [pack_summary(p) for p in installed_packs()]),
    (r"/api/review/([\w-]+)", review),
    (r"/api/lesson/([\w-]+)/(\d+)", lambda name, n: lesson(name, int(n))),
]
POST_ROUTES = [
    (r"/api/answer/([\w-]+)", answer),
    (r"/api/lesson/([\w-]+)/(\d+)", lambda name, n, body: add_lesson(name, int(n), body["picked"])),
]


class Handler(BaseHTTPRequestHandler):
    def send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status: int, data) -> None:
        self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def route(self, routes, *extra) -> None:
        path = self.path.split("?")[0]
        for pattern, fn in routes:
            m = re.fullmatch(pattern, path)
            if m:
                try:
                    return self.json(200, fn(*m.groups(), *extra))
                except SystemExit as e:  # fc's own validation errors: show them to the user
                    return self.json(400, {"error": str(e)})
                except Exception:
                    traceback.print_exc()
                    return self.json(500, {"error": traceback.format_exc()})
        self.json(404, {"error": f"no route {self.command} {path}"})

    def do_GET(self) -> None:
        if not self.path.startswith("/api/"):
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        self.route(GET_ROUTES)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.route(POST_ROUTES, body)

    def log_message(self, *args) -> None:
        pass


def serve(route: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#{route}"
    print(f"fc is open in your browser: {url}\nctrl-c here when you're done")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
