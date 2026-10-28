"""
JaatFlix API (MovieBox backend)
- token auto-refresh; the token is also sent with stream calls
- banner items are normalized (the hero image used to break)
- India headers (Asia/Kolkata + IN)
- /p = video proxy: sends the correct Referer to the CDN and rewrites m3u8 playlists
  (direct browser playback used to stall at 0:00 for this reason)
- Optional: env IN_PROXY=http://user:pass@indian-proxy:port routes all calls through an Indian IP
"""
import asyncio, hmac, json, os, re, time
from urllib.parse import quote, urljoin
import httpx
import accounts
import captcha
import protect
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(title="JaatFlix API", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()], allow_methods=["*"], allow_headers=["*", "X-Device", "X-VIP-Key", "X-Captcha", "X-Admin-Key"])
protect.start()

_DEBUG = ("/lab", "/shape", "/diag", "/discover", "/peek", "/raw", "/probe")   # developer tools: admin key only


# ---- anti-leak: nothing from MovieBox (hosts, raw errors, image CDN) may reach the browser; every byte goes through this server ----
_IMG_EXT = re.compile(r"\.(jpe?g|png|webp|avif|gif|bmp)(\?|#|$)", re.I)
_IMG_KEYS = {"cover", "image", "avatar", "stills", "thumbnail", "poster", "poster_url", "banner", "icon", "logo", "backdrop", "stillPath"}
_UP_HOST = re.compile(r"(moviebox|aoneroom|netfilm|hakunaymatata|mbcdn|movieboxpro|h5-api)", re.I)
_DROP_KEYS = {"_debug", "_domain", "tried", "via", "_http", "_body", "_err"}
_SKIP_CLEAN = ("/p", "/sub", "/dl", "/img", "/lab", "/shape", "/diag", "/discover", "/peek", "/raw", "/probe")


def _pimg(u):
    """Signed same-site link for an upstream picture (served by /img, cached)."""
    return "/img?u=%s&t=%s" % (quote(u, safe=""), protect._sig("img", u))


def _clean(o, key=""):
    if isinstance(o, dict):
        return {k: _clean(v, key if (k == "url" and key in _IMG_KEYS) else k) for k, v in o.items() if k not in _DROP_KEYS}
    if isinstance(o, list):
        return [_clean(v, key) for v in o]
    if isinstance(o, str) and o[:4].lower() == "http":
        if _IMG_EXT.search(o) or key in _IMG_KEYS:
            return _pimg(o) if protect.safe_url(o) else ""
        if _UP_HOST.search(o):
            return ""
    return o


def _is_admin(request):
    k = request.headers.get("x-admin-key") or request.query_params.get("key") or ""
    ak = os.getenv("ADMIN_KEY", "")
    return bool(ak) and hmac.compare_digest(k.encode(), ak.encode())


_IMGC = {}   # tiny in-memory picture cache: url -> (time, bytes, content-type)


@app.get("/img")
async def img(u: str, t: str = ""):
    if not t or not hmac.compare_digest(t, protect._sig("img", u)) or not protect.safe_url(u):
        return Response("not found", status_code=404, headers={"Cache-Control": "no-store"})
    hit = _IMGC.get(u)
    if hit and time.time() - hit[0] < 21600:
        return Response(hit[1], media_type=hit[2], headers={"Cache-Control": "public, max-age=604800, immutable"})
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, **CK) as c:
            r = await c.get(u, headers={"User-Agent": UA, "Referer": _ref["v"], "Accept": "image/*,*/*;q=0.8"})
    except Exception:
        return Response("bad gateway", status_code=502, headers={"Cache-Control": "no-store"})
    ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code != 200 or not ct.startswith("image/") or len(r.content) > 8_000_000:
        return Response("not found", status_code=404, headers={"Cache-Control": "no-store"})
    if len(_IMGC) > 400:
        for k_ in sorted(_IMGC, key=lambda x: _IMGC[x][0])[:100]:
            _IMGC.pop(k_, None)
    _IMGC[u] = (time.time(), r.content, ct)
    return Response(r.content, media_type=ct, headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.middleware("http")
async def _protect_mw(request: Request, call_next):
    path = request.url.path
    if path.startswith(_DEBUG):
        if not _is_admin(request):
            return JSONResponse({"error": "not found"}, status_code=404)
    try:
        r = protect.middleware_check(request)
    except Exception:
        r = None
    if r is not None:
        return JSONResponse(r[1], status_code=r[0])
    resp = await call_next(request)
    try:
        if path.startswith(_SKIP_CLEAN) or "application/json" not in resp.headers.get("content-type", "") or _is_admin(request):
            return resp
        body = b"".join([ch async for ch in resp.body_iterator])
        hd = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-type")}
        try:
            out = json.dumps(_clean(json.loads(body)), ensure_ascii=False).encode()
        except Exception:
            out = body
        return Response(out, status_code=resp.status_code, headers=hd, media_type="application/json")
    except Exception:
        return resp

API = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
XCI = '{"timezone":"Asia/Kolkata"}'
PROXY = os.getenv("IN_PROXY") or None
CK = {"proxy": PROXY} if PROXY else {}

H = {
    "User-Agent": UA, "Referer": "https://moviebox.ph/", "Origin": "https://moviebox.ph",
    "Accept": "application/json", "Content-Type": "application/json",
    "X-Request-Lang": "en", "X-Client-Info": XCI, "Accept-Language": "en-IN,en;q=0.9",
}
HOST = os.getenv("MB_HOST", "moviebox.ph")          # /home?host=... (mirror site)
HBASE = dict(H)  # without the India headers (used only by /probe)
# Confirmed via /probe: these IP headers return Indian content (now the default; no env var needed)
IN_IP = os.getenv("IN_FAKE_IP", "49.205.120.45")
INH = {"X-Forwarded-For": IN_IP, "X-Real-IP": IN_IP, "CF-Connecting-IP": IN_IP, "CF-IPCountry": "IN"}
H.update(INH)
H.update(json.loads(os.getenv("MB_HEADERS", "{}")))  # extra headers can be supplied as JSON via the environment
_tok = {"v": "", "t": 0}
_ref = {"v": "https://netfilm.world/"}


def _grab(r):
    xu = r.headers.get("x-user")
    if xu:
        try:
            t = json.loads(xu).get("token")
            if t:
                _tok["v"], _tok["t"] = t, time.time()
        except Exception:
            pass


async def ensure_token(force=False):
    if not force and _tok["v"] and time.time() - _tok["t"] < 900:
        return _tok["v"]
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, **CK) as c:
        try:
            _grab(await c.get(f"{API}/home?host={HOST}", headers=H))
        except Exception:
            pass
    return _tok["v"]


async def call(url, payload=None, hdr=None, retry=True):
    await ensure_token()
    h = dict(hdr or H)
    if _tok["v"]:
        h["Authorization"] = "Bearer " + _tok["v"]  # this was previously missing from player calls
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            r = await (c.post(url, headers=h, content=json.dumps(payload)) if payload is not None else c.get(url, headers=h))
        except Exception as e:
            return {"_err": str(e)}
        _grab(r)
        if r.status_code == 429:  # rate limit: wait and retry (up to 2 times)
            for wait in (2.5, 5):
                await asyncio.sleep(wait)
                try:
                    r = await (c.post(url, headers=h, content=json.dumps(payload)) if payload is not None else c.get(url, headers=h))
                except Exception as e:
                    return {"_err": str(e)}
                if r.status_code != 429:
                    break
            _grab(r)
        if r.status_code in (401, 403) and retry:
            await ensure_token(True)
            return await call(url, payload, hdr, retry=False)
        if r.status_code != 200:
            return {"_http": r.status_code, "_body": r.text[:200]}
        try:
            return r.json()
        except Exception:
            return {}


