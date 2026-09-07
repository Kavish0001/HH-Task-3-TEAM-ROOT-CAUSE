"""Stage 2: genuine web / social-media search for a face.

The pipeline is deliberately provider-agnostic and evidence-driven:

    build_queries()  ->  live providers (reddit / mastodon / linkedin /
                                         ddg-images / wikimedia / x)
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
import re
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
ALL_PROVIDERS: List[str] = [
    "reddit",
    "mastodon",
    "linkedin",
    "ddg_images",
    "wikimedia",
    "x",
]

# Platforms that count as "social media" for the headline result. The task is
# to surface a social-media post, so a social hit wins best_match even when a
# news-site portrait scores marginally higher; the full ranking is preserved in
# SearchReport.matches either way. A LinkedIn profile photo and an X avatar are
# both social-media identity hits, so they are eligible to win best_match too.
SOCIAL_PLATFORMS = {"mastodon", "reddit", "linkedin", "x"}
SOCIAL_PROVIDERS = {"reddit", "mastodon", "linkedin", "x"}

# Fediverse bridges republish other sites into the network. The posts are real
# federated statuses, but their canonical URL points back at the mirrored
# article, so a native permalink is the better headline result when we have one.
BRIDGE_HOSTS = ("fed.brid.gy", "brid.gy", "bird.makeup", "rss-parrot.net")

# How many candidates to keep examining while holding only a bridged social hit,
# hoping for a native permalink, before settling for the bridge.
BRIDGED_PATIENCE = 26


def _is_bridged(post_url: str) -> bool:
    """True if a post URL belongs to a fediverse bridge rather than an instance."""
    return any(host in (post_url or "").lower() for host in BRIDGE_HOSTS)

# Mastodon instances whose public (no-auth) API we poll for tagged posts.
MASTODON_INSTANCES: List[str] = [
    "https://mastodon.social",
    "https://mstdn.social",
    "https://fosstodon.org",
]

# Max candidates taken from one provider for one query.
PER_PROVIDER_CAP = 20

# Notes raised by providers that are not exceptions (e.g. a blocked API).
# Drained into SearchReport.notes by harvest_candidates().
_PROVIDER_NOTES: List[str] = []

# Subreddits that reliably carry people photos; searched in addition to
# the site-wide /search.json endpoint.
REDDIT_SUBREDDITS: List[str] = ["pics", "photographs", "portraits"]

# Reddit blocks its public JSON API from many networks (403 + interstitial).
# When that happens we fall back to a public redlib front-end, which serves the
# same posts as HTML; the permalinks and i.redd.it image URLs it exposes are
# genuine reddit URLs, so the evidence trail stays real.
REDDIT_MIRRORS: List[str] = [
    "https://safereddit.com",
    "https://redlib.catsarch.com",
    "https://redlib.freedit.eu",
]

# --- Identity-search providers (LinkedIn / X) ----------------------------
# Both are found the same way: a DDG *text* search with a site: filter (the
# image endpoint silently ignores site:), then one fetch of each public page to
# read its Open Graph photo. Nothing behind a login is ever touched.
#
# LinkedIn answers 999 to a browser User-Agent but 200 to a link-preview
# crawler, which is exactly what we are: we read the public og:image and
# nothing else. X answers 200 for profiles but strips Open Graph from status
# pages for unauthenticated crawlers, so posts yield no image at all.
CRAWLER_UA = (
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
)

# Public profile pages fetched per query, per provider. Deliberately small: one
# page per candidate identity, never a crawl.
LINKEDIN_PROFILE_CAP = 6
X_PAGE_CAP = 8

# Words a hint query picks up in build_queries(); stripped before deciding
# whether two queries describe the same identity.
_IDENTITY_SUFFIXES = {
    "reddit", "portrait", "photo", "photos", "photograph", "pic", "pics",
    "selfie", "headshot", "profile", "linkedin", "twitter", "x", "instagram",
}
# If any of these survive the strip the query is a generic people-photo probe,
# not a name, and an identity search on it would be noise.
_GENERIC_TOKENS = {
    "portrait", "photo", "photos", "person", "face", "selfie", "headshot",
    "official", "photograph", "people",
}

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


_CRAWLER_SESSION: Optional[requests.Session] = None


def _crawler_session() -> requests.Session:
    """Session identifying us as a link-preview crawler.

    Only used for sites that serve Open Graph tags to crawlers and refuse a
    browser UA (LinkedIn answers HTTP 999 to config.USER_AGENT). This is the
    documented public-preview surface - no cookies, no auth, no login walls.
    """
    global _CRAWLER_SESSION
    if _CRAWLER_SESSION is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": CRAWLER_UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        _CRAWLER_SESSION = s
    return _CRAWLER_SESSION


_OG_KEYS = ("og:image", "og:image:secure_url", "og:title", "og:description")
_OG_CACHE: Dict[str, tuple[int, Dict[str, str]]] = {}
_OG_CACHE_LOCK = threading.Lock()


def _meta_content(page: str, key: str) -> str:
    """Value of a <meta property|name="key" content="..."> tag, either order."""
    esc = re.escape(key)
    patterns = (
        r'<meta[^>]+(?:property|name)\s*=\s*["\']' + esc
        + r'["\'][^>]*?content\s*=\s*["\']([^"\']*)["\']',
        r'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*?(?:property|name)\s*=\s*["\']'
        + esc + r'["\']',
    )
    for pat in patterns:
        m = re.search(pat, page, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1)).strip()
    return ""


def _fetch_og(url: str) -> tuple[int, Dict[str, str]]:
    """Fetch one public page as a crawler and read its Open Graph tags.

    Returns ``(status_code, tags)``; status 0 means the request never landed.
    Cached per URL so the overlapping queries build_queries() produces never
    refetch the same profile, and so a run stays gentle on the host.
    """
    with _OG_CACHE_LOCK:
        hit = _OG_CACHE.get(url)
    if hit is not None:
        return hit

    status = 0
    tags: Dict[str, str] = {}
    try:
        _polite(url)
        resp = _crawler_session().get(url, timeout=config.HTTP_TIMEOUT)
        status = resp.status_code
        if status == 200:
            page = resp.text[:400_000]
            for key in _OG_KEYS:
                value = _meta_content(page, key)
                if value:
                    tags[key] = value
        resp.close()
    except Exception as exc:  # noqa: BLE001 - providers must never crash the run
        _log_debug(f"og fetch failed {url}: {exc}")

    result = (status, tags)
    with _OG_CACHE_LOCK:
        _OG_CACHE[url] = result
    return result


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


_REDDIT_JSON_BLOCKED = False

_POST_SPLIT = re.compile(r'<div class="post" id="')
_RE_PERMALINK = re.compile(r'href="(/r/[A-Za-z0-9_]+/comments/[a-z0-9]+/[^"?#]*)"')
_RE_AUTHOR = re.compile(r'class="post_author[^"]*" href="/u/([A-Za-z0-9_\-]+)"')
_RE_CREATED = re.compile(r'<span class="created" title="([^"]+)"')
_RE_TITLE = re.compile(
    r'<a href="/r/[A-Za-z0-9_]+/comments/[a-z0-9]+/[^"]*">([^<]{2,300})</a>'
)
_RE_PREVIEW = re.compile(r'/(?:preview/pre|img)/([a-z0-9]{6,32}\.(?:jpg|jpeg|png|webp))')
_RE_SUB = re.compile(r'href="/r/([A-Za-z0-9_]+)"')


def _mirror_created_iso(raw: str) -> str:
    """Redlib renders 'Aug 11 2026, 13:08:56 UTC'."""
    try:
        dt = datetime.strptime(raw.replace(" UTC", ""), "%b %d %Y, %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _parse_reddit_mirror(page: str, query: str) -> List[Candidate]:
    """Parse a redlib listing page into candidates with real reddit URLs."""
    out: List[Candidate] = []
    for block in _POST_SPLIT.split(page)[1:]:
        perm = _RE_PERMALINK.search(block)
        img = _RE_PREVIEW.search(block)
        if not perm or not img:
            continue
        title_m = _RE_TITLE.search(block)
        author_m = _RE_AUTHOR.search(block)
        created_m = _RE_CREATED.search(block)
        sub_m = _RE_SUB.search(block)
        out.append(
            Candidate(
                platform="reddit",
                post_url=f"https://www.reddit.com{perm.group(1)}",
                # Redlib proxies preview.redd.it; the same asset id is served
                # unsigned and full-size from i.redd.it.
                image_url=f"https://i.redd.it/{img.group(1)}",
                title=html.unescape(title_m.group(1)).strip() if title_m else "",
                author=author_m.group(1) if author_m else "",
                posted_at=_mirror_created_iso(created_m.group(1)) if created_m else "",
                query=query,
                discovered_via="keyword-search",
                metadata={
                    "subreddit": sub_m.group(1) if sub_m else "",
                    "via": "redlib-mirror",
                },
            )
        )
    return out


def _search_reddit_mirror(query: str) -> List[Candidate]:
    """Fallback path when reddit's own JSON API refuses the request."""
    paths = [("/search", {})] + [
        (f"/r/{sub}/search", {"restrict_sr": "on"}) for sub in REDDIT_SUBREDDITS
    ]
    for base in REDDIT_MIRRORS:
        found: List[Candidate] = []
        alive = False
        for path, extra in paths:
            params: Dict[str, Any] = {"q": query, "type": "link", "sort": "relevance"}
            params.update(extra)
            resp = _get(base + path, params=params, retries=0)
            if resp is None:
                continue
            alive = True
            found.extend(_parse_reddit_mirror(resp.text, query))
        if alive and found:
            return found
    return []


