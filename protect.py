# -*- coding: utf-8 -*-
"""
protect.py - JaatFlix protection, glue between guard.py (same engine as the anime site) and FastAPI.

What it does
  * Signed links: every /p (video proxy), /sub and /dl link carries a token t = signed(payload, url).
    The proxy no longer fetches ANY url a visitor types (that was an open proxy): only a url this
    server itself handed out, with a valid signature, before it expires.            (always on)
  * Sessions: a link is bound to the visitor's IP + a session id; the session ends after SESS_IDLE s
    without any request (every /p request is the heartbeat).  Another IP / expired / idle = violation.
  * PROTECT mode (env PROTECT = off | log | on, admin can change it):  off = nothing,
    log = dry-run (only writes what it WOULD block), on = really blocks.  Default: log.
  * Rate limits per IP + device, guest limit (GUEST_MAX videos / 24 h without sign-in), max live
    streams per IP, "open the title page first", sign-in brute-force limit, bans, bot score,
    VIP keys, download rule (signed-in only, DL_MAX at once).
Nothing in here may break the site: guard.py swallows its own errors.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from urllib.parse import quote, urlparse

os.environ.setdefault("SESS_MAX_MIN", "300")      # a movie can be long: a link lives 5 h (no renew step on this site)
import guard  # noqa: E402

LINK_KINDS = ("s", "d")
DL_LIFE = 600
_secret = None


def _get_secret():
    global _secret
    if _secret:
        return _secret
    s = os.environ.get("LINK_SECRET", "")
    if not s:
        try:
            import accounts
            s = accounts.kv_get("link_secret") or ""
            if not s:
                s = secrets.token_hex(24)
                accounts.kv_set("link_secret", s)
        except Exception:
            s = secrets.token_hex(24)             # DB down: links simply stop working after a restart
    _secret = s.encode()
    return _secret


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sig(payload, u):
    return _b64(hmac.new(_get_secret(), (payload + "|" + u).encode("utf-8", "ignore"), hashlib.sha256).digest())[:30]


# ------------------------------------------------------------ request context ----
def ctx(request):
    """ip / device id / vip name of this request (cached on request.state)."""
    st = request.state
    c = getattr(st, "pc", None)
    if c is None:
        peer = request.client.host if request.client else ""
        ip = guard.client_ip(request.headers, peer)
        dev = guard.device_id(request.headers)
        qs = {"vk": [request.query_params.get("vk", "")]}
        c = {"ip": ip, "dev": dev, "vip": guard.is_vip(request.headers, qs, ip)}
        st.pc = c
    return c


# ------------------------------------------------------------ safe url ----
def safe_url(u):
    """http(s) only, no localhost / private / link-local literal hosts."""
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        h = p.hostname.lower()
        if h in ("localhost",) or h.endswith(".local") or h.endswith(".internal"):
            return False
        try:
            a = ipaddress.ip_address(h)
            if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


# ------------------------------------------------------------ signed links ----
def new_session(c):
    """Start a session for a player (None when the guard is off)."""
    return guard.sess_open(c["ip"], dev=c["dev"])


def token(u, c, kind="s", sess=None, exp=None):
    now = time.time()
    if sess:
        sid, ipk, e = sess["sid"], sess["ipk"], sess["exp"]
    else:
        sid, ipk, e = "-", guard.ip_key(c["ip"]), now + guard.SESS_MAX
    if kind == "d":
        e = now + DL_LIFE
    if exp:
        e = exp
    payload = _b64(json.dumps({"k": kind, "s": sid, "i": ipk, "e": round(e)}, separators=(",", ":")).encode())
    return payload + "." + _sig(payload, u)


def link(u, c, sess=None, kind="s", path="/p"):
    """A signed same-site link to url u."""
    return "%s?u=%s&t=%s" % (path, quote(u, safe=""), token(u, c, kind, sess))


def link_like(u, c, t_old):
    """A link for a url inside a playlist: same session / expiry as the playlist's own link."""
    p = _parse(t_old, "")
    sess = None
    if p:
        sess = {"sid": p["s"], "ipk": p["i"], "exp": p["e"]}
    return link(u, c, sess)


def _parse(t, u, check_sig=False):
    try:
        payload, sig = (t or "").split(".", 1)
        if check_sig and not hmac.compare_digest(sig, _sig(payload, u)):
            return None
        d = json.loads(_unb64(payload))
        return {"k": d["k"], "s": d["s"], "i": d["i"], "e": float(d["e"])}
    except Exception:
        return None


def verify(c, u, t, want="s"):
    """-> (ok, reason).  Signature + expiry always count; IP / kind / idle follow PROTECT mode."""
    p = _parse(t, u, check_sig=True)
    if not p or p["k"] not in LINK_KINDS:
        return False, "bad"
    if time.time() > p["e"]:
        return False, "expired"
    why = ""
    if p["s"] == "-":
        if p["k"] != want:
            why = "kind"
        elif guard.ip_key(c["ip"]) != p["i"]:
            why = "ip"
    else:
        why = guard.sess_check({"kind": p["k"], "sid": p["s"], "ipk": p["i"], "exp": p["e"]}, c["ip"], want, beat=(want == "s"))
    if why:
        if guard.violation("link_" + why, c["ip"], "link refused (%s)" % why, dev=c["dev"]):
            return False, why
    return True, ""


