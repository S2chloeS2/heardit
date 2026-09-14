"""SQLite persistence for Heardit.

Sessions survive restarts, which is what makes the Review screen worth having.
Everything is stored in a single file so the app stays clone-and-run.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "heardit.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    google_sub  TEXT    NOT NULL UNIQUE,   -- Google's stable user id
    email       TEXT    NOT NULL,
    name        TEXT,
    picture     TEXT,
    created_at  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL DEFAULT 'Untitled session',
    kind        TEXT    NOT NULL DEFAULT 'lecture',   -- lecture | meeting
    source      TEXT    NOT NULL DEFAULT 'mic',       -- system | mic | link
    source_url  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    summary     TEXT,
    keywords    TEXT             -- JSON array
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    speaker    TEXT,          -- "A", "B" … from diarization; NULL when unknown
    start_ms   INTEGER,       -- offset into the recording
    end_ms     INTEGER
);

CREATE TABLE IF NOT EXISTS speakers (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label      TEXT    NOT NULL,   -- "A", "B" …
    name       TEXT,               -- what the user renamed them to
    PRIMARY KEY (session_id, label)
);

CREATE TABLE IF NOT EXISTS keyword_notes (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    keyword     TEXT    NOT NULL,
    explanation TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (session_id, keyword)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,   -- user | assistant
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS folder_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code       TEXT    PRIMARY KEY,           -- stored upper-case
    kind       TEXT    NOT NULL,              -- 'percent' | 'fixed' | 'comp'
    value      INTEGER NOT NULL DEFAULT 0,    -- percent, KRW, or unused for comp
    plan       TEXT,                          -- comp: plan granted
    months     INTEGER NOT NULL DEFAULT 1,    -- comp: how long
    max_uses   INTEGER,                       -- NULL = unlimited
    uses       INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    note       TEXT,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL REFERENCES promo_codes(code) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,            -- 'subscription' | 'renewal' | 'topup' | 'comp'
    item         TEXT    NOT NULL,            -- plan key or top-up key
    seconds      INTEGER NOT NULL DEFAULT 0,  -- credit granted (top-ups)
    list_price   INTEGER NOT NULL DEFAULT 0,  -- KRW
    discount     INTEGER NOT NULL DEFAULT 0,
    amount       INTEGER NOT NULL DEFAULT 0,  -- what was charged
    promo_code   TEXT,
    provider     TEXT    NOT NULL,            -- 'stripe' | 'simulated' | 'comp'
    provider_ref TEXT,                        -- checkout/invoice id, for support
    status       TEXT    NOT NULL DEFAULT 'paid',
    created_at   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    seconds    INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

"""

# Indexes run after the migrations below, because an index cannot reference a
# column that an older database has not been given yet.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_folder ON sessions(folder_id);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_redemptions_user ON promo_redemptions(user_id);
CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, created_at);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Older databases predate the diarization columns; add what is missing.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if "folder_id" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")

        if "language" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN language TEXT")

        for column, decl in (
            ("exam_sheet", "TEXT"),
            ("notes_lang", "TEXT"),        # override for the notes' language
            ("attachment_name", "TEXT"),   # original file name of the slides PDF
            ("attachment_text", "TEXT"),   # extracted text, capped, for the notes
        ):
            if column not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {decl}")

        ucols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "plan" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        for column, decl in (
            ("plan_until", "TEXT"),                          # paid/comped plan lapses after this
            ("bonus_seconds", "INTEGER NOT NULL DEFAULT 0"),  # top-up credits
            ("stripe_customer_id", "TEXT"),
            ("stripe_subscription_id", "TEXT"),
        ):
            if column not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {decl}")

        lcols = {row["name"] for row in conn.execute("PRAGMA table_info(usage_log)")}
        if "bonus_s" not in lcols:
            conn.execute("ALTER TABLE usage_log ADD COLUMN bonus_s INTEGER NOT NULL DEFAULT 0")

        existing = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
        for column, decl in (
            ("speaker", "TEXT"),
            ("start_ms", "INTEGER"),
            ("end_ms", "INTEGER"),
            ("translation", "TEXT"),       # cached translation of `text`
            ("translation_lang", "TEXT"),  # language code of that translation
            ("audio_path", "TEXT"),        # file under AUDIO_DIR, relative
            ("audio_offset_ms", "INTEGER"),  # where this segment starts in it
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE segments ADD COLUMN {column} {decl}")

        conn.executescript(INDEXES)


# ---------------------------------------------------------------- sessions

def create_session(title, kind="lecture", source="mic", source_url=None, user_id=None, folder_id=None):
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (title, kind, source, source_url, created_at, updated_at, user_id, folder_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (title, kind, source, source_url, ts, ts, user_id, folder_id),
        )
        return cur.lastrowid


def get_session(session_id, user_id=None):
    """Fetch a session. With `user_id`, only that user's own session is
    returned — everyone else gets None, which the app turns into a 404 so a
    stranger cannot even learn that the session exists."""
    with connect() as conn:
        if user_id is None:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)
            ).fetchone()
        return dict(row) if row else None


