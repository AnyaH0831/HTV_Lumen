"""Storage for KAZE: SQLite by default, Postgres (including Tiger Data / TimescaleDB) when a postgres:// URL is given.

Both drivers share the same SQL. Events are append-only and keyed by (ts_ms, id) so the table can become a TimescaleDB hypertable.
A dropped Postgres connection is re-opened once before an error is raised."""
import json, os, sqlite3, threading, time

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS kaze_users (id TEXT PRIMARY KEY, doc TEXT NOT NULL, updated_ms BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kaze_sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, doc TEXT NOT NULL, updated_ms BIGINT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS kaze_sessions_status ON kaze_sessions (status)",
    "CREATE INDEX IF NOT EXISTS kaze_sessions_user ON kaze_sessions (user_id)",
    "CREATE TABLE IF NOT EXISTS kaze_events (ts_ms BIGINT NOT NULL, id TEXT NOT NULL, session_id TEXT, user_id TEXT, kind TEXT NOT NULL, vis TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (ts_ms, id))",
    "CREATE INDEX IF NOT EXISTS kaze_events_session ON kaze_events (session_id, ts_ms)",
    "CREATE INDEX IF NOT EXISTS kaze_events_user ON kaze_events (user_id, ts_ms)",
    "CREATE TABLE IF NOT EXISTS kaze_locations (user_id TEXT NOT NULL, ts_ms BIGINT NOT NULL, lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL, acc DOUBLE PRECISION, PRIMARY KEY (user_id, ts_ms))",
]


def connect(url: str | None = None) -> "KazeDB":
    url = url or "sqlite:///:memory:"
    if url.startswith(("postgres://", "postgresql://")): return PostgresDB(url)
    if url.startswith("sqlite:///"): return SQLiteDB(url[len("sqlite:///"):])
    raise ValueError("database url must start with sqlite:/// or postgresql://")