def _search_reddit(query: str, limit: int = 25) -> List[Candidate]:
    """Reddit search: free public JSON API first, redlib mirror as fallback.

    Reddit requires a descriptive User-Agent (config.USER_AGENT); on networks
    where it 403s the JSON API outright we degrade to a public front-end rather
    than dropping the platform.
    """
    global _REDDIT_JSON_BLOCKED
    results: List[Candidate] = []

    if not _REDDIT_JSON_BLOCKED:
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

        reachable = False
        for url, params in endpoints:
            resp = _get(url, params=params, retries=0)
            if resp is None:
                continue
            reachable = True
            try:
                results.extend(_parse_reddit_listing(resp.json(), query))
            except (ValueError, json.JSONDecodeError):
                continue
        if not reachable:
            # Latch the block so later queries skip four dead round-trips.
            _REDDIT_JSON_BLOCKED = True
            _PROVIDER_NOTES.append(
                "reddit: public JSON API returned 403 (unauthenticated access is "
                "blocked); fell back to a public redlib front-end, permalinks and "
                "i.redd.it image URLs are still genuine reddit URLs"
            )
            _log_debug("reddit JSON API blocked; using mirror fallback")

    if not results:
        results = _search_reddit_mirror(query)
    return results


_RE_TAGS = re.compile(r"<[^>]+>")


