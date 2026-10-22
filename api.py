"""
MovieBox API — mb4.py jaisa hi header-style, lekin ab token HAR request se pehle
guarantee se fetch hota hai (yehi pichla bug tha — IP block nahi tha).
"""
import json
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="MovieBox API (fixed)", version="1.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def serve_ui():
    # index.html isi folder mein hona chahiye, api.py ke saath
    return FileResponse("index.html")

API = "https://h5-api.aoneroom.com/wefeed-h5api-bff"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

H = {
    "User-Agent": UA,
    "Referer": "https://moviebox.ph/",
    "Origin": "https://moviebox.ph",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Request-Lang": "en",
    "X-Client-Info": '{"timezone":"Asia/Kolkata"}',
}

_token = ""


async def _ensure_token():
    """Token khaali ho to guest token fetch karo /home se — har request se pehle guarantee."""
    global _token
    if _token:
        return _token
    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        try:
            r = await client.get(f"{API}/home?host=moviebox.ph", headers=H)
        except Exception:
            return ""
        xu = r.headers.get("x-user")
        if xu:
            try:
                tok = json.loads(xu).get("token")
                if tok:
                    _token = tok
            except Exception:
                pass
    return _token


async def call(url: str, payload: dict = None, hdr: dict = None) -> dict:
    global _token
    # Agar custom header di hai (player calls) to token mat jodo, warna hamesha ensure karo
    if hdr is not None:
        h = dict(hdr)
    else:
        await _ensure_token()
        h = dict(H)
        if _token:
            h["Authorization"] = "Bearer " + _token

    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        try:
            if payload is not None:
                r = await client.post(url, headers=h, content=json.dumps(payload))
            else:
                r = await client.get(url, headers=h)
        except Exception as e:
            return {"_err": str(e)}

        xu = r.headers.get("x-user")
        if xu:
            try:
                tok = json.loads(xu).get("token")
                if tok:
                    _token = tok
            except Exception:
                pass

        if r.status_code != 200:
            return {"_http": r.status_code, "_body": r.text[:300]}
        try:
            return r.json()
        except Exception:
            return {}


@app.get("/home")
async def home():
    d = await call(f"{API}/home?host=moviebox.ph")
    ops = (d.get("data") or {}).get("operatingList") or []
    sections = []
    for op in ops:
        t = op.get("type")
        if t == "BANNER":
            items = (op.get("banner") or {}).get("items") or []
            sections.append({"section": "Banner", "count": len(items), "items": items})
        else:
            subs = op.get("subjects") or []
            sections.append({
                "section": op.get("title", t),
                "count": len(subs),
                "items": [{
                    "name": s.get("title"),
                    "poster_url": (s.get("cover") or {}).get("url"),
                    "slug": s.get("detailPath"),
                    "subject_id": s.get("subjectId"),
                } for s in subs]
            })
    return {"status": "success", "_debug": d.get("_http") or d.get("_err"), "sections": sections}


@app.get("/search")
async def search(q: str = Query(..., min_length=1), page: int = 1):
    d = await call(f"{API}/subject/search", {"keyword": q, "page": page, "perPage": 20})
    inner = d.get("data") or {}
    items = inner.get("items") or inner.get("list") or []
    out = [{
        "name": s.get("title"),
        "poster_url": (s.get("cover") or {}).get("url"),
        "slug": s.get("detailPath"),
        "subject_id": s.get("subjectId"),
    } for s in items]
    return {"query": q, "total": len(out), "items": out, "_debug": d.get("_http") or d.get("_err"), "_body": d.get("_body")}


@app.get("/detail/{slug}")
async def detail(slug: str):
    d = await call(f"{API}/detail?detailPath={slug}")
    return d


@app.get("/api/stream/{subject_id}")
async def stream(subject_id: str, detail_path: str, se: int = 1, ep: int = 1):
    await _ensure_token()
    dom_data = await call(f"{API}/media-player/get-domain")
    dom = (dom_data.get("data") or "https://netfilm.world").rstrip("/")
    ref = f"{dom}/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    play_url = f"{dom}/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
    PH = {"User-Agent": UA, "Accept": "application/json", "Referer": ref, "X-Client-Info": '{"timezone":"Asia/Kolkata"}'}
    pd = (await call(play_url, hdr=PH)).get("data") or {}
    streams = pd.get("streams") or []
    return {
        "subject_id": subject_id, "se": se, "ep": ep,
        "has_resource": pd.get("hasResource", False),
        "sources": [{
            "resolution": f"{s.get('resolutions')}p",
            "format": s.get("format"),
            "url": s.get("url"),
        } for s in streams],
        "hls": pd.get("hls", []),
        "dash": pd.get("dash", []),
        "note": None if streams else "No stream found for this episode."
    }


@app.get("/api/stream/{subject_id}/captions")
async def captions(subject_id: str, detail_path: str, se: int = 1, ep: int = 1):
    await _ensure_token()
    dom_data = await call(f"{API}/media-player/get-domain")
    dom = (dom_data.get("data") or "https://netfilm.world").rstrip("/")
    ref = f"{dom}/spa/videoPlayPage/movies/{detail_path}?id={subject_id}&type=/movie/detail&detailSe={se}&detailEp={ep}&lang=en"
    play_url = f"{dom}/wefeed-h5api-bff/subject/play?subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
    PH = {"User-Agent": UA, "Accept": "application/json", "Referer": ref, "X-Client-Info": '{"timezone":"Asia/Kolkata"}'}
    pd = (await call(play_url, hdr=PH)).get("data") or {}
    streams = pd.get("streams") or []
    dash = pd.get("dash") or []
    sid = fmt = None
    if streams:
        sid, fmt = streams[0].get("id"), streams[0].get("format", "MP4")
    elif dash:
        sid, fmt = dash[0].get("id"), dash[0].get("format", "DASH")
    if not sid:
        return {"count": 0, "captions": []}
    cap_url = f"{API}/subject/caption?format={fmt}&id={sid}&subjectId={subject_id}&detailPath={detail_path}"
    cd = (await call(cap_url)).get("data")
    caps = (cd.get("captions") if isinstance(cd, dict) else cd) or []
    return {"count": len(caps), "captions": caps}
