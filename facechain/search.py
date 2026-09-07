"""Stage 2: genuine web / social-media search for a face.

The pipeline is deliberately provider-agnostic and evidence-driven:

    build_queries()  ->  live providers (reddit / ddg-images / wikimedia)
                     ->  concurrent image download + sha256 dedupe
                     ->  face detection + embedding (stage 1)
                     ->  cosine similarity vs the input face
                     ->  SearchReport with a full audit trail

Nothing here is hardcoded: candidates come from live HTTP calls and a post is
only ever reported as a match because its *face embedding* cleared the
threshold, never because a caption looked promising.

Debug entry point::

    python -m facechain.search "Elon Musk"
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import requests

from facechain import config
from facechain.types import FaceProfile, PostMatch, SearchReport

ProgressFn = Callable[[str], None]

# Providers, in the order they are fanned out.
ALL_PROVIDERS: List[str] = ["reddit", "ddg_images", "wikimedia"]

# Subreddits that reliably carry people photos; searched in addition to
# the site-wide /search.json endpoint.
REDDIT_SUBREDDITS: List[str] = ["pics", "photographs", "portraits"]

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MIN_IMAGE_BYTES = 1024

# Magic-number prefixes we accept as "really an image".
_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
}

_HOST_LOCK = threading.Lock()
_LAST_HIT: Dict[str, float] = {}
_HOST_DELAY = 0.35  # seconds between requests to the same host


# --------------------------------------------------------------------------
# Candidate container
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """One un-verified search hit, before any face work has been done."""

    platform: str
    post_url: str
    image_url: str
    title: str = ""
    author: str = ""
    posted_at: str = ""
    query: str = ""
    discovered_via: str = "keyword-search"
    metadata: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """Process-wide requests.Session carrying the project User-Agent."""
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        _SESSION = s
    return _SESSION


def _polite(url: str) -> None:
    """Sleep just enough to avoid hammering a single host."""
    host = urlparse(url).netloc.lower()
    with _HOST_LOCK:
        last = _LAST_HIT.get(host, 0.0)
        wait = _HOST_DELAY - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _LAST_HIT[host] = time.monotonic()


def _get(url: str, *, params: Optional[Dict[str, Any]] = None,
         stream: bool = False, retries: int = 1) -> Optional[requests.Response]:
    """GET with the shared session, politeness delay and one retry."""
    session = get_session()
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            _polite(url)
            resp = session.get(
                url, params=params, timeout=config.HTTP_TIMEOUT, stream=stream
            )
            if resp.status_code == 200:
                return resp
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
        except Exception as exc:  # noqa: BLE001 - providers must never crash the run
            last_exc = exc
        if attempt < retries:
            time.sleep(0.6 + random.random() * 0.4)
    if last_exc is not None:
        _log_debug(f"GET failed {url}: {last_exc}")
    return None


def _log_debug(msg: str) -> None:
    if __debug__ and _DEBUG:
        print(f"[search] {msg}", file=sys.stderr)


_DEBUG = False


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------

GENERIC_QUERIES: List[str] = [
    "portrait photo person face",
    "reddit selfie portrait",
    "headshot photograph person",
    "official portrait photo",
]


def build_queries(profile: FaceProfile, hints: Optional[List[str]] = None) -> List[str]:
    """Turn caller hints (a name, a subject term) into focused search queries.

    With no hints we fall back to generic people-photo queries so the pipeline
    still performs a real search instead of doing nothing.
    """
    queries: List[str] = []
    clean_hints = [h.strip() for h in (hints or []) if h and h.strip()]

    for hint in clean_hints:
        queries.extend(
            [
                hint,
                f"{hint} reddit",
                f"{hint} portrait",
                f"{hint} photo",
            ]
        )

    if not queries:
        queries.extend(GENERIC_QUERIES)
        # A hint-free run still gets a little signal from the source filename.
        stem = Path(profile.source_path).stem.replace("_", " ").replace("-", " ").strip()
        if stem and not stem.isdigit() and len(stem) > 2:
            queries.insert(0, f"{stem} photo")

    seen: set[str] = set()
    unique: List[str] = []
    for q in queries:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(q.strip())
    return unique


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _looks_like_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(IMAGE_EXTS)


def _reddit_image_url(data: Dict[str, Any]) -> str:
    """Best available image URL for a reddit post, preview first."""
    try:
        images = data.get("preview", {}).get("images") or []
        if images:
            src = images[0].get("source", {}).get("url")
            if src:
                return html.unescape(src)
    except Exception:  # noqa: BLE001
        pass
    direct = data.get("url_overridden_by_dest") or data.get("url") or ""
    if isinstance(direct, str) and _looks_like_image_url(direct):
        return html.unescape(direct)
    return ""


def _parse_reddit_listing(payload: Dict[str, Any], query: str) -> List[Candidate]:
    out: List[Candidate] = []
    children = payload.get("data", {}).get("children") or []
    for child in children:
        data = child.get("data") or {}
        image_url = _reddit_image_url(data)
        permalink = data.get("permalink") or ""
        if not image_url or not permalink:
            continue
        out.append(
            Candidate(
                platform="reddit",
                post_url=f"https://www.reddit.com{permalink}",
                image_url=image_url,
                title=(data.get("title") or "")[:300],
                author=data.get("author") or "",
                posted_at=_iso(data.get("created_utc")),
                query=query,
                discovered_via="keyword-search",
                metadata={
                    "subreddit": data.get("subreddit") or "",
                    "score": data.get("score"),
                    "num_comments": data.get("num_comments"),
                    "over_18": data.get("over_18"),
                },
            )
        )
    return out


def _search_reddit(query: str, limit: int = 25) -> List[Candidate]:
    """Reddit's free public JSON search API (no key, custom UA required)."""
    results: List[Candidate] = []
    endpoints: List[tuple[str, Dict[str, Any]]] = [
        (
            "https://www.reddit.com/search.json",
            {"q": query, "limit": limit, "type": "link", "raw_json": 1},
        )
    ]
    for sub in REDDIT_SUBREDDITS:
        endpoints.append(
            (
                f"https://www.reddit.com/r/{sub}/search.json",
                {
                    "q": query,
                    "limit": limit,
                    "restrict_sr": 1,
                    "type": "link",
                    "raw_json": 1,
                    "sort": "relevance",
                },
            )
        )

    for url, params in endpoints:
        resp = _get(url, params=params)
        if resp is None:
            continue
        try:
            results.extend(_parse_reddit_listing(resp.json(), query))
        except (ValueError, json.JSONDecodeError):
            continue
    return results