class KazeDB:
    ph = "?"
    def _run(self, sql, params=(), fetch=None): raise NotImplementedError
    def close(self): pass

    def _sql(self, sql): return sql.replace("?", self.ph)

    def init_schema(self):
        for s in SCHEMA: self._run(s)

    # ---- documents
    def put_user(self, doc):
        self._run("INSERT INTO kaze_users (id, doc, updated_ms) VALUES (?,?,?) ON CONFLICT (id) DO UPDATE SET doc = excluded.doc, updated_ms = excluded.updated_ms",
                  (doc["id"], json.dumps(doc), int(time.time() * 1000)))

    def get_user(self, uid):
        r = self._run("SELECT doc FROM kaze_users WHERE id = ?", (uid,), "one"); return json.loads(r[0]) if r else None

    def list_users(self, limit=200):
        return [json.loads(r[0]) for r in self._run("SELECT doc FROM kaze_users ORDER BY updated_ms DESC LIMIT ?", (limit,), "all")]

    def put_session(self, doc):
        self._run("INSERT INTO kaze_sessions (id, user_id, status, doc, updated_ms) VALUES (?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET status = excluded.status, doc = excluded.doc, updated_ms = excluded.updated_ms",
                  (doc["id"], doc["user_id"], doc["status"], json.dumps(doc), int(time.time() * 1000)))

    def get_session(self, sid):
        r = self._run("SELECT doc FROM kaze_sessions WHERE id = ?", (sid,), "one"); return json.loads(r[0]) if r else None

    def active_sessions(self):
        return [json.loads(r[0]) for r in self._run("SELECT doc FROM kaze_sessions WHERE status <> 'RESOLVED' ORDER BY updated_ms", (), "all")]

    def user_sessions(self, uid, limit=20):
        return [json.loads(r[0]) for r in self._run("SELECT doc FROM kaze_sessions WHERE user_id = ? ORDER BY updated_ms DESC LIMIT ?", (uid, limit), "all")]

    # ---- append-only event log (the audit trail and the evidence)
    def add_event(self, ev):
        self._run("INSERT INTO kaze_events (ts_ms, id, session_id, user_id, kind, vis, payload) VALUES (?,?,?,?,?,?,?)",
                  (int(ev["ts"] * 1000), ev["id"], ev.get("session_id"), ev.get("user_id"), ev["kind"], ev["vis"], json.dumps(ev["payload"])))

    @staticmethod
    def _ev(r): return {"ts": r[0] / 1000.0, "id": r[1], "session_id": r[2], "user_id": r[3], "kind": r[4], "vis": r[5], "payload": json.loads(r[6])}

    def events(self, session_id=None, user_id=None, vis=None, kind=None, limit=500):
        where, p = [], []
        if session_id: where.append("session_id = ?"); p.append(session_id)
        if user_id: where.append("user_id = ?"); p.append(user_id)
        if kind: where.append("kind = ?"); p.append(kind)
        if vis: where.append("vis LIKE ?"); p.append(f"%{vis}%")
        sql = "SELECT ts_ms, id, session_id, user_id, kind, vis, payload FROM kaze_events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts_ms DESC, id DESC LIMIT ?"
        return [self._ev(r) for r in reversed(self._run(sql, tuple(p) + (limit,), "all"))]

    # ---- location samples (short-lived by design)
    def add_location(self, uid, ts, lat, lon, acc):
        self._run("INSERT INTO kaze_locations (user_id, ts_ms, lat, lon, acc) VALUES (?,?,?,?,?) ON CONFLICT (user_id, ts_ms) DO NOTHING", (uid, int(ts * 1000), lat, lon, acc))

    def latest_location(self, uid):
        r = self._run("SELECT ts_ms, lat, lon, acc FROM kaze_locations WHERE user_id = ? ORDER BY ts_ms DESC LIMIT 1", (uid,), "one")
        return {"ts": r[0] / 1000.0, "lat": r[1], "lon": r[2], "acc": r[3]} if r else None

    def count_locations(self, uid): return self._run("SELECT COUNT(*) FROM kaze_locations WHERE user_id = ?", (uid,), "one")[0]

    def purge_locations(self, uid=None, before=None):
        if uid and before is None: self._run("DELETE FROM kaze_locations WHERE user_id = ?", (uid,))
        elif before is not None: self._run("DELETE FROM kaze_locations WHERE ts_ms < ?" + (" AND user_id = ?" if uid else ""), (int(before * 1000),) + ((uid,) if uid else ()))

    def delete_user(self, uid):
        """Erase everything held about a user: documents, events, location samples."""
        for t in ("kaze_events", "kaze_locations", "kaze_sessions"): self._run(f"DELETE FROM {t} WHERE user_id = ?", (uid,))
        self._run("DELETE FROM kaze_users WHERE id = ?", (uid,))

    def ping(self):
        try: self._run("SELECT 1", (), "one"); return True
        except Exception: return False


class SQLiteDB(KazeDB):
    def __init__(self, path):
        if path != ":memory:": os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:": self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")                 # timers must survive a crash
        self.init_schema()

    def _run(self, sql, params=(), fetch=None):
        with self._lock:
            cur = self.db.execute(self._sql(sql), params)
            return cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None

    def close(self): self.db.close()


class PostgresDB(KazeDB):
    ph = "%s"
    def __init__(self, url):
        import psycopg
        self._psycopg, self.url, self.timescale, self._lock = psycopg, url, "off", threading.RLock()
        self.conn = self._open(); self.init_schema()
        if os.getenv("KAZE_TIMESCALE", "1") != "0": self._try_timescale()

    def _open(self): return self._psycopg.connect(self.url, autocommit=True, connect_timeout=10)

    def _run(self, sql, params=(), fetch=None):
        with self._lock:
            for attempt in (0, 1):
                try:
                    with self.conn.cursor() as cur:
                        cur.execute(self._sql(sql), params)
                        return cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
                except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
                    if attempt: raise
                    try: self.conn.close()
                    except Exception: pass
                    self.conn = self._open()                       # re-open once, then retry the statement

    def _try_timescale(self):
        """On Tiger Data / TimescaleDB, turn the event log into a hypertable. Anything else: plain Postgres, still fully working."""
        try:
            self._run("CREATE EXTENSION IF NOT EXISTS timescaledb")
            self._run("SELECT create_hypertable('kaze_events', 'ts_ms', chunk_time_interval => 86400000, if_not_exists => TRUE)", (), "one")
            self.timescale = "hypertable"
        except Exception as e:
            self.timescale = f"plain postgres ({type(e).__name__})"

    def close(self):
        try: self.conn.close()
        except Exception: pass