# ------------------------------------------------------------ gates ----
def quiet():
    """What a banned visitor gets: an empty, boring answer (nothing to learn from)."""
    return {}


def banned(c):
    return (not c["vip"]) and guard.mode() != "off" and bool(guard.is_banned(c["ip"], c["dev"]))


BROWSE = ("/home", "/more", "/filter", "/browse", "/ranking", "/tab/", "/search", "/midnight", "/meta/filters")


def middleware_check(request):
    """Run before every request.  -> None to continue, or (status, json) to answer at once."""
    path = request.url.path
    c = ctx(request)
    if c["vip"] or guard.mode() == "off":
        return None
    guard.note(c["ip"])
    if path in ("/", "/admin", "/app.webmanifest", "/favicon.ico") or path.startswith("/icon-") or path == "/logo.png":
        return None
    if banned(c):
        return 200, quiet()
    if path.startswith(BROWSE):
        if guard.rate("browse", c["ip"], c["dev"], 60, 120, 120, detail=path):
            return 429, {"error": "Too many requests. Please wait a minute."}
    elif path.startswith("/detail/"):
        guard.track("meta", c["ip"], c["dev"])
        if guard.rate("detail", c["ip"], c["dev"], 60, 60, 60, detail="detail"):
            return 429, {"error": "Too many requests. Please wait a minute."}
    elif path.startswith("/api/stream/"):
        if guard.rate("stream", c["ip"], c["dev"], 60, 12, 12, detail="stream"):
            return 429, {"error": "Too many requests. Please wait a minute."}
    return None


def after_detail(request, slug):
    c = ctx(request)
    guard.open_mark(c["ip"], c["dev"], slug)


def stream_gate(request, user, subject_id, detail_path):
    """Before a stream link is made.  -> (status, json) to refuse, or None."""
    c = ctx(request)
    if c["vip"] or guard.mode() == "off":
        return None
    ip, dev = c["ip"], c["dev"]
    guard.track("stream", ip, dev)
    if not guard.was_opened(ip, dev, detail_path):
        if guard.violation("no_detail", ip, "stream without opening the title page (%s)" % detail_path, dev=dev):
            return 403, {"error": "Open the title page first.", "open": True}
    if not user and guard.guest_start(ip, dev, str(subject_id)):
        return 403, {"error": "Free limit reached. Please sign in to keep watching.", "login": True}
    if guard.conc_check(ip, dev):
        return 429, {"error": "Too many streams at the same time on this network."}
    if guard.check_score(ip, dev):
        return 200, quiet()
    return None


def login_gate(request, username):
    """Before a sign-in / sign-up try.  -> (status, json) to refuse, or None."""
    c = ctx(request)
    if c["vip"] or guard.mode() == "off":
        return None
    why = guard.login_why(c["ip"], username)
    if why and guard.violation("login_many", c["ip"], why, dev=c["dev"]):
        return 429, {"error": "Too many sign-in attempts. Please try again in 10 minutes."}
    if guard.login_hard(c["ip"], username):
        return 429, {"error": "Too many sign-in attempts. Please try again in 10 minutes."}
    return None


def login_note(request, username, ok):
    c = ctx(request)
    guard.login_note(c["ip"], username, ok)


# ------------------------------------------------------------ admin ----
def admin_action(action, body, request):
    """Protection actions for /api/admin/{action}.  -> dict, or None if not a protection action."""
    c = ctx(request)
    if action == "protect":
        return guard.info(request.headers, request.client.host if request.client else "")
    if action == "mode":
        return {"ok": guard.set_mode(str(body.get("mode", "")))}
    if action == "ban":
        return {"ok": True, "key": guard.ban(str(body.get("key", "")), str(body.get("why", "admin")), float(body.get("hours") or 0))}
    if action == "unban":
        return {"ok": guard.unban(str(body.get("key", "")))}
    if action == "bans":
        return {"bans": guard.ban_list()}
    if action == "bots":
        return {"bots": guard.bot_list()}
    if action == "vip_list":
        return {"keys": guard.vip_list()}
    if action == "vip_new":
        return guard.vip_new(str(body.get("name", "")))
    if action == "vip_del":
        return {"ok": guard.vip_del(str(body.get("id", "")))}
    return None


_dls = {}


def dl_begin(ip):
    """A download starts -> (id, how many are running from this network now)."""
    now = time.time()
    k = guard.ip_key(ip)
    lst = [(i, t) for i, t in _dls.get(k, []) if now - t < 10800]      # a leaked entry dies after 3 h
    did = secrets.token_hex(6)
    lst.append((did, now))
    _dls[k] = lst
    return did, len(lst)


def dl_end(ip, did):
    k = guard.ip_key(ip)
    lst = [(i, t) for i, t in _dls.get(k, []) if i != did]
    if lst:
        _dls[k] = lst
    else:
        _dls.pop(k, None)


def start():
    try:
        guard.start()
    except Exception as e:
        print("[protect] guard did not start:", e, flush=True)