def list_sessions(user_id, folder_id=None):
    """All of a user's sessions, newest first. With `folder_id`, only that
    folder's; with folder_id="none", only the ones not filed anywhere."""
    where = "s.user_id = ?"
    params = [user_id]
    if folder_id == "none":
        where += " AND s.folder_id IS NULL"
    elif folder_id is not None:
        where += " AND s.folder_id = ?"
        params.append(folder_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, ("
            "  SELECT COUNT(*) FROM segments g WHERE g.session_id = s.id"
            ") AS segment_count"
            f" FROM sessions s WHERE {where} ORDER BY s.created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def update_session(session_id, **fields):
    allowed = {"title", "kind", "summary", "keywords", "source_url", "folder_id", "language",
               "exam_sheet", "attachment_name", "attachment_text", "notes_lang"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "keywords" and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([now(), session_id])
    with connect() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", values)


def delete_session(session_id):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def expired_sessions(days, plan_keys=("free",), exempt_emails=()):
    """Sessions older than `days` belonging to users on the given plans."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    marks = ",".join("?" * len(plan_keys))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT s.id, s.user_id, u.email, u.plan, u.plan_until FROM sessions s"
            f" JOIN users u ON u.id = s.user_id"
            f" WHERE s.created_at < ? AND COALESCE(u.plan, 'free') IN ({marks})",
            (cutoff, *plan_keys),
        ).fetchall()
        return [dict(r) for r in rows if (r["email"] or "").lower() not in exempt_emails]


# ---------------------------------------------------------------- segments

def add_segment(session_id, text, speaker=None, start_ms=None, end_ms=None,
                audio_path=None, audio_offset_ms=None):
    """Store one transcribed segment and return its id."""
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO segments (session_id, text, created_at, speaker, start_ms,"
            " end_ms, audio_path, audio_offset_ms) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, text, ts, speaker, start_ms, end_ms, audio_path, audio_offset_ms),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))
        return cur.lastrowid


def replace_segments(session_id, segments):
    """Swap in a fresh set of segments — used when a session is re-transcribed
    with diarization, which reassigns text to speakers across the whole file."""
    ts = now()
    with connect() as conn:
        conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO segments (session_id, text, created_at, speaker, start_ms, end_ms)"
            " VALUES (?,?,?,?,?,?)",
            [
                (session_id, s["text"], ts, s.get("speaker"),
                 s.get("start_ms"), s.get("end_ms"))
                for s in segments
            ],
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))


def get_segments(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, text, created_at, speaker, start_ms, end_ms, translation,"
            " translation_lang, audio_path, audio_offset_ms"
            " FROM segments WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_transcript(session_id, with_speakers=False):
    """The session's words.

    With `with_speakers`, each line is prefixed by who said it — which is what
    makes a meeting summary able to say who committed to what.
    """
    segs = get_segments(session_id)
    if not with_speakers or not any(s.get("speaker") for s in segs):
        return " ".join(s["text"] for s in segs).strip()

    names = get_speaker_names(session_id)
    lines = []
    for s in segs:
        label = s.get("speaker")
        who = names.get(label) or (f"화자 {label}" if label else "알 수 없음")
        lines.append(f"{who}: {s['text']}")
    return "\n".join(lines).strip()


def segments_needing_translation(session_id, ids, lang):
    """Of `ids`, the segments that have no cached translation into `lang`."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, text FROM segments WHERE session_id=? AND id IN ({marks})"
            " AND (translation IS NULL OR translation_lang IS NOT ?) ORDER BY id",
            (session_id, *ids, lang),
        ).fetchall()
        return [dict(r) for r in rows]


def cached_translations(session_id, ids, lang):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, translation FROM segments WHERE session_id=? AND id IN ({marks})"
            " AND translation_lang=?",
            (session_id, *ids, lang),
        ).fetchall()
        return {r["id"]: r["translation"] for r in rows}


def save_translations(session_id, lang, mapping):
    with connect() as conn:
        conn.executemany(
            "UPDATE segments SET translation=?, translation_lang=? WHERE id=? AND session_id=?",
            [(text, lang, seg_id, session_id) for seg_id, text in mapping.items()],
        )


# ---------------------------------------------------------------- messages

def add_message(session_id, role, content):
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, now()),
        )