def _mastodon_tag(query: str) -> str:
    """'Elon Musk' -> 'elonmusk' (Mastodon hashtags are alphanumeric only)."""
    return re.sub(r"[^0-9a-z]+", "", query.lower())


def _mastodon_status_to_candidates(
    status: Dict[str, Any], query: str, instance: str
) -> List[Candidate]:
    """One status can carry several image attachments; keep them all."""
    post_url = status.get("url") or status.get("uri") or ""
    if not post_url:
        return []
    account = status.get("account") or {}
    text = html.unescape(_RE_TAGS.sub(" ", status.get("content") or ""))
    text = re.sub(r"\s+", " ", text).strip()[:200]

    out: List[Candidate] = []
    for media in status.get("media_attachments") or []:
        if media.get("type") != "image":
            continue
        image_url = media.get("url") or media.get("remote_url") or ""
        if not image_url:
            continue
        out.append(
            Candidate(
                platform="mastodon",
                post_url=post_url,
                image_url=image_url,
                title=text,
                author="@" + str(account.get("acct") or ""),
                posted_at=str(status.get("created_at") or ""),
                query=query,
                discovered_via="social-search",
                metadata={
                    "instance": instance,
                    "status_id": status.get("id"),
                    "description": (media.get("description") or "")[:200],
                    "favourites": status.get("favourites_count"),
                },
            )
        )
    return out


def _search_mastodon(query: str, limit: int = 40) -> List[Candidate]:
    """Mastodon's public API: no auth, no key, real social posts with media."""
    tag = _mastodon_tag(query)
    out: List[Candidate] = []
    if not tag:
        return out

    for instance in MASTODON_INSTANCES:
        resp = _get(
            f"{instance}/api/v1/timelines/tag/{tag}",
            params={"limit": limit, "only_media": "true"},
            retries=0,
        )
        if resp is None:
            continue
        try:
            statuses = resp.json()
        except ValueError:
            continue
        if not isinstance(statuses, list):
            continue
        for status in statuses:
            out.extend(_mastodon_status_to_candidates(status, query, instance))

    # Full-text search is token-gated on most instances; try it, ignore failures.
    for instance in MASTODON_INSTANCES[:1]:
        try:
            resp = _get(
                f"{instance}/api/v2/search",
                params={"q": query, "type": "statuses", "limit": 20},
                retries=0,
            )
            if resp is None:
                continue
            for status in (resp.json() or {}).get("statuses") or []:
                out.extend(_mastodon_status_to_candidates(status, query, instance))
        except Exception:  # noqa: BLE001
            continue
    return out


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