def _ddgs_class():
    """Import DDGS from whichever package name is installed."""
    try:
        from ddgs import DDGS  # type: ignore
        return DDGS
    except Exception:  # noqa: BLE001
        from duckduckgo_search import DDGS  # type: ignore
        return DDGS


def _search_ddg_images(query: str, limit: int = 25) -> List[Candidate]:
    """DuckDuckGo image search - the reverse-image surface reachable key-free."""
    DDGS = _ddgs_class()
    rows: Sequence[Dict[str, Any]] = []
    with DDGS() as ddgs:
        rows = ddgs.images(query=query, max_results=limit) or []

    out: List[Candidate] = []
    for row in rows:
        image_url = row.get("image") or ""
        if not image_url:
            continue
        post_url = row.get("url") or image_url
        source = (row.get("source") or "").strip().lower()
        if not source:
            source = urlparse(post_url).netloc.lower().replace("www.", "")
        out.append(
            Candidate(
                platform=source or "web",
                post_url=post_url,
                image_url=image_url,
                title=(row.get("title") or "")[:300],
                query=query,
                discovered_via="reverse-image-search",
                metadata={
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "thumbnail": row.get("thumbnail"),
                },
            )
        )
    return out


def _search_wikimedia(query: str, limit: int = 20) -> List[Candidate]:
    """Wikimedia Commons search API - free, key-free, real file pages."""
    resp = _get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 1024,
            "format": "json",
            "formatversion": 2,
        },
    )
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []

    pages = payload.get("query", {}).get("pages") or []
    if isinstance(pages, dict):  # formatversion=1 shape
        pages = list(pages.values())

    out: List[Candidate] = []
    for page in pages:
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        image_url = info.get("thumburl") or info.get("url") or ""
        if not image_url:
            continue
        meta = info.get("extmetadata") or {}

        def _meta(key: str) -> str:
            raw = (meta.get(key) or {}).get("value") or ""
            return html.unescape(str(raw))[:300]

        out.append(
            Candidate(
                platform="wikimedia",
                post_url=info.get("descriptionurl")
                or f"https://commons.wikimedia.org/wiki/{page.get('title', '')}",
                image_url=image_url,
                title=page.get("title") or "",
                author=_meta("Artist"),
                posted_at=_meta("DateTimeOriginal"),
                query=query,
                discovered_via="keyword-search",
                metadata={
                    "license": _meta("LicenseShortName"),
                    "credit": _meta("Credit"),
                    "full_url": info.get("url") or "",
                },
            )
        )
    return out


