"""JaatFlix accounts, sessions, watch progress, My List.

Same design as watch-anime's accounts.py, but a separate database.
Storage: SQL_URL (permanent SQL service) if set, otherwise a local SQLite
file (DB_PATH, default jaatflix.db - may be wiped on redeploy).
Titles are identified by MovieBox subject_id (sid); My List and Continue
Watching keep a small JSON snapshot (slug, name, poster) because JaatFlix
has no local catalog.
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time
import urllib.request

# Cloudflare (workers.dev) blocks the default "Python-urllib" User-Agent with 403 -> send a normal one
_HDR = {"Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json"}

SQL_URL = os.environ.get("SQL_URL", "").rstrip("/")
if SQL_URL and not SQL_URL.endswith("/sql"):
    SQL_URL += "/sql"
DB_PATH = os.environ.get("DB_PATH", "jaatflix.db")
SESSION_DAYS = 365
_lock = threading.Lock()
_local = None

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE, salt TEXT, pw TEXT, created REAL)",
    "CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, seen REAL)",
    "CREATE TABLE IF NOT EXISTS progress(user_id INTEGER, sid TEXT, season INTEGER, episode INTEGER, pos REAL, dur REAL, updated REAL, meta TEXT, PRIMARY KEY(user_id, sid))",
    "CREATE TABLE IF NOT EXISTS favs(user_id INTEGER, sid TEXT, added REAL, meta TEXT, PRIMARY KEY(user_id, sid))",
    "CREATE TABLE IF NOT EXISTS seen(user_id INTEGER, sid TEXT, eps TEXT, PRIMARY KEY(user_id, sid))",
    "CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS tmdb(slug TEXT PRIMARY KEY, v TEXT)",  # reserved for TMDB posters
    "CREATE TABLE IF NOT EXISTS signups(ipk TEXT, ts REAL)",  # one row per account made, per IP (survives restarts)
]


def _rows(resp):
    """Accept the common response shapes: {rows:[[..]]} or {rows:[{..}]}."""
    if isinstance(resp, dict):
        for k in ("rows", "results", "result", "data"):
            if k in resp:
                resp = resp[k]; break
        else:
            return []
    if isinstance(resp, dict):
        resp = resp.get("rows", [])
    return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in resp or []]


def q(sql, params=()):
    """Run one statement, return list of row tuples."""
    global _local
    if SQL_URL:
        req = urllib.request.Request(SQL_URL, json.dumps({"sql": sql, "params": list(params)}).encode(),
                                     _HDR)
        with urllib.request.urlopen(req, timeout=15) as r:
            return _rows(json.loads(r.read() or b"{}"))
    with _lock:
        if _local is None:
            _local = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = _local.execute(sql, params)
        _local.commit()
        return cur.fetchall() if cur.description else []


def init():
    if SQL_URL:
        body = json.dumps({"script": ";\n".join(SCHEMA)}).encode()
        urllib.request.urlopen(urllib.request.Request(SQL_URL, body, _HDR), timeout=15).read()
    else:
        for s in SCHEMA:
            q(s)


try:
    init()
except Exception as e:
    print("[accounts] init failed:", e, flush=True)


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def _th(tok):  # only a hash of the token is stored
    return hashlib.sha256(tok.encode()).hexdigest()


def _session(uid):
    tok = secrets.token_urlsafe(32)
    q("INSERT INTO sessions VALUES(?,?,?)", (_th(tok), uid, time.time()))
    return tok


SIGNUP_MAX = int(os.environ.get("SIGNUP_MAX", "3") or 3)          # accounts per IP ...
SIGNUP_HOURS = float(os.environ.get("SIGNUP_HOURS", "24") or 24)  # ... in this many hours


def signup_count(ipk):
    """How many accounts this IP made lately (from the DB). Never raises (0 on error)."""
    try:
        r = q("SELECT COUNT(*) FROM signups WHERE ipk=? AND ts>?", (ipk, time.time() - SIGNUP_HOURS * 3600))
        return int(r[0][0]) if r else 0
    except Exception:
        return 0


def signup(username, password, ipk=""):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username or ""):
        return None, "Username must be 3-20 letters, numbers or underscores."
    if len(password or "") < 6:
        return None, "Password must be at least 6 characters."
    username = username.lower()
    if q("SELECT 1 FROM users WHERE username=?", (username,)):
        return None, "That username is already taken."
    salt = secrets.token_hex(16)
    try:
        q("INSERT INTO users(username,salt,pw,created) VALUES(?,?,?,?)", (username, salt, _hash(password, salt), time.time()))
    except Exception:
        return None, "That username is already taken."
    rows = q("SELECT id FROM users WHERE username=?", (username,))
    if ipk:
        try:
            q("INSERT INTO signups(ipk,ts) VALUES(?,?)", (ipk, time.time()))
            q("DELETE FROM signups WHERE ts<?", (time.time() - 30 * 86400,))
        except Exception:
            pass
    return _session(rows[0][0]), None


def login(username, password):
    rows = q("SELECT id,salt,pw FROM users WHERE username=?", ((username or "").lower(),))
    if not rows or not hmac.compare_digest(str(rows[0][2]), _hash(password or "", rows[0][1])):
        return None, "Wrong username or password."
    return _session(rows[0][0]), None


_ucache = {}  # token hash -> (expires, user)


def user_for(token):
    if not token:
        return None
    hit = _ucache.get(_th(token))
    if hit and hit[0] > time.time():
        return hit[1]
    rows = q("SELECT u.id,u.username,s.seen FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (_th(token),))
    if not rows:
        return None
    uid, name, seen = rows[0]
    now = time.time()
    if now - float(seen) > SESSION_DAYS * 86400:
        q("DELETE FROM sessions WHERE token=?", (_th(token),))
        return None
    if now - float(seen) > 86400:  # sliding expiry
        q("UPDATE sessions SET seen=? WHERE token=?", (now, _th(token)))
    user = {"id": int(uid), "username": name}
    if len(_ucache) > 2000:
        _ucache.clear()
    _ucache[_th(token)] = (now + 120, user)
    return user


def logout(token):
    _ucache.clear()
    q("DELETE FROM sessions WHERE token=?", (_th(token),))


def _meta(m):
    m = m or {}
    return json.dumps({k: str(m.get(k) or "")[:300] for k in ("slug", "name", "poster_url", "rating")}, separators=(",", ":"))


def _load(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


# ---- watch progress (latest episode per title, 40 titles max) ----
def save_progress(uid, sid, season, episode, pos, dur, meta=None):
    q("INSERT OR REPLACE INTO progress VALUES(?,?,?,?,?,?,?,?)",
      (uid, sid, int(season or 0), int(episode or 0), float(pos or 0), float(dur or 0), time.time(), _meta(meta)))
    q("DELETE FROM progress WHERE user_id=? AND sid NOT IN (SELECT sid FROM progress WHERE user_id=? ORDER BY updated DESC LIMIT 40)", (uid, uid))


def get_progress(uid):
    out = []
    for r in q("SELECT sid,season,episode,pos,dur,meta FROM progress WHERE user_id=? ORDER BY updated DESC LIMIT 40", (uid,)):
        m = _load(r[5])
        out.append(dict(sid=r[0], season=int(r[1]), episode=int(r[2]), pos=float(r[3]), dur=float(r[4]),
                        slug=m.get("slug", ""), name=m.get("name", ""), poster_url=m.get("poster_url", "")))
    return out


# ---- My List ----
def set_fav(uid, sid, on, meta=None):
    if on:
        q("INSERT OR REPLACE INTO favs VALUES(?,?,?,?)", (uid, sid, time.time(), _meta(meta)))
        q("DELETE FROM favs WHERE user_id=? AND sid NOT IN (SELECT sid FROM favs WHERE user_id=? ORDER BY added DESC LIMIT 300)", (uid, uid))
    else:
        q("DELETE FROM favs WHERE user_id=? AND sid=?", (uid, sid))


def get_favs(uid):
    out = []
    for r in q("SELECT sid,meta FROM favs WHERE user_id=? ORDER BY added DESC LIMIT 300", (uid,)):
        m = _load(r[1])
        out.append({"subject_id": r[0], "slug": m.get("slug", ""), "name": m.get("name", ""),
                    "poster_url": m.get("poster_url", ""), "rating": m.get("rating", "")})
    return out


# ---- watched episodes ----
def get_seen(uid, sid):
    r = q("SELECT eps FROM seen WHERE user_id=? AND sid=?", (uid, sid))
    return [x for x in (r[0][0] or "").split(",") if x] if r else []


def mark_seen(uid, sid, season, episode):
    key = "%d:%d" % (int(season or 0), int(episode or 0))
    cur = get_seen(uid, sid)
    if key not in cur:
        q("INSERT OR REPLACE INTO seen VALUES(?,?,?)", (uid, sid, ",".join(cur + [key])[-6000:]))


def change_password(uid, token, old, new):
    if len(new or "") < 6:
        return "New password must be at least 6 characters."
    r = q("SELECT salt,pw FROM users WHERE id=?", (uid,))
    if not r or not hmac.compare_digest(str(r[0][1]), _hash(old or "", r[0][0])):
        return "Current password is incorrect."
    salt = secrets.token_hex(16)
    _ucache.clear()
    q("UPDATE users SET salt=?, pw=? WHERE id=?", (salt, _hash(new, salt), uid))
    q("DELETE FROM sessions WHERE user_id=? AND token<>?", (uid, _th(token)))  # sign out other devices
    return None


# ---- admin helpers (used by /api/admin/* when ADMIN_KEY env is set) ----
def admin_stats():
    n = q("SELECT COUNT(*) FROM users")[0][0]
    rows = q("SELECT u.username,u.created,(SELECT MAX(seen) FROM sessions WHERE user_id=u.id),"
             "(SELECT COUNT(*) FROM progress WHERE user_id=u.id) FROM users u ORDER BY u.created DESC LIMIT 500")
    return {"total": int(n), "users": [dict(username=r[0], created=r[1], last_seen=r[2], history=int(r[3])) for r in rows]}


def _uid(username):
    r = q("SELECT id FROM users WHERE username=?", ((username or "").lower(),))
    return r[0][0] if r else None


def admin_set_password(username, new_pw):
    if len(new_pw or "") < 6:
        return "Password must be at least 6 characters."
    uid = _uid(username)
    if uid is None:
        return "User not found."
    salt = secrets.token_hex(16)
    _ucache.clear()
    q("UPDATE users SET salt=?, pw=? WHERE id=?", (salt, _hash(new_pw, salt), uid))
    q("DELETE FROM sessions WHERE user_id=?", (uid,))
    return None


def admin_delete(username):
    uid = _uid(username)
    if uid is None:
        return "User not found."
    _ucache.clear()
    for t in ("sessions", "progress", "favs", "seen"):
        q("DELETE FROM %s WHERE user_id=?" % t, (uid,))
    q("DELETE FROM users WHERE id=?", (uid,))
    return None


# ---- kv + TMDB cache (reserved for the poster step) ----
def kv_get(k):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return r[0][0] if r else None


def kv_set(k, v):
    q("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))


def tmdb_get(slug):
    r = q("SELECT v FROM tmdb WHERE slug=?", (slug,))
    return _load(r[0][0]) if r else None


def tmdb_set(slug, obj):
    q("INSERT OR REPLACE INTO tmdb VALUES(?,?)", (slug, json.dumps(obj, separators=(",", ":"))))
