"""FastAPI web router serving the Mobile-First LINE MiniApp shell and public LIFF config."""

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"

_VERSIONED_PREFIX_RE = re.compile(r"^v/[0-9a-zA-Z_-]{4,32}/(.+)$")
_NO_CACHE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "CDN-Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _compute_asset_version() -> str:
    """Compute a deterministic short cache-busting hash from MiniApp static assets."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and path.suffix in {".html", ".css", ".js"}:
            st = path.stat()
            rel = path.relative_to(STATIC_DIR).as_posix()
            digest.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()[:10]


class NoCacheStaticFiles(StaticFiles):
    """Serve MiniApp CSS/JS without CDN/WebView stale caching (supports /v/{ver}/ prefix)."""

    async def get_response(self, path: str, scope: Any) -> Any:
        clean = path.lstrip("/")
        match = _VERSIONED_PREFIX_RE.match(clean)
        if match:
            clean = match.group(1)
        if ".." in PurePosixPath(clean).parts:
            raise HTTPException(status_code=404)
        resp = await super().get_response(clean, scope)
        for key, value in _NO_CACHE_HEADERS.items():
            resp.headers[key] = value
        return resp


@router.get("/miniapp", include_in_schema=False)
@router.get("/miniapp/", include_in_schema=False)
@router.get("/liff", include_in_schema=False)
async def serve_miniapp_shell() -> HTMLResponse:
    asset_ver = _compute_asset_version()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Version prefix on the base asset path keeps all nested relative ESM imports
    # (./auth.js, ./api.js, ./views/*.js, ../utils.js) in one shared module graph.
    html = html.replace("/miniapp/static/", f"/miniapp/static/v/{asset_ver}/")
    meta_tag = f'  <meta name="miniapp-version" content="{asset_ver}">\n</head>'
    if "</head>" in html:
        html = html.replace("</head>", meta_tag, 1)
    return HTMLResponse(content=html, headers=dict(_NO_CACHE_HEADERS))


@router.get("/miniapp/config.json", include_in_schema=False)
async def serve_miniapp_config() -> JSONResponse:
    liff_id = (getattr(settings, "LIFF_ID", None) or "").strip()
    payload: Dict[str, Any] = {
        "appName": "Investment Homelab",
        "liffId": liff_id or None,
        "liffConfigured": bool(liff_id),
        "phase": 5,
        "assetVersion": _compute_asset_version(),
        "features": ["brokers", "portfolio", "orders", "nego", "trade"],
    }
    return JSONResponse(payload, headers=dict(_NO_CACHE_HEADERS))
