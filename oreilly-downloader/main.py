"""
O'Reilly Book Downloader — FastAPI backend
Requires an active O'Reilly Learning subscription.
"""

import io
import os
import re
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="O'Reilly Book Downloader", version="1.0.0")

from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"Server error: {exc}"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OREILLY_BASE = "https://learning.oreilly.com"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://learning.oreilly.com/",
    "Origin": "https://learning.oreilly.com",
}


def _make_client(token: str) -> httpx.AsyncClient:
    # Send token under both names — O'Reilly uses 'groot_sessionid' in newer
    # versions and 'sessionid' in older ones.
    return httpx.AsyncClient(
        cookies={"sessionid": token, "groot_sessionid": token},
        headers=BASE_HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=90.0),
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenVerifyRequest(BaseModel):
    token: str


class DownloadRequest(BaseModel):
    token: str
    book_id: str
    chapter_urls: List[str]   # full or relative chapter API URLs
    format: str               # "md" | "pdf"
    title: str


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def login(req: LoginRequest):
    """Authenticate with O'Reilly and return a session token."""
    async with httpx.AsyncClient(
        headers=BASE_HEADERS,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        resp = await client.post(
            f"{OREILLY_BASE}/api/v2/account/login-unified/",
            json={"email": req.email, "password": req.password},
        )

    if resp.status_code in (400, 401, 403):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    cookies = dict(resp.cookies)
    sessionid = cookies.get("sessionid")
    if not sessionid:
        raise HTTPException(
            status_code=401,
            detail="Login failed — could not obtain session token. Check credentials.",
        )

    try:
        body = resp.json()
        name = body.get("name") or body.get("first_name") or req.email.split("@")[0]
    except Exception:
        name = req.email.split("@")[0]

    return {"token": sessionid, "name": name}


@app.post("/api/verify-token")
async def verify_token(req: TokenVerifyRequest):
    """
    Validate a session cookie from the browser (SSO flow).
    Uses the search API as a lightweight auth probe since /api/v2/me/ is unreliable.
    """
    token = req.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token cannot be empty.")

    # Probe with a minimal search request — cheapest reliable authenticated call
    try:
        async with _make_client(token) as client:
            probe = await client.get(
                f"{OREILLY_BASE}/api/v2/search/",
                params={"query": "python", "formats": "book", "limit": "1"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach O'Reilly: {exc}")

    if probe.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")
    if not probe.is_success:
        raise HTTPException(
            status_code=401,
            detail=f"Session token rejected by O'Reilly (HTTP {probe.status_code}).",
        )

    # Try to fetch a display name — best-effort, never fail hard here
    name = "User"
    for path in ("/api/v2/me/", "/api/v2/user/", "/api/v2/account/"):
        try:
            async with _make_client(token) as client:
                me = await client.get(f"{OREILLY_BASE}{path}")
            if me.is_success and "json" in me.headers.get("content-type", ""):
                data = me.json()
                name = (
                    data.get("name")
                    or data.get("first_name")
                    or data.get("username")
                    or data.get("email", "").split("@")[0]
                    or "User"
                )
                if name and name != "User":
                    break
        except Exception:
            continue

    return {"token": token, "name": name}


@app.get("/api/search")
async def search(query: str, token: str, page: int = 0, limit: int = 12):
    """Search O'Reilly for books matching *query*."""
    async with _make_client(token) as client:
        resp = await client.get(
            f"{OREILLY_BASE}/api/v2/search/",
            params={
                "query": query,
                "formats": "book",
                "limit": limit,
                "offset": page * limit,
                "include_facets": "false",
            },
        )

    if resp.status_code == 401:
        raise HTTPException(401, "Session expired — please log in again.")
    resp.raise_for_status()
    return resp.json()


@app.get("/api/book/{book_id:path}")
async def get_book(book_id: str, token: str):
    """Return full book metadata including the chapter list.
    book_id may be an ISBN, a slug, or a URL-encoded path segment.
    We try several endpoint patterns because O'Reilly's API accepts
    different identifier forms depending on the book.
    """
    candidates = [
        f"{OREILLY_BASE}/api/v2/book/{book_id}/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}",
    ]

    last_status = None
    async with _make_client(token) as client:
        for url in candidates:
            try:
                resp = await client.get(url)
            except httpx.RequestError as exc:
                raise HTTPException(502, f"Could not reach O'Reilly: {exc}")

            if resp.status_code == 401:
                raise HTTPException(401, "Session expired — please log in again.")
            if resp.is_success:
                return resp.json()
            last_status = resp.status_code

    raise HTTPException(
        404,
        f"Book not found (id: {book_id!r}, last HTTP status: {last_status}).",
    )


# ---------------------------------------------------------------------------
# Chapter content helpers
# ---------------------------------------------------------------------------

async def _fetch_chapter_html(client: httpx.AsyncClient, url: str) -> str:
    """
    Fetch chapter HTML, handling two possible shapes:
      - JSON response with a 'content' / 'body' key
      - Raw HTML response
    Falls back to fetching a 'content_url' when present in the JSON.
    """
    if not url.startswith("http"):
        url = f"{OREILLY_BASE}{url}"

    try:
        resp = await client.get(url)
    except httpx.RequestError:
        return ""

    if resp.status_code != 200:
        return ""

    ct = resp.headers.get("content-type", "")
    if "json" in ct:
        try:
            data = resp.json()
        except Exception:
            return ""
        html = data.get("content") or data.get("body") or data.get("text") or ""
        if not html and data.get("content_url"):
            try:
                cr = await client.get(data["content_url"])
                html = cr.text if cr.status_code == 200 else ""
            except Exception:
                pass
        return html

    # Raw HTML / XHTML
    return resp.text


def _html_to_markdown(html: str) -> str:
    from markdownify import markdownify as md

    return md(
        html,
        heading_style="ATX",
        strip=["script", "style", "noscript", "iframe"],
        newline_style="backslash",
    )


def _build_pdf(title: str, html_parts: List[str]) -> bytes:
    from weasyprint import HTML as WP

    body = "<hr class='sep'>".join(html_parts)
    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: Georgia, 'Times New Roman', serif;
    max-width: 820px;
    margin: auto;
    padding: 2.5rem 3rem;
    color: #1a1a1a;
    line-height: 1.75;
    font-size: 15px;
  }}
  h1 {{ color: #c84b31; font-size: 2.2em; margin: 0 0 0.25em; }}
  h2 {{ color: #2c3e50; border-bottom: 2px solid #e8e8e8; padding-bottom: 0.3em; margin-top: 2em; }}
  h3, h4 {{ color: #34495e; margin-top: 1.5em; }}
  a {{ color: #3498db; text-decoration: none; }}
  code {{
    font-family: 'Courier New', monospace;
    background: #f5f5f5;
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 0.87em;
    color: #c0392b;
  }}
  pre {{
    background: #f5f5f5;
    padding: 1em 1.2em;
    border-left: 4px solid #c84b31;
    border-radius: 4px;
    overflow-x: auto;
    margin: 1.2em 0;
  }}
  pre code {{ background: none; padding: 0; color: inherit; }}
  blockquote {{
    border-left: 4px solid #bdc3c7;
    margin: 1em 0;
    padding: 0.5em 1em;
    color: #555;
    background: #fafafa;
  }}
  img {{ max-width: 100%; height: auto; display: block; margin: 1em 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: 0.92em; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5em 0.8em; text-align: left; }}
  th {{ background: #f0f0f0; font-weight: bold; }}
  .sep {{ border: none; border-top: 2px dashed #ccc; margin: 3em 0; }}
  @page {{ margin: 2.5cm 2cm; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""
    return WP(string=full_html).write_pdf()


# ---------------------------------------------------------------------------
# Download endpoint
# ---------------------------------------------------------------------------

@app.post("/api/download")
async def download(req: DownloadRequest):
    """
    Fetch the requested chapters and stream back a .md or .pdf file.
    """
    if req.format not in ("md", "pdf"):
        raise HTTPException(400, f"Unknown format '{req.format}'. Use 'md' or 'pdf'.")

    async with _make_client(req.token) as client:
        html_parts = []
        for url in req.chapter_urls:
            html = await _fetch_chapter_html(client, url)
            if html:
                html_parts.append(html)

    if not html_parts:
        raise HTTPException(
            422,
            "No content could be fetched. "
            "The chapters may require a higher subscription tier or the session expired.",
        )

    safe = re.sub(r"[^\w\s\-]", "", req.title).strip().replace(" ", "_")[:80] or "book"

    if req.format == "md":
        sections = [_html_to_markdown(h) for h in html_parts]
        content = f"# {req.title}\n\n" + "\n\n---\n\n".join(sections)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{safe}.md"'},
        )

    # PDF
    pdf_bytes = _build_pdf(req.title, html_parts)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Serve the frontend SPA last (catch-all)
# ---------------------------------------------------------------------------

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
