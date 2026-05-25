"""
O'Reilly Book Downloader — FastAPI backend
Requires an active O'Reilly Learning subscription.
"""

import io
import json as _json
import os
import re
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi import Request
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


def _parse_cookie_str(raw: str) -> dict:
    """Parse a browser Cookie header string into a dict."""
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def _make_client(token: str, extra_cookies: str = "") -> httpx.AsyncClient:
    # Start with the session token under both known names.
    cookies: dict = {"sessionid": token, "groot_sessionid": token}
    # Merge any additional cookies the caller supplied (full browser Cookie header).
    if extra_cookies:
        cookies.update(_parse_cookie_str(extra_cookies))
    return httpx.AsyncClient(
        cookies=cookies,
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
    extra_cookies: str = ""   # full browser Cookie header string (optional)
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


def _check_auth(resp) -> None:
    """Raise a clean 401 for any response that indicates an expired/invalid session."""
    if resp.status_code in (401, 403):
        raise HTTPException(401, "Session expired — please log in again.")
    # O'Reilly sometimes redirects expired sessions to the login page (HTML)
    ct = resp.headers.get("content-type", "")
    if resp.is_redirect or ("text/html" in ct and resp.is_success):
        raise HTTPException(401, "Session expired — please log in again.")


@app.get("/api/search")
async def search(request: Request, query: str, token: str, page: int = 0, limit: int = 12):
    """Search O'Reilly for books matching *query*."""
    xc = request.headers.get("X-Session-Cookies", "")
    try:
        async with _make_client(token, xc) as client:
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
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Could not reach O'Reilly: {exc}")

    _check_auth(resp)

    if not resp.is_success:
        raise HTTPException(
            resp.status_code,
            f"O'Reilly search returned HTTP {resp.status_code}.",
        )

    try:
        return resp.json()
    except Exception:
        raise HTTPException(502, "O'Reilly returned an unexpected response. Try again.")


async def _chapters_from_api(client: httpx.AsyncClient, book_id: str) -> list | None:
    """Try every known REST endpoint that might return a chapter / TOC list."""
    candidates = [
        f"{OREILLY_BASE}/api/v2/book/{book_id}/chapter/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/chapters/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/flat-toc/",
        f"{OREILLY_BASE}/api/v2/book/orm:{book_id}/",
        f"{OREILLY_BASE}/api/v2/titles/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/titles/{book_id}/",
        f"{OREILLY_BASE}/api/v1/book/{book_id}/chapter/",
        f"{OREILLY_BASE}/api/v1/book/{book_id}/",
        # EPUB-specific endpoints
        f"{OREILLY_BASE}/api/v2/epubs/orm:{book_id}/",
        f"{OREILLY_BASE}/api/v2/epubs/{book_id}/",
        f"{OREILLY_BASE}/api/v2/epubs/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/epub/",
    ]
    for url in candidates:
        try:
            r = await client.get(url)
        except httpx.RequestError:
            continue
        _check_auth(r)
        if not r.is_success:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        # Normalise to a list
        if isinstance(data, list) and data:
            return data
        if isinstance(data, dict):
            for key in ("chapters", "results", "toc", "table_of_contents", "items"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    return val
    return None


def _looks_like_chapter(obj: object) -> bool:
    """Return True if obj looks like a chapter/TOC entry."""
    if not isinstance(obj, dict):
        return False
    return bool({"url", "href", "title", "filename", "natural_key", "id"} & obj.keys())


def _find_chapters_in(obj: object, depth: int = 0) -> list | None:
    """Recursively search any parsed JSON value for a chapters/toc list."""
    if depth > 8:
        return None
    if isinstance(obj, list):
        if obj and _looks_like_chapter(obj[0]):
            return obj
        for item in obj:
            found = _find_chapters_in(item, depth + 1)
            if found:
                return found
    elif isinstance(obj, dict):
        for key in ("chapters", "toc", "table_of_contents", "items", "children"):
            val = obj.get(key)
            if isinstance(val, list) and val and _looks_like_chapter(val[0]):
                return val
        for val in obj.values():
            found = _find_chapters_in(val, depth + 1)
            if found:
                return found
    return None


async def _chapters_from_web_reader(client: httpx.AsyncClient, book_id: str) -> list:
    """
    Fetch the HTML web-reader page and extract the chapter list from any
    embedded JSON — works regardless of whether the app uses Next.js, Redux,
    or plain window.* assignments.
    """
    for slug in ("-", "book", "title"):
        url = f"{OREILLY_BASE}/library/view/{slug}/{book_id}/"
        try:
            resp = await client.get(url, timeout=20.0)
        except httpx.RequestError:
            continue
        if not resp.is_success:
            continue

        html = resp.text

        # 0. Parse __NEXT_DATA__ — Next.js server-side initial props.
        #    O'Reilly's reader is a Next.js app; SSR props often include the full TOC.
        nd_match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if nd_match:
            try:
                nd = _json.loads(nd_match.group(1))
                found = _find_chapters_in(nd)
                if found:
                    return found
            except Exception:
                pass

        # 1. Try every <script> tag — parse content as JSON or look for
        #    window.VAR = <json> and window.VAR = [<json>] assignments.
        for script_body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            script_body = script_body.strip()
            if not script_body or "chapters" not in script_body:
                continue

            # Direct JSON object/array
            for prefix in ("{", "["):
                if script_body.startswith(prefix):
                    try:
                        data = _json.loads(script_body)
                        found = _find_chapters_in(data)
                        if found:
                            return found
                    except Exception:
                        pass

            # window.VAR = {...} or window.VAR = [...]
            for m in re.finditer(
                r"window\.\w+\s*=\s*(\{[\s\S]+?\}|\[[\s\S]+?\])\s*;",
                script_body,
            ):
                try:
                    data = _json.loads(m.group(1))
                    found = _find_chapters_in(data)
                    if found:
                        return found
                except Exception:
                    pass

            # __STORE__, __STATE__, __REDUX_STATE__, etc.
            for m in re.finditer(
                r'(?:__STORE__|__STATE__|__REDUX_STATE__|__INITIAL_STATE__|__DATA__)'
                r'\s*=\s*(\{[\s\S]+?\})\s*[;<]',
                script_body,
            ):
                try:
                    data = _json.loads(m.group(1))
                    found = _find_chapters_in(data)
                    if found:
                        return found
                except Exception:
                    pass

        # 2. JSON-LD structured data
        for ld in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.DOTALL,
        ):
            try:
                data = _json.loads(ld)
                found = _find_chapters_in(data)
                if found:
                    return found
            except Exception:
                pass

    return []


@app.get("/api/debug/{book_id:path}")
async def debug_book(request: Request, book_id: str, token: str):
    """Diagnostic endpoint: probes every URL and reports HTTP status + structure."""
    xc = request.headers.get("X-Session-Cookies", "")
    probe_urls = [
        f"{OREILLY_BASE}/api/v2/book/{book_id}/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/chapter/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/flat-toc/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/chapters/",
        f"{OREILLY_BASE}/api/v1/book/{book_id}/",
        f"{OREILLY_BASE}/api/v1/book/{book_id}/chapter/",
        # ORM-prefixed identifiers
        f"{OREILLY_BASE}/api/v2/book/orm:{book_id}/",
        f"{OREILLY_BASE}/api/v2/titles/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/titles/{book_id}/",
        # EPUB-specific endpoints
        f"{OREILLY_BASE}/api/v2/epubs/orm:{book_id}/",
        f"{OREILLY_BASE}/api/v2/epubs/{book_id}/",
        f"{OREILLY_BASE}/api/v2/epubs/{book_id}/toc/",
        f"{OREILLY_BASE}/api/v2/book/{book_id}/epub/",
        f"{OREILLY_BASE}/library/view/-/{book_id}/",
    ]
    results = {}
    async with _make_client(token, xc) as client:
        for url in probe_urls:
            key = url.replace(OREILLY_BASE, "")
            try:
                r = await client.get(url, timeout=10.0)
                ct = r.headers.get("content-type", "")
                info: dict = {"status": r.status_code, "content_type": ct[:80],
                              "final_url": str(r.url)}
                if r.is_success:
                    if "json" in ct:
                        try:
                            d = r.json()
                            if isinstance(d, dict):
                                info["json_keys"] = sorted(d.keys())
                            elif isinstance(d, list):
                                info["list_len"] = len(d)
                                if d and isinstance(d[0], dict):
                                    info["item_keys"] = sorted(d[0].keys())
                        except Exception as e:
                            info["json_error"] = str(e)
                    elif "html" in ct:
                        html = r.text
                        info["html_bytes"] = len(html)
                        info["has___NEXT_DATA__"] = "__NEXT_DATA__" in html
                        info["has_toc_key"] = '"toc"' in html
                        info["has_chapters_key"] = '"chapters"' in html
                        info["window_vars"] = re.findall(r'window\.(\w+)\s*=', html)[:20]
                        info["script_types"] = list(set(
                            re.findall(r'<script[^>]+type=["\']([^"\']+)["\']', html)))
                        # Parse __NEXT_DATA__ (Next.js SSR props)
                        nd_m = re.search(
                            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                            html, re.DOTALL,
                        )
                        if nd_m:
                            try:
                                nd = _json.loads(nd_m.group(1))
                                info["__NEXT_DATA__keys"] = sorted(nd.keys())
                                pp = nd.get("props", {}).get("pageProps", {})
                                if isinstance(pp, dict):
                                    info["pageProps_keys"] = sorted(pp.keys())
                                    # Show book sub-keys if present
                                    for bk in ("book", "bookData", "initialData"):
                                        bv = pp.get(bk)
                                        if isinstance(bv, dict):
                                            info[f"pageProps.{bk}_keys"] = sorted(bv.keys())
                            except Exception as ex:
                                info["__NEXT_DATA__parse_error"] = str(ex)
                        # Parse window.orm (the Redux initial state) and show its
                        # top-level keys + any keys that look book/toc related
                        for wm in re.finditer(r'window\.orm\s*=\s*', html):
                            pos = wm.end()
                            if pos < len(html) and html[pos] == '{':
                                try:
                                    data, _ = _json.JSONDecoder().raw_decode(html, pos)
                                    if isinstance(data, dict):
                                        info["window_orm_top_keys"] = sorted(data.keys())
                                        # Recurse one level to show sub-keys
                                        detail = {}
                                        for k, v in data.items():
                                            if isinstance(v, dict):
                                                detail[k] = sorted(v.keys())
                                        info["window_orm_detail"] = detail
                                except Exception as ex:
                                    info["window_orm_parse_error"] = str(ex)
                            break   # only inspect the first window.orm
            except Exception as e:
                info = {"error": str(e)}
            results[key] = info
    # Also fetch the search result for this book to expose all its fields —
    # it might contain a chapters_url or content_url we haven't tried.
    alt_ids: list[str] = []
    try:
        async with _make_client(token, xc) as src:
            search_resp = await src.get(
                f"{OREILLY_BASE}/api/v2/search/",
                params={"query": book_id, "formats": "book", "limit": "5"},
            )
            if search_resp.is_success:
                for item in search_resp.json().get("results", []):
                    if book_id in str(item.get("id", "")) or book_id in str(item.get("archive_id", "")):
                        results["_search_result_fields"] = {
                            k: v for k, v in item.items()
                            if not isinstance(v, str) or len(v) < 300
                        }
                        for field in ("isbn", "archive_id"):
                            val = str(item.get(field, "")).strip()
                            if val and val != book_id:
                                alt_ids.append(val)
                        break
    except Exception as e:
        results["_search_result_error"] = str(e)

    # Probe EPUB/TOC endpoints using alternative ISBNs found in the search result
    if alt_ids:
        results["_alt_ids_discovered"] = alt_ids
        async with _make_client(token, xc) as altc:
            for alt_id in alt_ids:
                for path in (
                    f"/api/v2/book/{alt_id}/",
                    f"/api/v2/book/{alt_id}/chapter/",
                    f"/api/v2/book/{alt_id}/flat-toc/",
                    f"/api/v2/book/{alt_id}/toc/",
                    f"/api/v1/book/{alt_id}/chapter/",
                    f"/api/v2/epubs/orm:{alt_id}/",
                    f"/api/v2/epubs/{alt_id}/",
                ):
                    key = f"[alt:{alt_id}]{path}"
                    try:
                        r = await altc.get(f"{OREILLY_BASE}{path}", timeout=10.0)
                        ct = r.headers.get("content-type", "")
                        info: dict = {"status": r.status_code, "content_type": ct[:80]}
                        if r.is_success and "json" in ct:
                            try:
                                d = r.json()
                                if isinstance(d, dict):
                                    info["json_keys"] = sorted(d.keys())
                                elif isinstance(d, list):
                                    info["list_len"] = len(d)
                                    if d and isinstance(d[0], dict):
                                        info["item_keys"] = sorted(d[0].keys())
                            except Exception:
                                pass
                        results[key] = info
                    except Exception as e:
                        results[key] = {"error": str(e)}

    return {"book_id": book_id, "extra_cookies_sent": bool(xc), "results": results}


async def _discover_alt_ids(client: httpx.AsyncClient, book_id: str) -> list[str]:
    """
    Search O'Reilly for *book_id* and return any alternative ISBNs/IDs found
    in the search result (e.g. the `isbn` field differs from `archive_id`).
    """
    alt_ids: list[str] = []
    try:
        sr = await client.get(
            f"{OREILLY_BASE}/api/v2/search/",
            params={"query": book_id, "formats": "book", "limit": "5"},
            timeout=10.0,
        )
        if not sr.is_success:
            return alt_ids
        for item in sr.json().get("results", []):
            item_id = str(item.get("id", ""))
            archive_id = str(item.get("archive_id", ""))
            if book_id not in item_id and book_id not in archive_id:
                continue
            for field in ("isbn", "archive_id"):
                val = str(item.get(field, "")).strip()
                if val and val != book_id and val not in alt_ids:
                    alt_ids.append(val)
    except Exception:
        pass
    return alt_ids


@app.get("/api/book/{book_id:path}")
async def get_book(request: Request, book_id: str, token: str):
    """Return full book metadata + chapter list using a cascade of fallbacks."""
    xc = request.headers.get("X-Session-Cookies", "")
    async with _make_client(token, xc) as client:
        # 1. Book-detail endpoint — works for older ISBNs, has metadata + chapters
        for url in (
            f"{OREILLY_BASE}/api/v2/book/{book_id}/",
            f"{OREILLY_BASE}/api/v2/book/{book_id}",
        ):
            try:
                resp = await client.get(url)
            except httpx.RequestError as exc:
                raise HTTPException(502, f"Could not reach O'Reilly: {exc}")
            _check_auth(resp)
            if resp.is_success:
                try:
                    data = resp.json()
                    return data
                except Exception:
                    pass

        # Discover alternative ISBNs for this book (e.g. EPUB isbn ≠ archive_id)
        alt_ids = await _discover_alt_ids(client, book_id)

        # 2. Chapter-list API endpoints — try primary id then any alternatives
        for try_id in [book_id] + alt_ids:
            chapters = await _chapters_from_api(client, try_id)
            if chapters is not None:
                return {"id": book_id, "title": "", "authors": [], "chapters": chapters}

        # 3. Web-reader HTML scrape — works for anything the browser can open
        chapters = await _chapters_from_web_reader(client, book_id)
        if chapters:
            return {"id": book_id, "title": "", "authors": [], "toc": chapters}

    raise HTTPException(
        404,
        f"Could not retrieve chapter list for book '{book_id}'. "
        "Try opening the book in your browser first to ensure your session has access, "
        "then get a fresh sessionid cookie.",
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

    async with _make_client(req.token, req.extra_cookies) as client:
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
