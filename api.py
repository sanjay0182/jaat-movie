"""
JaatFlix API (MovieBox backend)
- token auto-refresh; the token is also sent with stream calls
- banner items are normalized (the hero image used to break)
- India headers (Asia/Kolkata + IN)
- /p = video proxy: sends the correct Referer to the CDN and rewrites m3u8 playlists
  (direct browser playback used to stall at 0:00 for this reason)
- Optional: env IN_PROXY=http://user:pass@indian-proxy:port routes all calls through an Indian IP
"""
import asyncio, json, os, re, time
from urllib.parse import quote, urljoin
import httpx
import accounts
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(title="JaatFlix API", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    "icon-512.png": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAN5UlEQVR4nO3dwW0dxxKGUUrwjgut7QAMvBgUwIvZATgGAwrATuMtJDxT0iV570xPd1f95wRgEFB3fVNNCf7w/PzpCYA8H1f/AACsIQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQv6z+AeAqXz7/OuS/8/uf/wz578BuPjw/f1r9M8Bxo6b8MdpAaQJAGWtn/f1UgSoEgH1Vmfhv0wO2JQDspcfQf40YsBUBYL3eQ/81YsByAsAymXP/Z0rAKgLAbOb+a5SAyQSAScz9+ykBcwgA1zL3z1ACLiUAXMLcH0sJuIIAMJjRfx0ZYCwBYAxzfyYlYAgB4CyjfxUZ4CQB4DijfwcywGECwBFG/25kgAMEgMcY/TuTAR4iANzL6K9CBriTAPA+o78iGeBdAsBbjP7qZIA3CAC3Gf2dyAA3fVz9A7Aj078Zf6DcZAPgOyZFb1YBXhIAvjH6c8gAX3kC4unJ9A/jj5uvbADpzIJkVoFwNoBopn84ByCcDSCUm89LVoFMNoBEpj8/cCQyCUAcV52bHIxAnoCCuOHcw3NQDhtACtOfOzkqOQQggivNQxyYEJ6AmnOTOcNzUG82gM5Mf05yhHoTgLZcXYZwkBoTgJ5cWgZynLryO4Bu3FWu41cCzdgAWjH9uZQD1owA9OFyMoFj1okANOFaMo3D1oYAdOBCMpkj14MAlOcqsoSD14AA1OYSspDjV50AFOb6sZxDWJoAVOXisQlHsS4BKMmVYysOZFECUI/LxoYcy4oEoBjXjG05nOUIQCUuGJtzRGsRgDJcLUpwUAsRgBpcKgpxXKsQgAJcJ8pxaEsQgN25SBTl6O5PAABCCcDWfENRmgO8OQHYl8tDA47xzgRgU64NbTjM2xKAHbkwNONI70kAAEIJwHZ8K9GSg70hAdiLS0JjjvduBGAjrgftOeRbEQCAUAKwC19GhHDU9yEAW3AliOLAb0IA1nMZfvvjr9U/ArM59jsQALbw2x9/yQBMJgCL+Q56SQOiOPzLCcBKLsDPrAJRXIG1BIAdaQBMIADL+PZ5m1UghIuwkACs4dDfSQYSuA6rCAAFaABcQQAW8L1zgFWgN5diCQGgEhmAgQRgNl8652lAS67GfAIwlSM+ilWgJRdkMgGgMA2AMwRgHl83V7AKNOOazCQAdCADcIAATOK7ZgIN6MFlmUYAaMUqAPcTgBl80UwmA9W5MnMIAG1pALxNAC7nW2Yhq0BdLs4EAkB/GgA3CcC1fMVswipQketzNQEgiAzASwJwId8ve9KAQlyiSwkAiawC8CQA1/Hlsj8ZKMFVuo4AkE4DiCUAl/DNUotVYHMu1EUEAL7RANIIAPzLKkAUARjPulqdDGzItbqCAMBtGkB7AjCY75ROrAJbcbmGEwB4hwzQlQDAXTSAfgRgJCtqb1aB5VyxsQQAHqMBtCEA8DCrAD0IwDCW0zQysISLNpAAwCkaQF0CAGdZBShKAMawliID07huowgAjKQBFCIAMJhVgCoEYAALKT/TgEu5dEMIAFzFKsDmBACuJQNsSwDOsopyDw0YztU7TwBgEqsAuxEAmEoG2IcAwAIawA4E4BSvkBxmFTjPBTxJAGAlDWAhAYDFrAKsIgCwBRlgPgE4zvsjw2nAo1zDMwQA9mIVYBoBgB3JABMIAOxLA7iUAMDWrAJcRwAO8qsnZtKAN7iMhwkA1GAVYDgBgEpkgIEEAOrRAIYQACjJKsB5AgCFyQBnCACUpwEcIwBH+Gtn7CZ8FXAljxEA6CO5ARwgANBK+CrAQwQAGpIB7iEA0JYG8DYBgM6sArxBAKA/GeAmAYAUGsAPBACCWAV4SQAe5p+cUF3LBriYBwgAJLIK8CQAkEwGwgkApNOAWAIAWAVCCQDwjQykEQDgOxqQQwCAH1kFQggAcJsGtCcAwKusAr0JAPAOGehKAIC7aEA/AgDcyyrQzC+rfwCgjL//+5/VPwIj2QCAu5j+/dgAgHcY/V0JAPAqo783T0DAbaZ/ezYA4EdGfwgBAP5l9EfxBAR8Y/qnsQEARn8oGwCkM/1j2QAgl9EfTgAe9vuf/3z5/OvqnwJO6Tf6f//zn9U/Qj2egCBOv+nPMTYACGL085IAQASjn595AoL+TH9usgFAZ0Y/bxAA6Mno512egKAh05972ACgFaOf+wnAEf4tGBtKHv3+FdgxnoCgg+Tpz2E2AKjN6OcwGwAUZvpzhg0ASjL6OU8AoBijn1E8AUElpj8D2QAO8jdBmczof42/A3qYAMDujH4u4gkItmb6cx0bAGzK6OdqAgDbMfqZwxPQcX71xBVM/4e4hmfYAGAXRj+TCQCsZ/SzhCcgWMz0ZxUbwCn+ORhnGP0n+QXASTYAWMP0ZzkbAMxm9LMJAYB5jH624gnoLK+Q3Mn0H8vVO88GAJcz+tmTAMCFjH525gloAKsoN5n+13HphrABwHhGPyUIAIxk9FOIJ6AxLKQ8mf6zuG6j2ABgAKOfigQATjH6qcsT0DDW0kCm/3wu2kA2ADjC6KcBGwA8zPSnBxvASP73AO0Z/Wt5/xlLAOAuRj/9eAKC95n+tGQDGMwrUDNG/z68/wwnAHCb0U97noDG853SgOm/G9fqCjYA+I7RTw4BgG+MftJ4ArqEdbUc039nLtRFbACkM/qJJQBX8fdB92f0l+Dz/zqegAhl+oMN4EKWgD0Z/YX4/L+UDYAspj/8nw3gWpaAfRj95fj8v5oA0J/RDzd5Arqcr5i1TP+iXJwJbAC0ZfTD2wRgBr8JmMzor87n/xyegOjG9Ic72QAmsQRMYPT34PN/GgGgA6MfDvAENI/vmouY/p24JjPZACjM6IczBGAqvwkYxehvyef/ZJ6AZnPEzzP9W3I15rMBUInRDwPZABbwpXOM6d+YS7GEDYACjH64ggCs4bfBdzL6E/j8X8UT0DIO/btM/wQuwkI2AHZk9MMEArCSh6CfGf1RfP6v5QloMRfgJdM/isO/nA2ALRj9MN+H5+dPq38GnjwEkcbn/w48AW3BZSCKA78JAdiFK0EIR30fAgAQSgA24suI9hzyrQjAXlwPGnO8dyMA23FJaMnB3pAAAIQSgB35VqIZR3pPArApF4Y2HOZtCcC+XBsacIx3JgBbc3kozQHenAAAhBKA3fmGoihHd38CUICLRDkObQkCUIPrRCGOaxUCUIZLRQkOaiECUImrxeYc0VoEoBgXjG05nOUIQD2uGRtyLCsSgJJcNrbiQBYlAFW5cmzCUaxLAApz8VjOISxNAGpz/VjI8atOAMpzCVnCwWtAADpwFZnMketBAJpwIZnGYWtDAPpwLZnAMetEAFpxObmUA9bMh+fnT6t/Bsb78vnX1T8CrRj9LdkAenJdGchx6koA2nJpGcJBakwAOnN1OckR6s3vACL4lQCPMvoT2AAiuMw8xIEJIQApXGnu5Kjk8AQUx3MQrzH609gA4rjk3ORgBBKARK46P3AkMnkCiuY5CKM/mQ0gmssfzgEIZwPg6ckqkMfo58kGwFfGQRR/3HxlA+A7VoHejH5eEgBukIF+jH5+5gmIGwyLZvyBcpMNgLdYBaoz+nmDAPA+GajI6OddAsC9ZKAKo587CQCPkYGdGf08RAA4QgZ2Y/RzgABwnAzswOjnMAHgLBlYxejnJAFgDBmYyehnCAFgMCW4jrnPWALAJWRgLKOfKwgA11KCM8x9LiUATKIE9zP3mUMAmE0JXmPuM5kAsIwSfGXus4oAsF5mCcx9lhMA9tI7BoY+WxEA9tUjBoY+2xIAyqjSAxOfKgSA2tZWwaynNAGgrVFtMOXpSgAAQn1c/QMAsIYAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQgkAQCgBAAglAAChBAAglAAAhBIAgFACABBKAABCCQBAKAEACCUAAKEEACCUAACEEgCAUAIAEEoAAEIJAEAoAQAIJQAAoQQAIJQAAIQSAIBQAgAQSgAAQv0P6nyfXtcI938AAAAASUVORK5CYII="),
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
    return FileResponse("index.html")


def _item(s):
    s = s or {}
    t = s.get("title") or ""
    cn = s.get("corner")
    badge = cn if isinstance(cn, str) and cn else ("Hindi" if "[hindi]" in t.lower() else None)  # "Hindi" tag shown on the card
    return {"name": t or None, "poster_url": (s.get("cover") or {}).get("url"),
            "slug": s.get("detailPath"), "subject_id": s.get("subjectId"), "rating": s.get("imdbRatingValue"),
            "badge": badge}


# ===== MovieBox's real home / tabs / "All" (not search-based: MovieBox updates trending itself every day) =====
_TAB_KEYS = ("tabList", "tabs", "navList", "homeTabs", "topTabs", "navigation", "categoryList", "channelList")
_SUB_KEYS = ("tabs", "tabList", "subTabs", "categoryList", "categories", "filters", "genres")
_ID_KEYS = ("rankingId", "rankId", "opId", "id", "url", "jumpUrl", "path", "link")


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
    return {"sections": _sections(d), "tabs": _toptabs(d), "_debug": d.get("_http") or d.get("_err")}


@app.get("/more")
async def more(id: str = "", url: str = "", title: str = "", page: int = 1, per: int = 24):
    """Full list for 'All >' / a sub-tab. MovieBox's own endpoints are tried one by one."""
    cands = []
    if url.startswith("/"):
        cands.append(f"{API}{url}{'&' if '?' in url else '?'}page={page}&perPage={per}")
    if id:
        cands.append(f"{API}/ranking-list/content?id={quote(id, safe='')}&page={page}&perPage={per}")
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
    body = {**ft, "tabId": tab, "channelId": tab, "page": page, "perPage": per}
    flat = urlencode({**ft, "tabId": tab, "channelId": tab, "page": page, "perPage": per})
    cands = [
        ("POST", f"{API}/subject/filter", body),
        ("POST", f"{API}/home/movieFilter", body),
        ("POST", f"{API}/subject/browse", body),
        ("GET", f"{API}/subject/filter?{flat}", None),
        ("GET", f"{API}/home/movieFilter?tabId={tab}&filterType={quote(json.dumps(ft, separators=(',', ':')), safe='')}&page={page}&perPage={per}", None),
    ]
    tried = []
    for method, u, payload in cands:
        d = await call(u, payload) if method == "POST" else await call(u)
        its = _extract(d)
        err = d.get("_http") or d.get("_err")
        row = {"m": method, "url": u[:140], "n": len(its), "err": err}
        if not its and not err:
            row["head"] = json.dumps(d, ensure_ascii=False)[:300]
        tried.append(row)
        if its:
            return {"items": its, "page": page, "via": row, "tried": tried}
    return {"items": [], "page": page, "tried": tried}


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


@app.get("/search")
async def search(q: str = Query(..., min_length=1), page: int = 1):
    d = await call(f"{API}/subject/search", {"keyword": q, "page": page, "perPage": 24})
    inner = d.get("data") or {}
    return {"items": [_item(s) for s in inner.get("items") or inner.get("list") or []]}


@app.get("/detail/{slug}")
async def detail(slug: str):
    urls = [f"{API}/detail?detailPath={slug}", f"{API}/detail?detailPath={slug}&host={HOST}"]
    d = await call(urls[0])
    if not (d.get("data") or {}).get("subject"):  # retry with a fresh token, then with the host parameter
        await ensure_token(True)
        for u in urls:
            d = await call(u)
            if (d.get("data") or {}).get("subject"):
                break
    return d


def _px(u):
    return "/p?u=" + quote(u, safe="")


@app.get("/api/stream/{subject_id}")
async def stream(subject_id: str, detail_path: str, se: int = 0, ep: int = 0):
    dd = await call(f"{API}/media-player/get-domain")
    dom = (dd.get("data") if isinstance(dd.get("data"), str) else "") or "https://netfilm.world"
    dom = dom.rstrip("/")
    ref = f"{dom}/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    _ref["v"] = dom + "/"
    play = f"{dom}/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
    PH = {"User-Agent": UA, "Accept": "application/json", "Referer": ref, "X-Client-Info": XCI, "Accept-Language": "en-IN,en;q=0.9", **INH}
    pd = (await call(play, hdr=PH)).get("data") or {}
    if not pd.get("hasResource"):  # retry once with a fresh token
        await ensure_token(True)
        pd = (await call(play, hdr=PH)).get("data") or {}
    src = []
    for x in pd.get("hls") or []:  # hls items can be dicts or strings
        u = x.get("url") if isinstance(x, dict) else x
        if u:
            src.append({"kind": "hls", "label": "Auto (HLS)", "res": 9999, "url": _px(u)})
    for s in pd.get("streams") or []:
        if s.get("url"):
            res = int(s.get("resolutions") or 0)
            src.append({"kind": "hls" if ".m3u8" in s["url"] else "mp4", "label": f"{res}p", "res": res, "url": _px(s["url"])})
    src.sort(key=lambda x: -x["res"])
    st = (pd.get("streams") or pd.get("dash") or [None])[0] or {}
    cap = {"id": st.get("id"), "fmt": st.get("format") or "MP4"}
    return {"has_resource": bool(src), "sources": src, "note": None if src else "No stream was found for this episode.", "cap": cap, "_domain": dom}


@app.get("/p")
async def proxy(request: Request, u: str):
    h = {"User-Agent": UA, "Referer": _ref["v"], "Origin": _ref["v"].rstrip("/"), "Accept": "*/*"}
    if request.headers.get("range"):
        h["Range"] = request.headers["range"]
    c = httpx.AsyncClient(follow_redirects=True, timeout=30, **CK)
    try:
        r = await c.send(c.build_request("GET", u, headers=h), stream=True)
    except Exception as e:
        await c.aclose()
        return Response(str(e), status_code=502)
    ct = r.headers.get("content-type", "").lower()
    if "mpegurl" in ct or u.split("?")[0].endswith(".m3u8"):
        text = (await r.aread()).decode("utf-8", "ignore")
        base = str(r.url)
        await r.aclose(); await c.aclose()
        fix = lambda x: _px(urljoin(base, x.strip()))
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
async def captions(subject_id: str, detail_path: str, id: str = "", fmt: str = "MP4"):
    if not id:
        return {"captions": []}
    d = await call(f"{API}/subject/caption?format={fmt}&id={id}&subjectId={subject_id}&detailPath={detail_path}")
    cd = d.get("data")
    caps = (cd.get("captions") if isinstance(cd, dict) else cd) or []
    return {"captions": [{"lang": c.get("lanName") or c.get("lan") or "Subtitle", "url": "/sub?u=" + quote(c["url"], safe="")}
                         for c in caps if isinstance(c, dict) and c.get("url")]}


@app.get("/sub")
async def sub(u: str):
    """Fetch subtitles and convert SRT -> WebVTT (the browser <track> element only understands VTT)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            r = await c.get(u, headers={"User-Agent": UA, "Referer": _ref["v"]})
        except Exception as e:
            return Response(str(e), status_code=502)
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


async def _dl_hls(u, name, check):
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            segs, init, err = await _hls_plan(c, u)
        except Exception as e:
            segs, init, err = None, None, str(e)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if check:
        return {"ok": True, "segments": len(segs)}
    ext = ".mp4" if init else ".ts"

    async def gen():
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
    return StreamingResponse(gen(), media_type="application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{_fname(name, ext)}"'})


@app.get("/dl")
async def download(u: str, name: str = "video", check: int = 0):
    if not u.startswith("http"):
        return JSONResponse({"ok": False, "error": "Invalid link"}, status_code=400)
    if ".m3u8" in u.split("?")[0].lower():
        return await _dl_hls(u, name, check)
    c = httpx.AsyncClient(follow_redirects=True, timeout=30, **CK)
    try:
        r = await c.send(c.build_request("GET", u, headers=_dh()), stream=True)
    except Exception as e:
        await c.aclose()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    ct = r.headers.get("content-type", "").lower()
    if r.status_code != 200 or "mpegurl" in ct:
        code = r.status_code
        await r.aclose(); await c.aclose()
        if "mpegurl" in ct:
            return await _dl_hls(u, name, check)
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
    return StreamingResponse(gen(), headers=hd)


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
    tok, err = await asyncio.to_thread(accounts.signup, str(b.get("username", "")).strip(), str(b.get("password", "")))
    if err:
        return _err(err)
    return {"token": tok, "user": await asyncio.to_thread(accounts.user_for, tok)}


@app.post("/api/login")
async def api_login(request: Request):
    b = await _body(request)
    tok, err = await asyncio.to_thread(accounts.login, str(b.get("username", "")).strip(), str(b.get("password", "")))
    if err:
        return _err(err)
    return {"token": tok, "user": await asyncio.to_thread(accounts.user_for, tok)}


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
    if action == "stats":
        return await asyncio.to_thread(accounts.admin_stats)
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