# --------------------------------------------------------------------------
# Identity search: LinkedIn and X / Twitter
# --------------------------------------------------------------------------

# Results are memoised per (provider, identity) so the four queries
# build_queries() derives from one hint cost one site: search and one set of
# page fetches instead of four.
_IDENTITY_CACHE: Dict[tuple[str, str], List[Candidate]] = {}

_RE_LINKEDIN_PROFILE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9\-_%.]{2,100})", re.I
)
_RE_X_URL = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/"
    r"([A-Za-z0-9_]{1,15})(/status/\d+)?",
    re.I,
)
# Handles that are site furniture, not people.
_X_RESERVED = {
    "home", "search", "explore", "i", "intent", "share", "hashtag", "settings",
    "login", "signup", "about", "privacy", "tos", "notifications", "messages",
}


def _identity_core(query: str) -> str:
    """'Sundar Pichai reddit' -> 'sundar pichai'; '' if it is not a name."""
    tokens = [t for t in re.split(r"\s+", query.lower().strip()) if t]
    while tokens and tokens[-1].strip(".,\"'") in _IDENTITY_SUFFIXES:
        tokens.pop()
    if not tokens or len(tokens) > 5:
        return ""
    if any(t.strip(".,\"'") in _GENERIC_TOKENS for t in tokens):
        return ""  # a generic people-photo probe, not an identity
    return " ".join(tokens)


def _ddg_text(query: str, limit: int) -> List[Dict[str, Any]]:
    """DDG *text* search. Unlike the image endpoint it honours `site:`."""
    DDGS = _ddgs_class()
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query=query, max_results=limit) or [])
    except Exception as exc:  # noqa: BLE001
        _log_debug(f"ddg text failed for {query!r}: {type(exc).__name__}: {exc}")
        return []