# ---------------- logo / icons (embedded, so no separate file is needed) ----------------
import base64
_ICONS = {
    "icon-192.png": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAE4UlEQVR4nO3dwY0USRCF4ZwVtzlwXgxAWhvWgLUZA7ABCQMWN/ZQUqm2B4buflUZ70X+3xmhypi/I4sZBC+vrx8H8Kw/qh8A2QgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSD5UP4CL73//+dCv//z1x0VPkuVl2X/m99Fi3rdsT2sFdG40v7JUTEsENKebt1YoqXlAVekc9c6oZ0AO3bzVsqRuAXmmc9Qso1bfB/KvZ4Q85P2abKDEr0qPVRQfUGI6R+kZZV9h6fWM/CMEB5Q++l30QSKvsOiJvyPxOsvbQF3rGZlHCwsoccQPiTtgUkBxw31O1jFjAsoaqyjosBkBBQ30LClHDggoZZSnizi4e0ARQ7yO//GtA/If3wTmQ/ANyHxwMzmPwjQg55GVsB2IaUBI4RiQ7aetludY7AK6ekyfvny79Pe/lGFDXgHNGdCnL99yM3JryCugmXIbsmIU0PzPVugqslpCRgFVCc3IhEtA5Z+qrIbKx7WzCMhkHFmryGRoFgFZCWrIQX1AJp+ko5RV5DC6+oBspWRUi4B+g4beVxyQwxL+LedVVD5ANtC9nDMqVBlQ+afnCYYN1Y6RDfQwVtERAT2JjDZlASXeX2+ZNFQ4TDaQavFVREDnWLYhAjrNmquoJqAeL0A/VZVR1UjZQJdYZxUR0FUWudEI6FrtMyKgGRo3VBBQ4zfod0xYRSWDZQNN1e9GI6ACnRoioBptVhEBVWrQEAEVS19FBGQhNyMCMpLYEAF5iVtFH6ofAP/z7z9/VT/CY9hARuLqGWwgE4npbAioWG46G66wSun1DDZQlQbpbAhotjbpbAoC+vz1x5p/JejqdEr+02fegSZptnh2XGGX65rOhoAu1DudDVfYVVaoZ1RtoN7v0SXplLxBD66wcy2ydY64wk6zYD2DDXSKNdPZlAXU4zXIJJ2qF6DBFaYwqacWV9gzSGdXGVDiLWaYTuH9NbjCHmJYT7niKyxlCdmmU7t+RnlA/mzTMUFAv0Q696h/Bypfwj8VUY/D6NhAtyLS8WERkMmrdFY6DutnOFxhm/JxUM9zLDZQrax03BgFNP8iC03HZ/0MnytsvtB63BhtoDFrCUWnY7V+xhgvr68fq5/hlsOfyDy51TM8rzDDMTnwHItjQAhiGpDnp62Q7UBMAxrGI5vPeRS+AQ3vwU1jPgTrgIb9+K7mf3z3gEbCEC8ScfCAgEbIKM+VcuSMgEbOQE8RdNiYgEbUWBVZx0wKaKQN9wlxBwwLaASO+H6JR3P8YeqdOv3MNTGdTd4G2uUO/Ub0QYIDGuGj36QfIfgKO0q8ztLT2TQJaJOSUY90NtlX2I2IL0zEQ96v1Qbaea6iZulsega0cyipZTe75gFtqjLqnc5miYB2c0paoZvdWgEdnRvTUtEcrRvQjUd7WraYGwQESavvA2E+AoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAgSAoKEgCAhIEgICBICgoSAICEgSAgIEgKChIAgISBICAiS/wAopWLEgEoGGwAAAABJRU5ErkJggg=="),
    "icon-512.png": base64.b64decode("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"),
}
_MANIFEST = {"name": "JaatFlix", "short_name": "JaatFlix", "start_url": "/", "display": "standalone",
             "background_color": "#0a0a0d", "theme_color": "#0a0a0d",
             "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                       {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}


def _icon(name):
    return Response(_ICONS[name], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-192.png")
async def icon192():
    return _icon("icon-192.png")


@app.get("/icon-512.png")
async def icon512():
    return _icon("icon-512.png")


@app.get("/logo.png")
async def logo_png():
    return _icon("icon-512.png")


@app.get("/favicon.ico")
async def favicon():
    return _icon("icon-192.png")


@app.get("/app.webmanifest")
async def webmanifest():
    return Response(json.dumps(_MANIFEST), media_type="application/manifest+json")


@app.get("/")
async def ui():
    return FileResponse("index.html", headers={"Cache-Control": "no-cache"})


def _item(s):
    s = s or {}
    t = s.get("title") or ""
    cn = s.get("corner")
    badge = cn if isinstance(cn, str) and cn else ("Hindi" if "[hindi]" in t.lower() else None)  # "Hindi" tag shown on the card
    return {"name": t or None, "poster_url": (s.get("cover") or {}).get("url"),
            "slug": s.get("detailPath"), "subject_id": s.get("subjectId"), "rating": s.get("imdbRatingValue"),
            "badge": badge, "type": s.get("subjectType"), "genre": s.get("genre"), "country": s.get("countryName"),
            "year": str(s.get("releaseDate") or "")[:4]}


# ===== MovieBox's real home / tabs / "All" (not search-based: MovieBox updates trending itself every day) =====
_TAB_KEYS = ("tabList", "tabs", "navList", "homeTabs", "topTabs", "navigation", "categoryList", "channelList")
_SUB_KEYS = ("tabs", "tabList", "subTabs", "categoryList", "categories", "filters", "genres")
_ID_KEYS = ("genreTopId", "rankingId", "rankId", "opId", "id", "url", "jumpUrl", "path", "link")


def _nm(d):
    return d.get("name") or d.get("title") or d.get("tabName") or d.get("categoryName") or d.get("label") or d.get("lanName")


def _tid(d):
    for k in ("tabId", "id", "categoryId", "rankingId", "opId", "path", "url"):
        if d.get(k) not in (None, ""):
            return str(d[k])
    return None


def _banner_items(op):
    items = []
    for b in (op.get("banner") or {}).get("items") or []:
        sub = b.get("subject") or {}
        items.append({
            "name": b.get("title") or sub.get("title"),
            "poster_url": (b.get("image") or {}).get("url") or (sub.get("cover") or {}).get("url"),
            "slug": b.get("detailPath") or sub.get("detailPath"),
            "subject_id": b.get("subjectId") or sub.get("subjectId"),
        })
    return [i for i in items if i["slug"]]


def _subtabs(op):
    """Tabs inside a section (Asian Drama / Western TV / Turkish Drama / New ...) as provided by MovieBox."""
    for k in _SUB_KEYS:
        v = op.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            tabs = []
            for t in v:
                n = _nm(t)
                if not n:
                    continue
                subs = t.get("subjects")
                img = t.get("image")
                tabs.append({"name": n, "id": _tid(t), "query": t.get("query") or "", "image": img.get("url") if isinstance(img, dict) else None,
                             "items": [_item(s) for s in subs] if isinstance(subs, list) else None})
            if tabs:
                return tabs
    return None


def _sections(d):
    out = []
    for op in (d.get("data") or {}).get("operatingList") or []:
        if op.get("type") == "BANNER":
            out.append({"section": "Banner", "items": _banner_items(op)})
            continue
        more = {k: op[k] for k in _ID_KEYS if op.get(k) not in (None, "")}
        out.append({"section": op.get("title") or op.get("type"),
                    "items": [i for i in (_item(s) for s in op.get("subjects") or []) if i["slug"]],
                    "more": more or None, "tabs": _subtabs(op),
                    "kind": "filter" if op.get("type") == "FILTER" else None})
    return out


def _toptabs(d):
    """Top-level tabs (Trending / Movie / Anime / ...) as provided in the MovieBox home response."""
    data = d.get("data") or {}
    for k in _TAB_KEYS:
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            tabs = [{"name": _nm(t), "id": _tid(t)} for t in v if _nm(t)]
            if tabs:
                return tabs
    return []


def _platforms(d):
    """Streaming services row (Netflix / Prime ...) = `platformList` of the home response."""
    out = []
    for p in (d.get("data") or {}).get("platformList") or []:
        if not isinstance(p, dict):
            continue
        n = p.get("name") or p.get("platform") or p.get("platformName") or p.get("title")
        if not n:
            continue
        img = p.get("image") or p.get("logo") or p.get("icon") or p.get("cover")
        if isinstance(img, dict):
            img = img.get("url")
        out.append({"name": n, "by": p.get("uploadBy") or "", "image": img if isinstance(img, str) else None})
    return out


def _sentinel(d):
    """MovieBox answers a guest's /subject/filter with total=1000000 and a junk default list (kids songs, music videos)."""
    x = (d.get("data") or {}) if isinstance(d, dict) else {}
    for src in (x, x.get("pager") if isinstance(x.get("pager"), dict) else {}):
        for k in ("totalCount", "total", "count"):
            if str(src.get(k)) == "1000000":
                return True
    return False


def _extract(d):
    x = d.get("data") if isinstance(d, dict) else None
    if isinstance(x, list):
        lst = x
    elif isinstance(x, dict):
        lst = next((x[k] for k in ("subjectList", "subjects", "items", "list", "contents", "records") if isinstance(x.get(k), list)), [])
    else:
        lst = []
    its = (_item(s.get("subject") if isinstance(s.get("subject"), dict) else s) for s in lst if isinstance(s, dict))
    return [i for i in its if i["slug"]]


def _home_url(tab="", page=0):
    u = f"{API}/home?host={HOST}"
    if tab:
        u += "&tabId=" + quote(tab, safe="")
    if page:
        u += f"&page={page}"
    return u


@app.get("/home")
async def home(tab: str = "", page: int = 0):
    d = await call(_home_url(tab, page))
    return {"sections": _sections(d), "tabs": _toptabs(d), "platforms": _platforms(d), "_debug": d.get("_http") or d.get("_err")}


@app.get("/more")
async def more(id: str = "", url: str = "", title: str = "", page: int = 1, per: int = 24):
    """Full list for 'All >' / a sub-tab. MovieBox's own endpoints are tried one by one."""
    cands = []
    if id:  # verified: ranking-list/content (id = genreTopId) pages correctly for every home row
        cands.append(f"{API}/ranking-list/content?id={quote(id, safe='')}&page={page}&perPage={per}")
    if url.startswith("/") and not url.startswith("//") and ".." not in url and "@" not in url and re.match(r"^/(ranking-list|subject|tab-operating|operating|home)\b", url):
        cands.append(f"{API}{url}{'&' if '?' in url else '?'}page={page}&perPage={per}")
    if re.search(r"trending", title, re.I):
        cands.append(f"{API}/subject/trending?page={max(page - 1, 0)}&perPage={per}")
    tried = []
    for u in cands:
        d = await call(u)
        its = _extract(d)
        tried.append({"url": u, "n": len(its), "err": d.get("_http") or d.get("_err")})
        if its:
            return {"items": its, "page": page, "via": u, "tried": tried}
    return {"items": [], "page": page, "tried": tried}


@app.get("/filter")
async def filter_list(query: str, page: int = 1, per: int = 24):
    """Category lists (All Movies / All Dramas / Punjabi / Bengali ...). `query` is the raw query string MovieBox supplies for each filter.
    The real API path is not documented, so several likely endpoints (POST and GET) are tried one by one."""
    from urllib.parse import parse_qsl, urlencode
    prm = dict(parse_qsl(query, keep_blank_values=True))
    prm.pop("type", None)
    try:
        ft = json.loads(prm.pop("filterType", "{}"))
    except Exception:
        ft = {}
    tab = prm.get("tabId", "")
    # The web API numbers its channels 1 = movies, 2 = TV, 4 = anime; the "Categories" widget uses the app's ids (2 / 5 / 8).
    chan = {"2": "1", "5": "2", "8": "4"}.get(tab, tab)
    # Verified by testing the real API: /subject/filter takes channelId + genre/country/year/sort/classify.
    # Language = classify ("Hindi dub" etc.); a separate `language` field is ignored.
    # total=1000000 is MovieBox's normal total, NOT junk, so no sentinel filtering is done.
    base = {"channelId": chan, "page": page, "perPage": per, "genre": "All", "country": "All", "year": "All", "sort": "ForYou", "classify": "All"}
    b1 = {**base, **{k: v for k, v in ft.items() if v not in (None, "")}}
    body = {**ft, "tabId": tab, "channelId": tab, "page": page, "perPage": per}
    flat = urlencode(body)
    cands = [("POST", f"{API}/subject/filter", b1),
             ("POST", f"{API}/subject/filter", {**b1, "country": "India"} if b1.get("country") == "All" else b1)]
    if cands[0][2] == cands[1][2]:
        cands = cands[:1]
    tried = []
    for method, u, payload in cands:
        d = await call(u, payload) if method == "POST" else await call(u)
        its = _extract(d)
        sen = False
        err = d.get("_http") or d.get("_err")
        row = {"m": method, "url": u[:140], "n": len(its), "err": err, "sentinel": sen,
               "chan": (payload or {}).get("channelId")}
        if not its and not err:
            row["head"] = json.dumps(d, ensure_ascii=False)[:300]
        tried.append(row)
        if its:
            return {"items": its, "page": page, "via": row, "tried": tried}
    return {"items": [], "page": page, "tried": tried}


_LAB_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MovieBox lab</title>
<style>body{font:14px system-ui,sans-serif;background:#0d0d10;color:#eee;margin:0;padding:12px}
button,select,input,textarea{font:inherit;background:#1d1d24;color:#eee;border:1px solid #333;border-radius:8px;padding:8px}
.row{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;align-items:center}
textarea{width:100%;box-sizing:border-box;height:120px;font-family:monospace;font-size:12px}
input.p{width:100%;box-sizing:border-box;font-family:monospace;font-size:12px}
pre{white-space:pre-wrap;word-break:break-all;background:#15151a;padding:10px;border-radius:8px;font-size:12px}
.go{background:#e50914;border-color:#e50914}h3{margin:4px 0}small{color:#999}</style></head><body>
<h3>MovieBox lab</h3><small>Requests go from your server to MovieBox (token added automatically).</small>
<div class="row" id="pre"></div>
<small>Home rows (tap = open that row's full list via genreTopId):</small>
<div class="row" id="ids"></div>
<div class="row"><select id="m"><option>GET</option><option>POST</option></select><button class="go" id="run">Run</button><label><input type="checkbox" id="rawc"> raw JSON</label></div>
<input class="p" id="path"><textarea id="body" placeholder="POST body (JSON)"></textarea>
<pre id="out">Tap a preset, then Run.</pre>
<script>
const $=i=>document.getElementById(i);
const F=(ch,x)=>JSON.stringify(Object.assign({channelId:ch,page:1,perPage:12,genre:"All",country:"All",year:"All",sort:"ForYou",classify:"All"},x||{}));
const PRE=[["Home","GET","/home?host=moviebox.ph",""],
["Movies ch1","POST","/subject/filter",F("1")],["TV ch2","POST","/subject/filter",F("2")],["Anime ch4","POST","/subject/filter",F("4")],
["Movies Action 2025","POST","/subject/filter",F("1",{genre:"Action",year:"2025"})],
["Movies Hindi (classify)","POST","/subject/filter",F("1",{classify:"Hindi dub"})],
["Movies Hindi (language)","POST","/subject/filter",F("1",{language:"Hindi dub"})],
["Old: TV ch5","POST","/subject/filter",F("5")],
["Trending","GET","/subject/trending?page=0&perPage=12",""],
["Midnight","GET","/tab-operating?tabId=ONEROOM_MIDNIGHT",""],
["Bottom tabs","GET","/tab/get-bottom-tab-list",""],
["Search","POST","/subject/search",JSON.stringify({keyword:"avatar",page:1,perPage:12})]];
function find(o,d){if(d>6||!o||typeof o!=="object")return null;
 if(Array.isArray(o)){if(o.length&&o[0]&&typeof o[0]==="object"&&("title" in o[0]||"subject" in o[0]))return o;for(const x of o){const r=find(x,d+1);if(r)return r}return null}
 for(const k of Object.keys(o)){const r=find(o[k],d+1);if(r)return r}return null}
function summ(d){let s="";if(d._http||d._err)s+="HTTP/ERR: "+(d._http||d._err)+" "+(d._body||"")+"\n";
 const x=d.data||{},pg=x.pager||{},tot=pg.totalCount!==undefined?pg.totalCount:(x.total!==undefined?x.total:pg.total);
 if(tot!==undefined)s+="total: "+tot+(String(tot)==="1000000"?"  <- SENTINEL (filter ignored)":"")+"\n";
 const a=find(d.data||d,0);
 if(a)s+="items: "+a.length+"\n"+a.slice(0,12).map(i=>"- "+((i.subject&&i.subject.title)||i.title)+(i.subjectType!=null?" ["+i.subjectType+"]":"")).join("\n");
 else s+="no item list found. data keys: "+Object.keys(x).join(", ");return s}
async function run(){$("out").textContent="...";let body=null;
 if($("m").value==="POST"){try{body=JSON.parse($("body").value||"{}")}catch(e){$("out").textContent="Body is not valid JSON";return}}
 try{const r=await fetch("/lab/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({method:$("m").value,path:$("path").value,body:body})});
  const d=await r.json();$("out").textContent=$("rawc").checked?JSON.stringify(d,null,1).slice(0,20000):summ(d)}catch(e){$("out").textContent="Error: "+e}}
function setReq(m,p,b,go){$("m").value=m;$("path").value=p;$("body").value=b;if(go)run()}
PRE.forEach(([n,m,p,b])=>{const e=document.createElement("button");e.textContent=n;e.onclick=()=>setReq(m,p,b,true);$("pre").append(e)});
$("run").onclick=run;
fetch("/lab/ids").then(r=>r.json()).then(L=>L.forEach(o=>{const e=document.createElement("button");e.textContent=o.title;
 e.onclick=()=>setReq("GET","/ranking-list/content?id="+o.id+"&page=1&perPage=12","",true);$("ids").append(e)})).catch(()=>{});
</script></body></html>'''


@app.get("/lab")
async def lab_page():
    """Test page: send any request to MovieBox from the browser and see the answer, without changing code."""
    return Response(_LAB_HTML, media_type="text/html")


@app.post("/lab/run")
async def lab_run(req: Request):
    j = await req.json()
    path = j.get("path") or ""
    if not path.startswith("/"):
        return JSONResponse({"_err": "path must start with /"})
    post = (j.get("method") or "GET").upper() == "POST"
    return JSONResponse(await call(API + path, (j.get("body") or {}) if post else None))


@app.get("/lab/ids")
async def lab_ids():
    d = await call(_home_url())
    return [{"title": op.get("title") or op.get("type"), "id": op.get("genreTopId")}
            for op in (d.get("data") or {}).get("operatingList") or [] if op.get("genreTopId")]


@app.get("/shape")
async def shape(tab: str = ""):
    """Open in the browser: /shape -> structure of the MovieBox home response (tabs, section ids). For debugging."""
    d = await call(_home_url(tab))
    data = d.get("data") or {}
    return {"status": d.get("_http") or d.get("_err") or "ok",
            "data_keys": {k: type(v).__name__ + (f"[{len(v)}]" if isinstance(v, (list, dict)) else "") for k, v in data.items()},
            "top_tabs_found": _toptabs(d),
            "ops": [{"type": op.get("type"), "title": op.get("title"), "keys": sorted(op.keys()), "n": len(op.get("subjects") or []),
                     "ids": {k: op[k] for k in _ID_KEYS if k in op}, "sub_tabs": [t["name"] for t in (_subtabs(op) or [])]}
                    for op in data.get("operatingList") or []]}


@app.get("/diag")
async def diag():
    """Debug: open /diag in the browser for a one-page health check (token, home, detail of the first title)."""
    out = {"token_ok": bool(await ensure_token(True))}
    d = await call(_home_url())
    out["home"] = d.get("_http") or d.get("_err") or "ok"
    first = next((i for sec in _sections(d) for i in sec["items"] if i.get("slug")), None)
    out["first_item"] = first
    if first:
        dd = await call(f"{API}/detail?detailPath={first['slug']}")
        data = dd.get("data") or {}
        out["detail"] = {"status": dd.get("_http") or dd.get("_err") or "ok", "code": dd.get("code"),
                         "message": dd.get("message") or dd.get("msg"), "has_subject": bool(data.get("subject")),
                         "body": dd.get("_body"), "keys": list(dd.keys())[:10]}
    return out


@app.get("/discover")
async def discover():
    """Debug: probes home tabs 0-12, filter variants (genre / platform) and a few candidate endpoints in one go."""
    async def tab(t):
        d = await call(_home_url(str(t)))
        ops = (d.get("data") or {}).get("operatingList") or []
        return str(t), {"err": d.get("_http") or d.get("_err"), "n_ops": len(ops),
                        "titles": [(op.get("title") or op.get("type")) for op in ops][:9],
                        "filters": [f.get("title") for op in ops if op.get("type") == "FILTER" for f in (op.get("filters") or [])][:25]}

    base = {"classify": "All", "country": "All", "genre": "All", "sort": "ForYou", "year": "All", "page": 1, "perPage": 6}

    async def flt(name, extra):
        body = {**base, **extra}
        d = await call(f"{API}/subject/filter", body)
        its = _extract(d)
        return name, {"n": len(its), "err": d.get("_http") or d.get("_err"), "names": [i["name"] for i in its[:4]]}

    async def probe(path):
        d = await call(API + path)
        return path, d.get("_http") or ("ok:" + json.dumps(d, ensure_ascii=False)[:160])

    tabs = dict(await asyncio.gather(*[tab(t) for t in range(0, 13)]))
    tests = dict(await asyncio.gather(
        flt("tv_romance", {"tabId": "5", "channelId": "5", "genre": "Romance"}),
        flt("tv_all", {"tabId": "5", "channelId": "5"}),
        flt("tab3_all", {"tabId": "3", "channelId": "3"}),
        flt("tab4_all", {"tabId": "4", "channelId": "4"}),
        flt("tv_platform_netflix", {"tabId": "5", "channelId": "5", "platform": "Netflix"}),
        flt("tv_classify_netflix", {"tabId": "5", "channelId": "5", "classify": "Netflix"}),
    ))
    paths = ["/subject/filter-options", "/subject/filter/options", "/home/filter", "/subject/genres", "/subject/platform", "/subject/platform-list", "/subject/filter/config"]
    probes = dict(await asyncio.gather(*[probe(p) for p in paths]))
    return {"tabs": tabs, "filter_tests": tests, "probes": probes}


@app.get("/peek")
async def peek(tab: str = ""):
    """Debug: open /peek in the browser to see the raw platformList, the Categories (FILTER) block and one sample section."""
    d = await call(_home_url(tab))
    data = d.get("data") or {}
    ops = data.get("operatingList") or []
    flt = next((op for op in ops if op.get("type") == "FILTER"), {})
    smp = next((op for op in ops if op.get("type") == "SUBJECTS_MOVIE"), {})
    trim = lambda op: {k: (v[:1] if k == "subjects" and isinstance(v, list) else v) for k, v in op.items()}
    out = {"platformList": data.get("platformList"), "filter_block": trim(flt), "sample_section": trim(smp)}
    return Response(json.dumps(out, ensure_ascii=False)[:30000], media_type="application/json")


@app.get("/raw")
async def raw(p: str):
    """Debug: view any MovieBox API path directly. /raw?p=%2Fhome%3Fhost%3Dmoviebox.ph"""
    if not p.startswith("/"):
        return JSONResponse({"error": "p must start with '/'"}, status_code=400)
    d = await call(API + p)
    s = json.dumps(d, ensure_ascii=False)
    return Response(s[:60000], media_type="application/json")


# ===== Content-type endpoints (modelled on the real MovieBox site: TV show / Movie / Animation / Most Watched / ...) =====
# Filter values below are copied from the real site's /web/film and /web/tv-series filter panels.
TYPES = {"movie": "1", "tv": "2", "anime": "4"}  # web API channelId (verified earlier by the phone test)
FILTERS = {
    "genre": ["All", "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy", "Film-Noir", "Game-Show", "History", "Horror", "Music", "Musical", "Mystery", "News", "Reality-TV", "Romance", "Sci-Fi", "Short", "Sport", "Talk-Show", "Thriller", "War", "Western", "Other"],
    "country": ["All", "United States", "United Kingdom", "Korea", "Japan", "Bangladesh", "China", "Egypt", "France", "Germany", "India", "Indonesia", "Iraq", "Italy", "Ivory Coast", "Kenya", "Lebanon", "Mexico", "Morocco", "Nigeria", "Pakistan", "Philippines", "Russia", "Saudi Arabia", "South Africa", "Spain", "Syria", "Thailand", "Malaysia", "Turkey", "Other"],
    "year": ["All", "2026", "2025", "2024", "2023", "2022", "2021", "2020", "2010s", "2000s", "1990s", "1980s", "Other"],
    "language": ["All", "English dub", "French dub", "Hindi dub", "Bengali dub", "Urdu dub", "Punjabi dub", "Tamil dub", "Telugu dub", "Malayalam dub", "Kannada dub", "Arabic dub", "Arabic sub", "Tagalog dub", "Indonesian dub", "Russian dub", "Kurdish sub", "Spanish dub", "Spanish sub", "SpanishLatam dub"],
    "sort": ["ForYou", "Hottest", "Latest", "Rating"],
}


@app.get("/meta/filters")
async def meta_filters():
    """All filter options + content types, so the frontend does not need to hard-code them."""
    return {"types": list(TYPES), "filters": FILTERS,
            "pages": {"movie": "/web/movie", "tv": "/web/tv-series", "anime": "/web/animated-series",
                      "midnight": "/web/midnight (18+, disabled unless ENABLE_MIDNIGHT=1)", "most_watched": "/ranking-list"}}


@app.get("/browse")
async def browse(type: str = "movie", genre: str = "All", country: str = "All", year: str = "All",
                 language: str = "All", sort: str = "ForYou", page: int = 1, per: int = 24):
    """One clean endpoint for every video type: type = movie | tv | anime."""
    ch = TYPES.get(type)
    if not ch:
        return JSONResponse({"error": "type must be one of " + ", ".join(TYPES)}, status_code=400)
    if type == "anime":  # channel 4 is not anime (phone test); anime = series channel + Japan
        ch = "2"
        if country == "All":
            country = "Japan"
    body = {"channelId": ch, "page": page, "perPage": per, "genre": genre, "country": country,
            "year": year, "sort": sort, "classify": language}
    d = await call(f"{API}/subject/filter", body)
    return {"type": type, "items": _extract(d), "page": page, "pager": (d.get("data") or {}).get("pager"),
            "_debug": d.get("_http") or d.get("_err")}


@app.get("/ranking")
async def ranking(id: str = "", page: int = 1, per: int = 24):
    """Most Watched (/ranking-list). With id -> that ranking's titles; without -> the list of rankings."""
    if id:
        d = await call(f"{API}/ranking-list/content?id={quote(id, safe='')}&page={page}&perPage={per}")
        return {"items": _extract(d), "page": page, "_debug": d.get("_http") or d.get("_err")}
    # Verified on the live server: GET /ranking-list -> data.rankingList = [{name, id, path}, ...] (TOP Movies, Cinema, Bollywood, South Indian, Hollywood, ...)
    d = await call(f"{API}/ranking-list")
    rl = (d.get("data") or {}).get("rankingList") or []
    return {"rankings": [{"name": r.get("name"), "id": str(r.get("id")), "path": r.get("path")} for r in rl if isinstance(r, dict) and r.get("id")],
            "_debug": d.get("_http") or d.get("_err")}


# ===== Real MovieBox tabs: Movie / TV Show / Anime (rows like the MovieBox site) =====
# Scan result: tab-operating?tabId=ONEROOM_MOVIE works (14 rows). ONEROOM_TV / ONEROOM_ANIME give HTTP 400,
# so TV and Anime rows are built from /subject/filter (channelId 2 and 4) + the matching home rows.
_TABCACHE = {}


async def _frow(title, ch, **kw):
    body = {"channelId": ch, "page": 1, "perPage": 18, "genre": "All", "country": "All", "year": "All", "sort": "ForYou", "classify": "All"}
    body.update(kw)
    d = await call(f"{API}/subject/filter", body)
    its = _extract(d)
    q = "type=/home/movieFilter&tabId=%s&filterType=%s" % ({"1": "2", "2": "5", "4": "8"}[ch], json.dumps(
        {"classify": body["classify"], "country": body["country"], "genre": body["genre"], "sort": body["sort"], "year": body["year"]}))
    return {"section": title, "items": its, "more": {"query": q}, "tabs": None, "kind": None, "query": q}


# Phone test result: channelId 4 is NOT anime (returns random films/shows), and "Top Rated" sort returns junk.
# Anime = channel 2 (series) + country Japan (verified: Naruto, Demon Slayer, Spy x Family, Tokyo Revengers),
# anime movies = channel 1 + country Japan. Rows that come back empty are dropped automatically.
_TAB_ROWS = {
    "tv": [("Hottest TV Shows", "2", {"sort": "Hottest"}), ("Hindi Dubbed Series", "2", {"classify": "Hindi dub"}),
           ("Latest Releases", "2", {"sort": "Latest"}), ("Korean Dramas", "2", {"country": "Korea"}),
           ("Indian Series", "2", {"country": "India"}), ("US Series", "2", {"country": "United States"}),
           ("Crime", "2", {"genre": "Crime", "sort": "Hottest"}), ("Romance", "2", {"genre": "Romance", "sort": "Hottest"}),
           ("Comedy", "2", {"genre": "Comedy", "sort": "Hottest"}), ("Thriller", "2", {"genre": "Thriller", "sort": "Hottest"}),
           ("Sci-Fi", "2", {"genre": "Sci-Fi", "sort": "Hottest"})],
    "anime": [("Hottest Anime", "2", {"country": "Japan", "sort": "Hottest"}),
              ("Hindi Dubbed Anime", "2", {"country": "Japan", "classify": "Hindi dub", "sort": "Hottest"}),
              ("Latest Anime", "2", {"country": "Japan", "sort": "Latest"}),
              ("Anime Movies", "1", {"country": "Japan", "sort": "Hottest"}),
              ("Hindi Dubbed Anime Movies", "1", {"country": "Japan", "classify": "Hindi dub"}),
              ("Animation (Cartoons)", "2", {"genre": "Animation", "sort": "Hottest"}),
              ("Anime Action", "2", {"country": "Japan", "genre": "Action", "sort": "Hottest"}),
              ("Anime Adventure", "2", {"country": "Japan", "genre": "Adventure", "sort": "Hottest"}),
              ("Anime Comedy", "2", {"country": "Japan", "genre": "Comedy", "sort": "Hottest"}),
              ("Anime Romance", "2", {"country": "Japan", "genre": "Romance", "sort": "Hottest"}),
              ("Anime Fantasy", "2", {"country": "Japan", "genre": "Fantasy", "sort": "Hottest"})],
}
_TAB_HOME_ROWS = {"tv": re.compile(r"series|drama|tv|show", re.I), "anime": re.compile(r"anime|animation", re.I)}


@app.get("/tab/{name}")
async def tab_page(name: str):
    """name = movie | tv | anime. Same shape as /home ({sections: [...]}), so the frontend draws it with the same code."""
    name = name.lower()
    if name not in ("movie", "tv", "anime"):
        return JSONResponse({"error": "name must be movie, tv or anime"}, status_code=400)
    c = _TABCACHE.get(name)
    if c and time.time() - c[0] < 300:
        return c[1]
    secs = []
    if name == "movie":
        d = await call(f"{API}/tab-operating?tabId=ONEROOM_MOVIE")
        seen = set()
        for s_ in _sections(d):   # MovieBox's own rows; the same row (e.g. Trending in Cinema) comes twice -> keep once
            key = (s_["section"], str((s_.get("more") or {}).get("genreTopId") or ""))
            if key in seen or (not s_["items"] and s_.get("kind") != "filter"):
                continue
            seen.add(key)
            if s_.get("kind") == "filter" and s_.get("tabs"):
                s_["tabs"] = [t for t in s_["tabs"] if t.get("query")]
            secs.append(s_)
        if not any(x["items"] for x in secs):  # fallback if the tab call fails
            res = await asyncio.gather(*[_frow(t, "1", **kw) for t, kw in [("Hottest Movies", {"sort": "Hottest"}), ("Hindi Dubbed Movies", {"classify": "Hindi dub"}), ("Latest Releases", {"sort": "Latest"})]])
            secs = [r for r in res if r["items"]]
    else:
        res = await asyncio.gather(*[_frow(t, ch, **kw) for t, ch, kw in _TAB_ROWS[name]])
        secs = [r for r in res if r["items"]]
        h = await call(_home_url())   # plus the matching rows of MovieBox's own home (Top Anime, Best Asian Series, Western TV ...)
        extra = [s_ for s_ in _sections(h) if s_.get("kind") != "filter" and s_["section"] != "Banner" and s_["items"]
                 and _TAB_HOME_ROWS[name].search(str(s_["section"])) and "short" not in str(s_["section"]).lower()]
        secs = extra + secs
    out = {"sections": secs, "tabs": [], "platforms": [], "type": name}
    if secs:
        _TABCACHE[name] = (time.time(), out)
    return out


@app.get("/midnight/status")
async def midnight_status():
    return {"enabled": os.getenv("ENABLE_MIDNIGHT") == "1"}


@app.get("/midnight")
async def midnight(page: int = 0):
    """MovieBox's 'Midnight' section is adult (18+) content. Off by default; the site owner must set ENABLE_MIDNIGHT=1."""
    if os.getenv("ENABLE_MIDNIGHT") != "1":
        return JSONResponse({"error": "Midnight (18+) is disabled. Set ENABLE_MIDNIGHT=1 to enable it."}, status_code=403)
    # Verified on the phone scan: /tab-operating?tabId=ONEROOM_MIDNIGHT -> HTTP 200 with ~19 rows.
    # (The old code looked for a "Midnight" tab inside the /home response; that tab does not exist there.)
    d = await call(f"{API}/tab-operating?tabId=ONEROOM_MIDNIGHT")
    secs = [x for x in _sections(d) if x["items"]]
    return {"sections": secs, "tabs": [], "platforms": [], "_debug": d.get("_http") or d.get("_err")}


@app.get("/search")
async def search(q: str = Query(..., min_length=1), page: int = 1):
    d = await call(f"{API}/subject/search", {"keyword": q, "page": page, "perPage": 24})
    inner = d.get("data") or {}
    return {"items": [_item(s) for s in inner.get("items") or inner.get("list") or []]}


@app.get("/detail/{slug}")
async def detail(request: Request, slug: str):
    protect.after_detail(request, slug)
    urls = [f"{API}/detail?detailPath={slug}", f"{API}/detail?detailPath={slug}&host={HOST}"]
    d = await call(urls[0])
    if not (d.get("data") or {}).get("subject"):  # retry with a fresh token, then with the host parameter
        await ensure_token(True)
        for u in urls:
            d = await call(u)
            if (d.get("data") or {}).get("subject"):
                break
    return d


def _px(u, c, sess=None):
    return protect.link(u, c, sess)


@app.get("/api/stream/{subject_id}")
async def stream(request: Request, subject_id: str, detail_path: str, se: int = 0, ep: int = 0):
    c = protect.ctx(request)
    user = await _me(request)
    refuse = protect.stream_gate(request, user, subject_id, detail_path, se, ep)
    if refuse is not None:
        return JSONResponse(refuse[1], status_code=refuse[0])
    sess = protect.new_session(c)
    dd = await call(f"{API}/media-player/get-domain")
    dom = (dd.get("data") if isinstance(dd.get("data"), str) else "") or "https://netfilm.world"
    dom = dom.rstrip("/")
    ref = f"{dom}/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    _ref["v"] = dom + "/"
    play = f"{dom}/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
    PH = {"User-Agent": UA, "Accept": "application/json", "Referer": ref, "X-Client-Info": XCI, "Accept-Language": "en-IN,en;q=0.9", **INH}
    pr = await call(play, hdr=PH)
    pd = pr.get("data") or {}
    if not pd.get("hasResource"):  # retry once with a fresh token
        await ensure_token(True)
        pr = await call(play, hdr=PH)
        pd = pr.get("data") or {}
    used = (se, ep)
    if not pd.get("hasResource"):
        # Shorts (short dramas) and some series number their episodes differently (season 0 or 1, episode 0 or 1):
        # try the other numberings before giving up
        alts = []
        if ep == 0:
            alts += [(1, 1), (0, 1)]
        if se == 0 and ep > 0:
            alts.append((1, ep))
        if se > 0:
            alts.append((0, ep))
        for se2, ep2 in alts[:3]:
            if (se2, ep2) == (se, ep):
                continue
            ref2 = f"{dom}/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se2}&detailEp={ep2}&lang=en"
            play2 = f"{dom}/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se2}&ep={ep2}&detailPath={detail_path}"
            r2 = await call(play2, hdr={**PH, "Referer": ref2})
            d2 = r2.get("data") or {}
            if d2.get("hasResource"):
                pr, pd, used = r2, d2, (se2, ep2)
                break
    src = []
    for x in pd.get("hls") or []:  # hls items can be dicts or strings
        u = x.get("url") if isinstance(x, dict) else x
        if u:
            src.append({"kind": "hls", "label": "Auto (HLS)", "res": 9999, "url": _px(u, c, sess)})
    for s in pd.get("streams") or []:
        if s.get("url"):
            res = int(s.get("resolutions") or 0)
            src.append({"kind": "hls" if ".m3u8" in s["url"] else "mp4", "label": f"{res}p", "res": res, "url": _px(s["url"], c, sess)})
    src.sort(key=lambda x: -x["res"])
    st = (pd.get("streams") or pd.get("dash") or [None])[0] or {}
    cap = {"id": st.get("id"), "fmt": st.get("format") or "MP4"}
    why = None
    if not src:   # the real reason, shown in the player (urls / upstream host names are masked)
        def _m(x):
            return _UP_HOST.sub("upstream", re.sub(r"https?://\S+", "<url>", str(x)))[:160]
        try:
            why = "play(se=%s,ep=%s tried alts) http=%s err=%s code=%s msg=%s hasResource=%s keys=%s hls=%d streams=%d dash=%d" % (se, ep, 
                pr.get("_http") or 200, _m(pr.get("_err") or "-"), pr.get("code", "-"), _m(pr.get("message") or pr.get("msg") or pr.get("_body") or "-"),
                pd.get("hasResource"), ",".join(sorted(map(str, pd.keys())))[:80] or "-",
                len(pd.get("hls") or []), len(pd.get("streams") or []), len(pd.get("dash") or []))
        except Exception as e:
            why = "diag failed: " + _m(e)
    return {"has_resource": bool(src), "sources": src, "note": None if src else "No stream was found for this title.", "why": why, "cap": cap, "_domain": dom}


@app.get("/p")
async def proxy(request: Request, u: str, t: str = ""):
    pc = protect.ctx(request)
    if not pc["vip"]:
        ok, why = protect.verify(pc, u, t, "s")
        if not ok or not protect.safe_url(u):
            return Response("forbidden", status_code=403, headers={"Cache-Control": "no-store"})
    h = {"User-Agent": UA, "Referer": _ref["v"], "Origin": _ref["v"].rstrip("/"), "Accept": "*/*"}
    if request.headers.get("range"):
        h["Range"] = request.headers["range"]
    c = httpx.AsyncClient(follow_redirects=True, timeout=30, **CK)
    try:
        r = await c.send(c.build_request("GET", u, headers=h), stream=True)
    except Exception as e:
        await c.aclose()
        return Response("bad gateway", status_code=502, headers={"Cache-Control": "no-store"})
    ct = r.headers.get("content-type", "").lower()
    if "mpegurl" in ct or u.split("?")[0].endswith(".m3u8"):
        text = (await r.aread()).decode("utf-8", "ignore")
        base = str(r.url)
        await r.aclose(); await c.aclose()
        fix = lambda x: protect.link_like(urljoin(base, x.strip()), pc, t)
        lines = []
        for ln in text.splitlines():
            if ln.startswith("#"):
                ln = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + fix(m.group(1)) + '"', ln)
            elif ln.strip():
                ln = fix(ln)
            lines.append(ln)
        return Response("\n".join(lines), media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}

    async def gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose(); await c.aclose()
    return StreamingResponse(gen(), status_code=r.status_code, headers=keep)


@app.get("/api/captions/{subject_id}")
async def captions(request: Request, subject_id: str, detail_path: str, id: str = "", fmt: str = "MP4"):
    c = protect.ctx(request)
    if not id:
        return {"captions": []}
    d = await call(f"{API}/subject/caption?format={fmt}&id={id}&subjectId={subject_id}&detailPath={detail_path}")
    cd = d.get("data")
    caps = (cd.get("captions") if isinstance(cd, dict) else cd) or []
    return {"captions": [{"lang": cap.get("lanName") or cap.get("lan") or "Subtitle", "url": protect.link(cap["url"], c, None, "s", "/sub")}
                         for cap in caps if isinstance(cap, dict) and cap.get("url")]}


@app.get("/sub")
async def sub(request: Request, u: str, t: str = ""):
    pc = protect.ctx(request)
    if not pc["vip"]:
        ok, why = protect.verify(pc, u, t, "s")
        if not ok or not protect.safe_url(u):
            return Response("forbidden", status_code=403)
    """Fetch subtitles and convert SRT -> WebVTT (the browser <track> element only understands VTT)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            r = await c.get(u, headers={"User-Agent": UA, "Referer": _ref["v"]})
        except Exception:
            return Response("bad gateway", status_code=502)
    t = r.content.decode("utf-8-sig", "ignore").replace("\r", "")
    if not t.lstrip().startswith("WEBVTT"):
        t = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d{3})", r"\1.\2", t)
    return Response(t, media_type="text/vtt; charset=utf-8")


# ---------------- /dl : download ----------------
def _dh():
    return {"User-Agent": UA, "Referer": _ref["v"], "Origin": _ref["v"].rstrip("/"), "Accept": "*/*"}


def _fname(name, ext):
    n = re.sub(r"[^\w\- ]+", "", name or "").strip().replace(" ", "_") or "video"
    return n + ext


async def _hls_plan(c, u):
    """master/media playlist -> (segments, init, error). For a master playlist, the highest-bandwidth variant is used."""
    t, base = "", u
    for _ in range(3):
        r = await c.get(u, headers=_dh())
        if r.status_code != 200:
            return None, None, f"Playlist returned {r.status_code}"
        t, base = r.text, str(r.url)
        if "#EXT-X-STREAM-INF" not in t:
            break
        lines, best, bw = t.splitlines(), None, -1
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", ln)
                b = int(m.group(1)) if m else 0
                nx = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
                if nx and b > bw:
                    best, bw = urljoin(base, nx), b
        if not best:
            return None, None, "No variant found in the playlist"
        u = best
    lines = t.splitlines()
    if any(l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l for l in lines):
        return None, None, "This stream is encrypted and cannot be downloaded."
    m = re.search(r'#EXT-X-MAP:.*?URI="([^"]+)"', t)
    init = urljoin(base, m.group(1)) if m else None
    segs = [urljoin(base, x.strip()) for x in lines if x.strip() and not x.startswith("#")]
    if not segs:
        return None, None, "The playlist is empty"
    return segs, init, None


async def _dl_hls(u, name, check, done=None):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            segs, init, err = await _hls_plan(c, u)
        except Exception as e:
            segs, init, err = None, None, "download failed"
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if check:
        return {"ok": True, "segments": len(segs)}
    ext = ".mp4" if init else ".ts"

    async def gen():
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=40, **CK) as c:
                for s in ([init] if init else []) + segs:
                    for _ in range(3):
                        try:
                            r = await c.get(s, headers=_dh())
                            if r.status_code == 200:
                                yield r.content
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1)
                    else:
                        return  # segment failed 3 times: stop the download here
        finally:
            if done:
                done()
    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{_fname(name, ext)}"'})


@app.get("/dl")
async def download(request: Request, u: str, name: str = "video", check: int = 0, t: str = ""):
    if not u.startswith("http"):
        return JSONResponse({"ok": False, "error": "Invalid link"}, status_code=400)
    pc = protect.ctx(request)
    if not pc["vip"]:
        ok, why = protect.verify(pc, u, t, "d")
        if not ok or not protect.safe_url(u):
            return JSONResponse({"ok": False, "error": "This download link has expired. Please press Download again."}, status_code=403)
    did = None
    if not check:
        did, n = protect.dl_begin(pc["ip"])
        if n > protect.guard.DL_MAX and not pc["vip"] and protect.guard.violation("dl_many", pc["ip"], "%d downloads at once" % n, dev=pc["dev"]):
            protect.dl_end(pc["ip"], did)
            return JSONResponse({"ok": False, "error": "Too many downloads at the same time. Please wait for one to finish."}, status_code=429)
    _done = (lambda: protect.dl_end(pc["ip"], did)) if did else None
    if ".m3u8" in u.split("?")[0].lower():
        r = await _dl_hls(u, name, check, _done)
        if _done and not isinstance(r, StreamingResponse):
            _done()
        return r
    c = httpx.AsyncClient(follow_redirects=True, timeout=30, **CK)
    try:
        r = await c.send(c.build_request("GET", u, headers=_dh()), stream=True)
    except Exception as e:
        await c.aclose()
        if _done:
            _done()
        return JSONResponse({"ok": False, "error": "download failed"}, status_code=502)
    ct = r.headers.get("content-type", "").lower()
    if r.status_code != 200 or "mpegurl" in ct:
        code = r.status_code
        await r.aclose(); await c.aclose()
        if "mpegurl" in ct:
            rr = await _dl_hls(u, name, check, _done)
            if _done and not isinstance(rr, StreamingResponse):
                _done()
            return rr
        if _done:
            _done()
        return JSONResponse({"ok": False, "error": f"Server returned {code}"}, status_code=502)
    if check:
        await r.aclose(); await c.aclose()
        return {"ok": True}
    hd = {"Content-Disposition": f'attachment; filename="{_fname(name, ".mp4")}"', "Content-Type": ct or "video/mp4"}
    if r.headers.get("content-length"):
        hd["Content-Length"] = r.headers["content-length"]

    async def gen():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose(); await c.aclose()
            if _done:
                _done()
    return StreamingResponse(gen(), headers=hd)


@app.post("/api/dltok")
async def api_dltok(request: Request):
    """Download ticket: only for a signed-in user, only for a url this page was given (its stream link proves that)."""
    b = await _body(request)
    u, t = str(b.get("u", "")), str(b.get("t", ""))
    user = await _me(request)
    if not user:
        return JSONResponse({"error": "Please sign in to download.", "login": True}, status_code=401)
    pc = protect.ctx(request)
    if not pc["vip"]:
        ok, why = protect.verify(pc, u, t, "s")
        if not ok or not protect.safe_url(u):
            return JSONResponse({"error": "This video link has expired. Please reload the video."}, status_code=403)
    nm = str(b.get("name", "video"))[:120]
    return {"url": protect.link(u, pc, None, "d", "/dl") + "&name=" + quote(nm, safe="")}


# ===================== ACCOUNTS (JaatFlix's own database) =====================
import hmac, threading
ADMIN_KEY = os.getenv("ADMIN_KEY", "")


def _token(request: Request):
    a = request.headers.get("authorization") or ""
    return a[7:] if a.lower().startswith("bearer ") else ""


async def _body(request: Request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


async def _me(request: Request):
    return await asyncio.to_thread(accounts.user_for, _token(request))


def _err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


@app.post("/api/signup")
async def api_signup(request: Request):
    b = await _body(request)
    uname = str(b.get("username", "")).strip()
    g = await asyncio.to_thread(protect.login_gate, request, uname, True)      # captcha every time + accounts per IP (DB)
    if g:
        return JSONResponse(g[1], status_code=g[0])
    tok, err = await asyncio.to_thread(accounts.signup, uname, str(b.get("password", "")), protect.signup_ipk(request))
    protect.login_note(request, uname, not err)
    if err:
        return _err(err)
    protect.signup_done(request)
    return {"token": tok, "user": await asyncio.to_thread(accounts.user_for, tok)}


@app.post("/api/login")
async def api_login(request: Request):
    b = await _body(request)
    uname = str(b.get("username", "")).strip()
    g = protect.login_gate(request, uname)
    if g:
        return JSONResponse(g[1], status_code=g[0])
    tok, err = await asyncio.to_thread(accounts.login, uname, str(b.get("password", "")))
    protect.login_note(request, uname, not err)
    if err:
        return _err(err)
    return {"token": tok, "user": await asyncio.to_thread(accounts.user_for, tok)}


# ---------------------------------------------------------------- captcha ----
# /api/captcha/new     -> {id, ttl, game:{mode:"text", img:"data:image/png;base64,..."}, pow:{salt,bits}}
# /api/captcha/verify  {id, pow, text} -> {ok:true, token, ttl} | {ok:false, why, retry:true}
# /api/captcha/status  -> {pass: bool, mode}
# The page sends the token in header X-Captcha; protect.ctx() checks it (signed, bound to the IP).
@app.get("/captcha.js")
async def captcha_js():
    return FileResponse("captcha.js", media_type="application/javascript", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/captcha/status")
async def captcha_status(request: Request):
    c = protect.ctx(request)
    return {"pass": bool(c["pass"]), "vip": bool(c["vip"]), "mode": protect.guard.mode(), "captcha": captcha.MODE}


@app.post("/api/captcha/{action}")
async def api_captcha(action: str, request: Request):
    if action not in ("new", "verify"):
        return _err("not found", 404)
    c = protect.ctx(request)
    b = await _body(request)
    if action == "new":
        g = await asyncio.to_thread(captcha.new, c["ip"], c["dev"])      # the picture is drawn with Pillow: off the event loop
        if not g:
            return _err("captcha error", 500)
        return g
    res = await asyncio.to_thread(captcha.verify, b.get("id"), c["ip"], b)
    if res.get("ok"):
        protect.after_captcha(request)           # a passed captcha wipes the bot score: no second one for the same old reason
    return res


@app.post("/api/logout")
async def api_logout(request: Request):
    await asyncio.to_thread(accounts.logout, _token(request))
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    u = await _me(request)
    if not u:
        return _err("Please sign in first.", 401)
    favs, prog = await asyncio.gather(asyncio.to_thread(accounts.get_favs, u["id"]),
                                      asyncio.to_thread(accounts.get_progress, u["id"]))
    return {"user": u, "progress": prog, "favs": favs}


@app.post("/api/progress")
async def api_progress(request: Request):
    u = await _me(request)
    if not u:
        return _err("Please sign in first.", 401)
    b = await _body(request)
    sid = str(b.get("sid", ""))[:60]
    if not sid:
        return _err("sid is required.")

    def _save():
        try:
            accounts.save_progress(u["id"], sid, b.get("season"), b.get("episode"), b.get("pos"), b.get("dur"), b)
            if float(b.get("dur") or 0) > 0 and float(b.get("pos") or 0) / float(b["dur"]) >= 0.85:
                accounts.mark_seen(u["id"], sid, b.get("season"), b.get("episode"))
        except Exception as e:
            print("[jf] progress save failed:", e, flush=True)
    threading.Thread(target=_save, daemon=True).start()
    return {"ok": True}


@app.post("/api/fav")
async def api_fav(request: Request):
    u = await _me(request)
    if not u:
        return _err("Please sign in first.", 401)
    b = await _body(request)
    it = b.get("item") if isinstance(b.get("item"), dict) else {}
    sid = str(it.get("subject_id") or b.get("sid") or "")[:60]
    if not sid:
        return _err("subject_id is required.")
    await asyncio.to_thread(accounts.set_fav, u["id"], sid, bool(b.get("on")), it)
    return {"ok": True}


@app.get("/api/seen/{sid}")
async def api_seen(sid: str, request: Request):
    u = await _me(request)
    return {"seen": await asyncio.to_thread(accounts.get_seen, u["id"], sid) if u else []}


@app.post("/api/password")
async def api_password(request: Request):
    u = await _me(request)
    if not u:
        return _err("Please sign in first.", 401)
    b = await _body(request)
    err = await asyncio.to_thread(accounts.change_password, u["id"], _token(request), str(b.get("old", "")), str(b.get("new", "")))
    return _err(err) if err else {"ok": True}


@app.get("/admin")
async def admin_page():
    return FileResponse("admin.html")


_admin_fails = {}


@app.post("/api/admin/{action}")
async def api_admin(action: str, request: Request):
    """Header X-Admin-Key = env ADMIN_KEY. actions: stats | password | delete"""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    bad = _admin_fails.get(ip, (0, 0))
    if bad[0] >= 5 and time.time() - bad[1] < 600:
        return _err("Too many attempts. Please try again in 10 minutes.", 429)
    given = request.headers.get("x-admin-key") or ""
    if not ADMIN_KEY or not hmac.compare_digest(given.encode(), ADMIN_KEY.encode()):
        _admin_fails[ip] = (bad[0] + 1, time.time())
        return _err("Invalid admin key.", 403)
    _admin_fails.pop(ip, None)
    b = await _body(request)
    pr = await asyncio.to_thread(protect.admin_action, action, b, request)
    if pr is not None:
        if isinstance(pr, dict) and "_status" in pr:
            return JSONResponse({k: v for k, v in pr.items() if k != "_status"}, status_code=pr["_status"])
        return pr
    if action == "stats":
        try:
            st = await asyncio.to_thread(accounts.admin_stats)
            return {**st, "sql": accounts.sql_state()}
        except Exception as e:
            return {"total": 0, "users": [], "error": str(e)[:200], "sql": accounts.sql_state()}
    if action == "db":
        act = str(b.get("action", ""))
        if act not in ("info", "check", "sync", "discard"):
            return _err("bad action")
        if act == "discard" and b.get("confirm") is not True:
            return _err("confirm required")
        return await asyncio.to_thread(accounts.db_admin, act)
    if action == "password":
        err = await asyncio.to_thread(accounts.admin_set_password, str(b.get("username", "")), str(b.get("password", "")))
    elif action == "delete":
        err = await asyncio.to_thread(accounts.admin_delete, str(b.get("username", "")))
    else:
        return _err("not found", 404)
    return _err(err) if err else {"ok": True}


PROBES = {
    "base": ({}, "moviebox.ph"),
    "lang_hi": ({"Accept-Language": "hi-IN,hi;q=0.9", "X-Request-Lang": "hi"}, "moviebox.ph"),
    "ip_headers": ({"X-Forwarded-For": IN_IP, "X-Real-IP": IN_IP, "CF-Connecting-IP": IN_IP, "CF-IPCountry": "IN"}, "moviebox.ph"),
    "host_h5": ({}, "h5.aoneroom.com"),
    "all_signals": ({"Accept-Language": "hi-IN,hi;q=0.9", "X-Forwarded-For": IN_IP, "X-Real-IP": IN_IP, "CF-IPCountry": "IN"}, "h5.aoneroom.com"),
}


@app.get("/probe")
async def probe():
    """Open in the browser: /probe -> shows which variant returns Indian/Hindi titles."""
    out = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, **CK) as c:
        try:
            out["server_ip_info"] = (await c.get("https://ipinfo.io/json")).json()
        except Exception as e:
            out["server_ip_info"] = str(e)
        for k, (extra, host) in PROBES.items():
            try:
                r = await c.get(f"{API}/home?host={host}", headers={**HBASE, **extra})
                try:
                    info = {a: b for a, b in json.loads(r.headers.get("x-user", "{}")).items() if a != "token"}
                except Exception:
                    info = {}
                ops = (r.json().get("data") or {}).get("operatingList") or []
                out[k] = {"status": r.status_code, "x_user": info,
                          "sample": [s.get("title") for op in ops for s in (op.get("subjects") or [])][:12]}
            except Exception as e:
                out[k] = {"err": str(e)}
    return out
