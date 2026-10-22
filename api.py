"""
MovieBox API (fixed)
- token auto-refresh + token bhi stream call mein jata hai
- banner items normalize (hero image pehle toot-ti thi)
- India headers (Asia/Kolkata + IN)
- /p  = video proxy: CDN ko sahi Referer deta hai, m3u8 rewrite karta hai
  (browser se direct play isi wajah se 0:00 pe atakta tha)
- Optional: env IN_PROXY=http://user:pass@indian-proxy:port -> saari calls India IP se
"""
import json, os, re, time
from urllib.parse import quote, urljoin
import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

app = FastAPI(title="MovieBox API", version="2.0")
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
H.update(json.loads(os.getenv("MB_HEADERS", "{}")))  # jo probe me kaam kare wo headers yahan daalo
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
        h["Authorization"] = "Bearer " + _tok["v"]  # pehle player calls mein ye missing tha
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


@app.get("/")
async def ui():
    return FileResponse("index.html")


def _item(s):
    s = s or {}
    return {"name": s.get("title"), "poster_url": (s.get("cover") or {}).get("url"),
            "slug": s.get("detailPath"), "subject_id": s.get("subjectId"), "rating": s.get("imdbRatingValue")}


@app.get("/home")
async def home():
    d = await call(f"{API}/home?host={HOST}")
    out = []
    for op in (d.get("data") or {}).get("operatingList") or []:
        if op.get("type") == "BANNER":
            items = []
            for b in (op.get("banner") or {}).get("items") or []:
                sub = b.get("subject") or {}
                items.append({
                    "name": b.get("title") or sub.get("title"),
                    "poster_url": (b.get("image") or {}).get("url") or (sub.get("cover") or {}).get("url"),
                    "slug": b.get("detailPath") or sub.get("detailPath"),
                    "subject_id": b.get("subjectId") or sub.get("subjectId"),
                })
            out.append({"section": "Banner", "items": [i for i in items if i["slug"]]})
        else:
            out.append({"section": op.get("title") or op.get("type"), "items": [_item(s) for s in op.get("subjects") or []]})
    return {"sections": out, "_debug": d.get("_http") or d.get("_err")}


@app.get("/search")
async def search(q: str = Query(..., min_length=1), page: int = 1):
    d = await call(f"{API}/subject/search", {"keyword": q, "page": page, "perPage": 24})
    inner = d.get("data") or {}
    return {"items": [_item(s) for s in inner.get("items") or inner.get("list") or []]}


@app.get("/detail/{slug}")
async def detail(slug: str):
    return await call(f"{API}/detail?detailPath={slug}")


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
    PH = {"User-Agent": UA, "Accept": "application/json", "Referer": ref, "X-Client-Info": XCI, "Accept-Language": "en-IN,en;q=0.9"}
    pd = (await call(play, hdr=PH)).get("data") or {}
    if not pd.get("hasResource"):  # ek baar fresh token se dobara try
        await ensure_token(True)
        pd = (await call(play, hdr=PH)).get("data") or {}
    src = []
    for x in pd.get("hls") or []:  # hls items dict bhi ho sakte hain, string bhi
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
    return {"has_resource": bool(src), "sources": src, "note": None if src else "Is episode ka stream nahi mila.", "cap": cap, "_domain": dom}


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
    """Subtitle fetch + SRT -> WebVTT (browser <track> sirf VTT samajhta hai)."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, **CK) as c:
        try:
            r = await c.get(u, headers={"User-Agent": UA, "Referer": _ref["v"]})
        except Exception as e:
            return Response(str(e), status_code=502)
    t = r.content.decode("utf-8-sig", "ignore").replace("\r", "")
    if not t.lstrip().startswith("WEBVTT"):
        t = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d{3})", r"\1.\2", t)
    return Response(t, media_type="text/vtt; charset=utf-8")


IN_IP = "49.205.120.45"  # sirf test ke liye ek Indian ISP range ka IP
PROBES = {
    "base": ({}, "moviebox.ph"),
    "lang_hi": ({"Accept-Language": "hi-IN,hi;q=0.9", "X-Request-Lang": "hi"}, "moviebox.ph"),
    "ip_headers": ({"X-Forwarded-For": IN_IP, "X-Real-IP": IN_IP, "CF-Connecting-IP": IN_IP, "CF-IPCountry": "IN"}, "moviebox.ph"),
    "host_h5": ({}, "h5.aoneroom.com"),
    "all_signals": ({"Accept-Language": "hi-IN,hi;q=0.9", "X-Forwarded-For": IN_IP, "X-Real-IP": IN_IP, "CF-IPCountry": "IN"}, "h5.aoneroom.com"),
}


@app.get("/probe")
async def probe():
    """Browser me kholo: /probe  -> dekho kis variant me Indian/Hindi titles aate hain."""
    out = {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, **CK) as c:
        try:
            out["server_ip_info"] = (await c.get("https://ipinfo.io/json")).json()
        except Exception as e:
            out["server_ip_info"] = str(e)
        for k, (extra, host) in PROBES.items():
            try:
                r = await c.get(f"{API}/home?host={host}", headers={**H, **extra})
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