def _ddg_urls(queries: Sequence[str], limit: int) -> List[str]:
    """Run site: text searches until one yields hits; return unique hrefs."""
    urls: List[str] = []
    seen: set[str] = set()
    for q in queries:
        for row in _ddg_text(q, limit):
            href = (row.get("href") or row.get("url") or row.get("link") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            urls.append(href)
        if urls:
            break  # first pass produced something; skip the looser fallback
    return urls


def _og_image(tags: Dict[str, str]) -> str:
    url = tags.get("og:image") or tags.get("og:image:secure_url") or ""
    return url if url.lower().startswith(("http://", "https://")) else ""


def _search_linkedin(query: str, limit: int = 10) -> List[Candidate]:
    """LinkedIn public profiles found via a `site:linkedin.com/in` text search.

    For every profile URL we fetch exactly one public page (as a link-preview
    crawler, because LinkedIn answers 999 to anything else) and read its
    `og:image`, which is the profile photo on media.licdn.com. Nothing behind a
    login is touched and no connection/network page is followed; the profile is
    only ever reported later if its *face embedding* clears the threshold.
    """
    core = _identity_core(query)
    if not core:
        return []
    key = ("linkedin", core)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return list(cached)
    _IDENTITY_CACHE[key] = []  # guard against re-running on a failed pass

    found = _ddg_urls(
        [
            f'site:linkedin.com/in "{core}"',
            f"site:linkedin.com/in {core}",
        ],
        max(limit, 10),
    )

    profiles: List[str] = []
    slugs: Dict[str, str] = {}
    for href in found:
        m = _RE_LINKEDIN_PROFILE.search(href)
        if not m:
            continue
        url = f"https://www.linkedin.com/in/{m.group(1).rstrip('/')}"
        if url not in slugs:
            slugs[url] = m.group(1)
            profiles.append(url)

    if not profiles:
        _PROVIDER_NOTES.append(
            f"linkedin: site:linkedin.com/in search for '{core}' returned no "
            f"public profile URLs ({len(found)} results seen)"
        )
        return []

    out: List[Candidate] = []
    blocked: Dict[int, int] = {}
    fetched = 0
    for url in profiles[:LINKEDIN_PROFILE_CAP]:
        if fetched:
            time.sleep(0.5)  # LinkedIn 429s a fast run of profile fetches
        status, tags = _fetch_og(url)
        fetched += 1
        if status != 200:
            blocked[status] = blocked.get(status, 0) + 1
            continue
        image_url = _og_image(tags)
        if not image_url or "static.licdn.com" in image_url.lower():
            continue  # no photo, or the generic "no picture" ghost avatar
        title = tags.get("og:title", "")
        for tail in (" | LinkedIn", " - LinkedIn", " | Linkedin"):
            if title.endswith(tail):
                title = title[: -len(tail)].strip()
        out.append(
            Candidate(
                platform="linkedin",
                post_url=url,
                image_url=image_url,
                title=title[:300],
                author=slugs.get(url, ""),
                query=query,
                discovered_via="identity-search",
                metadata={
                    "profile_slug": slugs.get(url, ""),
                    "source": "og:image on the public profile page",
                    "og_description": tags.get("og:description", "")[:200],
                },
            )
        )

    if blocked:
        detail = ", ".join(
            f"HTTP {code or 'no response'} x{n}" for code, n in sorted(blocked.items())
        )
        _PROVIDER_NOTES.append(
            f"linkedin: profile fetch blocked ({detail}) for "
            f"{sum(blocked.values())} of {fetched} profiles"
        )
    _PROVIDER_NOTES.append(
        f"linkedin: {len(profiles)} public profile URLs found for '{core}', "
        f"{fetched} fetched (cap {LINKEDIN_PROFILE_CAP}/query), "
        f"{len(out)} exposed an og:image profile photo"
    )
    _IDENTITY_CACHE[key] = list(out)
    return out


def _upgrade_x_avatar(url: str) -> str:
    """pbs.twimg.com serves several sizes; ask for the largest square."""
    for small in ("_200x200", "_normal", "_bigger", "_mini", "_reasonably_small"):
        if small in url:
            return url.replace(small, "_400x400")
    return url


def _search_x(query: str, limit: int = 10) -> List[Candidate]:
    """X / Twitter identity search - honest about how little it can return.

    A `site:x.com` text search does find real profile and status URLs, but X
    strips Open Graph from status pages for unauthenticated crawlers, so a post
    exposes no image at all; only profile pages carry an `og:image`, and that
    avatar is frequently not a photograph of a face. A candidate is emitted only
    when an image was actually obtained - a candidate with no image could never
    be face-verified and must never surface as a result.
    """
    core = _identity_core(query)
    if not core:
        return []
    key = ("x", core)
    cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return list(cached)
    _IDENTITY_CACHE[key] = []

    found = _ddg_urls([f'site:x.com "{core}"'], max(limit, 10))
    found += _ddg_urls([f'site:twitter.com "{core}"'], max(limit, 10))

    pages: List[tuple[str, str, bool]] = []  # (url, handle, is_status)
    seen: set[str] = set()
    for href in found:
        m = _RE_X_URL.search(href)
        if not m:
            continue
        handle = m.group(1)
        if handle.lower() in _X_RESERVED:
            continue
        is_status = bool(m.group(2))
        url = f"https://x.com/{handle}{m.group(2) or ''}"
        if url in seen:
            continue
        seen.add(url)
        pages.append((url, handle, is_status))

    # Profiles first: they carry a real avatar; status pages only ever expose a
    # generated card, when they expose anything at all.
    pages.sort(key=lambda row: row[2])

    if not pages:
        _PROVIDER_NOTES.append(
            f"x: site:x.com / site:twitter.com search for '{core}' returned no "
            f"usable URLs ({len(found)} results seen; the text endpoint also "
            f"rate-limits after repeated queries)"
        )
        return []

    out: List[Candidate] = []
    with_og = 0
    placeholders = 0
    fetched = 0
    for url, handle, is_status in pages[:X_PAGE_CAP]:
        status, tags = _fetch_og(url)
        fetched += 1
        if status != 200:
            continue
        image_url = _og_image(tags)
        if not image_url:
            continue
        with_og += 1
        if "abs.twimg.com" in image_url.lower():
            # X's generic "no preview" placeholder, not a photograph.
            placeholders += 1
            continue
        out.append(
            Candidate(
                platform="x",
                post_url=url,
                image_url=_upgrade_x_avatar(image_url),
                title=(tags.get("og:title", ""))[:300],
                author=f"@{handle}",
                query=query,
                discovered_via="identity-search",
                metadata={
                    "handle": handle,
                    "page_type": "status" if is_status else "profile",
                    "source": "og:image",
                },
            )
        )

    statuses = sum(1 for _, _, is_status in pages if is_status)
    _PROVIDER_NOTES.append(
        f"x: {len(pages)} URLs found for '{core}' "
        f"({len(pages) - statuses} profiles, {statuses} status pages), "
        f"{fetched} fetched, {with_og} exposed an og:image "
        f"({placeholders} of those were X's generic placeholder, "
        f"{len(out)} kept) - X strips real Open Graph media from status pages for "
        f"unauthenticated crawlers, so only profile avatars and generated post "
        f"cards are reachable and those frequently contain no detectable face"
    )
    _IDENTITY_CACHE[key] = list(out)
    return out


_PROVIDER_FNS: Dict[str, Callable[[str], List[Candidate]]] = {
    "reddit": _search_reddit,
    "mastodon": _search_mastodon,
    "linkedin": _search_linkedin,
    "ddg_images": _search_ddg_images,
    "wikimedia": _search_wikimedia,
    "x": _search_x,
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
            # Cap per provider per query so one chatty provider cannot crowd
            # the others out of the candidate pool.
            for cand in rows[:PER_PROVIDER_CAP]:
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
                notes.extend(_drain_provider_notes())
                return out[:limit], working, notes
    notes.extend(_drain_provider_notes())
    return out, working, notes


def _interleave_by_platform(candidates: Sequence[Candidate]) -> List[Candidate]:
    """Round-robin candidates across platforms.

    Without this the first provider in the list monopolises the examination
    budget and the early-stop rule never reaches the other platforms.
    """
    buckets: Dict[str, List[Candidate]] = {}
    for cand in candidates:
        buckets.setdefault(cand.platform, []).append(cand)
    out: List[Candidate] = []
    while buckets:
        for key in list(buckets):
            out.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return out


def _drain_provider_notes() -> List[str]:
    """Take non-fatal provider notes (blocked APIs, fallbacks) for the report."""
    drained, _PROVIDER_NOTES[:] = list(dict.fromkeys(_PROVIDER_NOTES)), []
    return drained


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
    candidates = _interleave_by_platform(candidates)
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
    have_social_match = False
    have_native_social = False
    # If no social provider ran there is nothing to hold out for.
    social_in_play = bool(SOCIAL_PROVIDERS & set(working))

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
                    if match.platform in SOCIAL_PLATFORMS:
                        have_social_match = True
                        if not _is_bridged(match.post_url):
                            have_native_social = True

            # Fast demo, but only after real work: >= 12 candidates examined and
            # a match in hand. Never stop on a non-social match while a social
            # provider is still in play - the deliverable is a social-media post.
            # A bridged social hit counts, but is worth spending a few more
            # candidates on in case a native permalink turns up.
            enough = have_native_social or (have_match and not social_in_play)
            if not enough and have_social_match and examined >= BRIDGED_PATIENCE:
                enough = True  # settle for the bridge rather than search forever
            if enough and examined >= 12:
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
    # best_match prefers a social-media post; `matches` keeps the full ranking.
    social = [m for m in report.matches if m.platform in SOCIAL_PLATFORMS]
    # Among social hits, prefer a native permalink over a bridge. A brid.gy URL
    # is a genuine federated post, but it resolves to the news article it
    # mirrors, so it reads as a news link rather than as a social one.
    native = [m for m in social if not _is_bridged(m.post_url)]
    if native and native[0] is not social[0]:
        report.notes.append(
            f"best match: preferred the native social permalink "
            f"({native[0].platform}, {native[0].similarity:.4f}) over a "
            f"bridged post scoring {social[0].similarity:.4f}"
        )
        social = native + [m for m in social if m not in native]
    if social:
        report.best_match = social[0]
        top = report.matches[0]
        note = (
            f"best match: highest-scoring social-media post "
            f"({report.best_match.platform}, {report.best_match.similarity:.4f})"
        )
        if top is not report.best_match:
            note += (
                f"; a higher-scoring non-social candidate was available at "
                f"{top.similarity:.4f} ({top.platform})"
            )
        report.notes.append(note)
    elif report.matches:
        report.best_match = report.matches[0]
        report.notes.append(
            f"best match: no social-media post cleared the threshold; using the "
            f"highest-scoring candidate overall ({report.best_match.platform}, "
            f"{report.best_match.similarity:.4f})"
        )
    else:
        report.best_match = None
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