_PROVIDER_FNS: Dict[str, Callable[[str], List[Candidate]]] = {
    "reddit": _search_reddit,
    "ddg_images": _search_ddg_images,
    "wikimedia": _search_wikimedia,
}


def harvest_candidates(
    queries: Sequence[str],
    providers: Optional[Sequence[str]] = None,
    *,
    progress: Optional[ProgressFn] = None,
    limit: Optional[int] = None,
) -> tuple[List[Candidate], List[str], List[str]]:
    """Fan out across providers and queries.

    Returns ``(candidates, providers_that_worked, notes)``. Each provider call
    is independently wrapped so one dead provider never kills the run.
    """
    names = list(providers) if providers else list(ALL_PROVIDERS)
    out: List[Candidate] = []
    working: List[str] = []
    notes: List[str] = []
    seen_urls: set[str] = set()

    for query in queries:
        for name in names:
            fn = _PROVIDER_FNS.get(name)
            if fn is None:
                notes.append(f"unknown provider '{name}' skipped")
                continue
            try:
                rows = fn(query)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{name} failed on '{query}': {type(exc).__name__}: {exc}")
                continue
            fresh = 0
            for cand in rows:
                key = cand.image_url.split("?")[0]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                out.append(cand)
                fresh += 1
            if rows and name not in working:
                working.append(name)
            if progress:
                progress(f"{name}: '{query}' -> {len(rows)} hits ({fresh} new)")
            if limit is not None and len(out) >= limit:
                return out[:limit], working, notes
    return out, working, notes


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _is_image_bytes(data: bytes) -> bool:
    if len(data) < MIN_IMAGE_BYTES:
        return False
    for magic in _MAGIC:
        if data.startswith(magic):
            return True
    # WebP: RIFF....WEBP
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def download_image(url: str) -> Optional[bytes]:
    """Download an image, capped at 8 MB, verified by magic number.

    Returns None for anything that is not really an image.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    resp = _get(url, stream=True)
    if resp is None:
        return None
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not (ctype.startswith("image/") or "octet-stream" in ctype):
            return None
        chunks: List[bytes] = []
        total = 0
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                return None
        data = b"".join(chunks)
    except Exception:  # noqa: BLE001
        return None
    finally:
        resp.close()
    return data if _is_image_bytes(data) else None


def _cache_path(sha: str) -> Path:
    return Path(config.CACHE_DIR) / f"{sha[:16]}.jpg"


def _cache_write(sha: str, data: bytes) -> str:
    path = _cache_path(sha)
    try:
        if not path.exists():
            path.write_bytes(data)
        return str(path)
    except OSError:
        return ""


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def find_matching_post(
    profile: FaceProfile,
    hints: Optional[List[str]] = None,
    max_candidates: int = config.MAX_CANDIDATES,
    threshold: float = config.MATCH_THRESHOLD,
    providers: Optional[List[str]] = None,
    *,
    progress: Optional[ProgressFn] = None,
) -> SearchReport:
    """Search the live web for a post whose face matches ``profile``.

    Candidates are harvested from real providers, downloaded concurrently,
    then face-encoded and compared on the main thread (OpenCV models are not
    thread-safe). Only cosine similarity decides what counts as a match.
    """
    started = time.monotonic()

    def say(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    queries = build_queries(profile, hints)
    report = SearchReport(queries=queries, threshold=threshold)

    say(f"built {len(queries)} queries")
    candidates, working, notes = harvest_candidates(
        queries, providers, progress=progress, limit=max_candidates * 3
    )
    report.providers = working
    report.notes.extend(notes)
    report.candidates_seen = len(candidates)
    say(f"harvested {len(candidates)} candidates from {len(working)} providers")

    if not candidates:
        report.notes.append("no candidates returned by any provider")
        report.elapsed_seconds = round(time.monotonic() - started, 3)
        return report

    # Stage-1 API, imported lazily so importing this module never depends on it.
    try:
        from facechain.face import compare, encode_image_bytes
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"face module unavailable: {type(exc).__name__}: {exc}")
        report.elapsed_seconds = round(time.monotonic() - started, 3)
        return report

    seen_hashes: set[str] = set()
    scored: List[PostMatch] = []
    examined = 0
    have_match = False

    # Download in parallel, encode serially as results land.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        batch = 16
        index = 0
        while index < len(candidates) and examined < max_candidates:
            window = candidates[index : index + batch]
            index += batch
            futures = {pool.submit(download_image, c.image_url): c for c in window}
            for future in concurrent.futures.as_completed(futures):
                cand = futures[future]
                try:
                    data = future.result()
                except Exception:  # noqa: BLE001
                    data = None
                if not data:
                    continue
                report.candidates_downloaded += 1

                sha = hashlib.sha256(data).hexdigest()
                if sha in seen_hashes:
                    continue
                seen_hashes.add(sha)

                local_path = _cache_write(sha, data)
                examined += 1

                try:
                    face = encode_image_bytes(data, source_label=cand.image_url)
                except Exception as exc:  # noqa: BLE001
                    report.notes.append(
                        f"encode failed for {cand.image_url[:80]}: {type(exc).__name__}"
                    )
                    continue
                if face is None:
                    continue
                report.candidates_with_faces += 1

                try:
                    sim = float(compare(profile.embedding, face.embedding))
                except Exception as exc:  # noqa: BLE001
                    report.notes.append(f"compare failed: {type(exc).__name__}: {exc}")
                    continue

                match = PostMatch(
                    platform=cand.platform,
                    post_url=cand.post_url,
                    image_url=cand.image_url,
                    title=cand.title,
                    author=cand.author,
                    posted_at=cand.posted_at,
                    similarity=round(sim, 6),
                    is_match=sim >= threshold,
                    query=cand.query,
                    discovered_via=cand.discovered_via,
                    image_sha256=sha,
                    local_image_path=local_path,
                    bbox=list(face.bbox),
                    metadata=dict(cand.metadata),
                )
                scored.append(match)
                say(
                    f"[{examined}/{max_candidates}] {cand.platform} "
                    f"sim={sim:.3f} {'MATCH' if match.is_match else ''}"
                )
                if match.is_match:
                    have_match = True

            # Fast demo, but only after real work: match found + >= 12 examined.
            if have_match and examined >= 12:
                report.notes.append(
                    f"early stop: match found after examining {examined} candidates"
                )
                break

    scored.sort(key=lambda m: m.similarity, reverse=True)
    report.matches = [m for m in scored if m.is_match]
    report.near_misses = [
        m
        for m in scored
        if not m.is_match and m.similarity >= config.NEAR_MISS_THRESHOLD
    ][:5]
    report.best_match = report.matches[0] if report.matches else None
    report.elapsed_seconds = round(time.monotonic() - started, 3)

    if report.best_match is None:
        report.notes.append(
            f"no candidate cleared threshold {threshold}; "
            f"best score was {scored[0].similarity if scored else 0.0}"
        )
    say(
        f"done in {report.elapsed_seconds}s: {len(report.matches)} matches, "
        f"{len(report.near_misses)} near misses"
    )
    return report


# --------------------------------------------------------------------------
# Debug CLI: python -m facechain.search "<query>"
# --------------------------------------------------------------------------


def _main(argv: List[str]) -> int:
    global _DEBUG
    _DEBUG = True
    query = " ".join(argv) or "Elon Musk"
    print(f"query: {query!r}\n")
    total = 0
    for name in ALL_PROVIDERS:
        fn = _PROVIDER_FNS[name]
        t0 = time.monotonic()
        try:
            rows = fn(query)
        except Exception as exc:  # noqa: BLE001
            print(f"--- {name}: FAILED {type(exc).__name__}: {exc}\n")
            continue
        dt = time.monotonic() - t0
        total += len(rows)
        print(f"--- {name}: {len(rows)} candidates in {dt:.1f}s")
        for cand in rows[:5]:
            print(f"    post : {cand.post_url}")
            print(f"    image: {cand.image_url[:120]}")
            print(f"    title: {cand.title[:90]}")
            print()
    print(f"total candidates: {total}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