def get_messages(session_id, limit=40):
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ------------------------------------------------------------ keyword notes

def save_keyword_note(session_id, keyword, explanation):
    """Keep an explanation so reopening the session shows it without another
    model call — and so the review page can surface what was looked up."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO keyword_notes (session_id, keyword, explanation, created_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(session_id, keyword) DO UPDATE SET"
            "   explanation=excluded.explanation, created_at=excluded.created_at",
            (session_id, keyword, explanation, now()),
        )


def get_keyword_notes(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT keyword, explanation FROM keyword_notes WHERE session_id=?",
            (session_id,),
        ).fetchall()
        return {r["keyword"]: r["explanation"] for r in rows}


# ---------------------------------------------------------------- speakers

def set_speaker_name(session_id, label, name):
    with connect() as conn:
        conn.execute(
            "INSERT INTO speakers (session_id, label, name) VALUES (?,?,?)"
            " ON CONFLICT(session_id, label) DO UPDATE SET name=excluded.name",
            (session_id, label, name),
        )


def get_speaker_names(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT label, name FROM speakers WHERE session_id=?", (session_id,)
        ).fetchall()
        return {r["label"]: r["name"] for r in rows if r["name"]}


def speaker_stats(session_id):
    """Talk time per speaker, as milliseconds and share of the total."""
    segs = [s for s in get_segments(session_id) if s.get("speaker")]
    if not segs:
        return []
    names = get_speaker_names(session_id)
    totals = {}
    for s in segs:
        span = (s.get("end_ms") or 0) - (s.get("start_ms") or 0)
        if span <= 0:
            span = len(s["text"]) * 60  # rough fallback when timings are absent
        totals[s["speaker"]] = totals.get(s["speaker"], 0) + span

    grand = sum(totals.values()) or 1
    return [
        {
            "label": label,
            "name": names.get(label) or f"화자 {label}",
            "ms": ms,
            "share": round(ms / grand * 100),
        }
        for label, ms in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


# ------------------------------------------------------------------- users

def upsert_user(google_sub, email, name=None, picture=None):
    """Create the user on first sign-in, refresh their profile after that."""
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (google_sub, email, name, picture, created_at, last_seen)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(google_sub) DO UPDATE SET"
            "   email=excluded.email, name=excluded.name,"
            "   picture=excluded.picture, last_seen=excluded.last_seen",
            (google_sub, email, name, picture, ts, ts),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub=?", (google_sub,)
        ).fetchone()
        return dict(row)


def get_user(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def claim_orphan_sessions(user_id):
    """Adopt sessions recorded before sign-in existed.

    Without this, everything created while the app had no accounts would become
    unreachable the moment login is switched on.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sessions SET user_id=? WHERE user_id IS NULL", (user_id,)
        )
        return cur.rowcount


def usage_minutes(user_id, since=None):
    """Minutes of audio transcribed, for enforcing plan limits later."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(g.end_ms - g.start_ms), 0) AS ms"
            " FROM segments g JOIN sessions s ON s.id = g.session_id"
            " WHERE s.user_id = ? AND g.start_ms IS NOT NULL"
            + (" AND g.created_at >= ?" if since else ""),
            (user_id, since) if since else (user_id,),
        ).fetchone()
        return round((row["ms"] or 0) / 60000, 1)


# ----------------------------------------------------------------- folders

def create_folder(user_id, name):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO folders (user_id, name, created_at) VALUES (?,?,?)",
            (user_id, name, now()),
        )
        return cur.lastrowid


def get_folder(folder_id, user_id):
    """A folder, only if it belongs to this user — None otherwise."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM folders WHERE id=? AND user_id=?", (folder_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def list_folders(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.*, ("
            "  SELECT COUNT(*) FROM sessions s WHERE s.folder_id = f.id"
            ") AS session_count"
            " FROM folders f WHERE f.user_id = ? ORDER BY f.created_at",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def rename_folder(folder_id, name):
    with connect() as conn:
        conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))


def delete_folder(folder_id):
    """Removes the folder. Its sessions stay, just unfiled (ON DELETE SET NULL)."""
    with connect() as conn:
        conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))


def add_folder_message(folder_id, role, content):
    with connect() as conn:
        conn.execute(
            "INSERT INTO folder_messages (folder_id, role, content, created_at) VALUES (?,?,?,?)",
            (folder_id, role, content, now()),
        )


def get_folder_messages(folder_id, limit=40):
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM folder_messages WHERE folder_id=? ORDER BY id DESC LIMIT ?",
            (folder_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def folder_corpus(folder_id):
    """Everything the folder chat may draw on: each session's title, summary
    and segments. Kept as a plain structure so ai.py can rank pieces of it."""
    with connect() as conn:
        sessions = conn.execute(
            "SELECT id, title, kind, summary, created_at FROM sessions"
            " WHERE folder_id=? ORDER BY created_at",
            (folder_id,),
        ).fetchall()
        out = []
        for s in sessions:
            segs = conn.execute(
                "SELECT text FROM segments WHERE session_id=? ORDER BY id", (s["id"],)
            ).fetchall()
            out.append({
                "id": s["id"], "title": s["title"], "kind": s["kind"],
                "summary": s["summary"] or "", "date": (s["created_at"] or "")[:10],
                "segments": [r["text"] for r in segs],
            })
        return out


# ------------------------------------------------------------------- usage

def log_usage(user_id, session_id, seconds, bonus_s=0):
    """Record transcribed seconds. `bonus_s` is the part paid by top-up
    credits, which the monthly meter must not count."""
    if not seconds or seconds <= 0:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_log (user_id, session_id, seconds, bonus_s, created_at)"
            " VALUES (?,?,?,?,?)",
            (user_id, session_id, int(round(seconds)), int(bonus_s or 0), now()),
        )


def usage_seconds(user_id, since=None):
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(seconds - bonus_s), 0) AS s FROM usage_log WHERE user_id=?"
            + (" AND created_at >= ?" if since else ""),
            (user_id, since) if since else (user_id,),
        ).fetchone()
        return int(row["s"] or 0)


def set_plan(user_id, plan, until=None):
    with connect() as conn:
        conn.execute("UPDATE users SET plan=?, plan_until=? WHERE id=?", (plan, until, user_id))


def add_bonus_seconds(user_id, delta):
    with connect() as conn:
        conn.execute(
            "UPDATE users SET bonus_seconds = MAX(0, bonus_seconds + ?) WHERE id=?",
            (int(delta), user_id),
        )


def set_stripe_ids(user_id, customer_id=None, subscription_id=None):
    with connect() as conn:
        if customer_id is not None:
            conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user_id))
        conn.execute("UPDATE users SET stripe_subscription_id=? WHERE id=?", (subscription_id, user_id))


def user_by_stripe_customer(customer_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------- orders

def add_order(user_id, kind, item, amount, list_price=0, discount=0, seconds=0,
              promo_code=None, provider="simulated", provider_ref=None, status="paid"):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (user_id, kind, item, seconds, list_price, discount, amount,"
            " promo_code, provider, provider_ref, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, kind, item, int(seconds), int(list_price), int(discount), int(amount),
             promo_code, provider, provider_ref, status, now()),
        )
        return cur.lastrowid


def order_exists(provider_ref):
    with connect() as conn:
        return conn.execute("SELECT 1 FROM orders WHERE provider_ref=?", (provider_ref,)).fetchone() is not None


def list_orders(user_id, limit=50):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# -------------------------------------------------------------- promos

def get_promo(code):
    with connect() as conn:
        row = conn.execute("SELECT * FROM promo_codes WHERE code=?", ((code or "").strip().upper(),)).fetchone()
        return dict(row) if row else None


def add_promo(code, kind, value=0, plan=None, months=1, max_uses=None, expires_at=None, note=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO promo_codes (code, kind, value, plan, months, max_uses, uses, expires_at, note, created_at)"
            " VALUES (?,?,?,?,?,?,0,?,?,?)",
            (code.strip().upper(), kind, int(value), plan, int(months), max_uses, expires_at, note, now()),
        )


def delete_promo(code):
    with connect() as conn:
        conn.execute("DELETE FROM promo_codes WHERE code=?", (code.strip().upper(),))


def list_promos():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()]


def promo_used_by(code, user_id):
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?", (code.upper(), user_id)
        ).fetchone() is not None


def redeem_promo(code, user_id):
    with connect() as conn:
        conn.execute("UPDATE promo_codes SET uses = uses + 1 WHERE code=?", (code.upper(),))
        conn.execute(
            "INSERT INTO promo_redemptions (code, user_id, created_at) VALUES (?,?,?)",
            (code.upper(), user_id, now()),
        )


# -------------------------------------------------------------- account

def delete_user(user_id):
    """Remove a user and everything that belongs to them.

    Deletes are explicit rather than relying on ON DELETE CASCADE: databases
    migrated from before accounts existed got `sessions.user_id` via ALTER
    TABLE without a cascade clause, and SQLite cannot add one after the fact.
    Explicit order keeps every foreign key satisfied on both fresh and
    migrated files.
    """
    with connect() as conn:
        session_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM sessions WHERE user_id=?", (user_id,))]
        conn.execute("DELETE FROM orders WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM promo_redemptions WHERE user_id=?", (user_id,))
        folder_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM folders WHERE user_id=?", (user_id,))]

        if session_ids:
            marks = ",".join("?" * len(session_ids))
            for table in ("segments", "messages", "keyword_notes", "speakers"):
                conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", session_ids)
        if folder_ids:
            marks = ",".join("?" * len(folder_ids))
            conn.execute(f"DELETE FROM folder_messages WHERE folder_id IN ({marks})", folder_ids)

        conn.execute("DELETE FROM usage_log WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM folders WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def usage_seconds_all(since=None):
    """Seconds transcribed across every account — the service-wide spend meter."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(seconds), 0) AS s FROM usage_log"
            + (" WHERE created_at >= ?" if since else ""),
            (since,) if since else (),
        ).fetchone()
        return int(row["s"] or 0)
