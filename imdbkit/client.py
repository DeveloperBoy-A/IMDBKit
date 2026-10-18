import html as html_lib
import json
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

from .models import Episode, Movie, SearchResult, Season, SeriesInfo, Title, unique_names
from .textutils import (
    build_primary_queries,
    double_letter_variants,
    current_year,
    normalize_text,
    parse_media_query,
    phonetic_key,
    prefix_variants,
    strip_article,
    word_variants,
)
from .vocab import SEED_TITLES, TitleVocabulary

logger = logging.getLogger(__name__)

# Only these kinds are ever returned by search (no games, podcasts, shorts,
# music videos, single episodes, specials ...).
ALLOWED_KINDS = {"Movie", "Web Series", "Mini Series", "TV Movie"}
SERIES_KINDS = {"Web Series", "Mini Series"}

GOOD_MATCH = 88.0        # base similarity above which no spell-rescue is attempted
UPCOMING_STATUSES = {"planned", "in production", "post production", "rumored"}


# ============================================================
# Smart Caching & Concurrency Protection Helpers
# ============================================================

class TTLCache:
    """Thread-safe TTL Cache with Negative/Error Caching support."""

    def __init__(self, ttl: int = 86400, negative_ttl: int = 300, maxsize: int = 3000):
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.maxsize = maxsize
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Tuple[bool, Any]:
        with self._lock:
            if key not in self._cache:
                return False, None
            timestamp, value = self._cache[key]

            is_empty = value is None or (isinstance(value, (list, dict, str)) and not value)
            current_ttl = self.negative_ttl if is_empty else self.ttl

            if time.time() - timestamp > current_ttl:
                del self._cache[key]
                return False, None
            return True, value

    def set(self, key: str, value: Any):
        with self._lock:
            if key not in self._cache and len(self._cache) >= self.maxsize:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            self._cache[key] = (time.time(), value)


class RequestCoalescer:
    """
    Dogpile protection: concurrent callers asking for the same key share ONE fetch.

    (The previous implementation cleared the stored result before waking the
    waiters, so every waiting thread received None.  A Future fixes that.)
    """

    def __init__(self):
        self._in_flight: Dict[str, Future] = {}
        self._lock = threading.Lock()

    def execute(self, key: str, fetch_func, *args, **kwargs):
        with self._lock:
            future = self._in_flight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._in_flight[key] = future

        if not owner:
            try:
                return future.result(timeout=90)
            except Exception:
                return None

        try:
            result = fetch_func(*args, **kwargs)
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._in_flight.pop(key, None)


class IMDBKit:
    """
    IMDb + TMDB metadata client.

    * Spell-tolerant search (typos, Roman-Hindi spellings, Devanagari input)
    * Released *and* upcoming titles, movies and series only (no junk kinds)
    * Poster + backdrop images, plot, runtime, cast/crew, certificates, trailer
    * Series: seasons, episodes (S01E01 codes), per-episode details
    * Thread-safe, cached, request-coalescing
    """

    IMDb_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/"
    IMDb_TITLE_URL = "https://www.imdb.com/title/"
    TMDB_BASE_URL = "https://api.themoviedb.org/3"
    WIKI_API_URL = "https://en.wikipedia.org/w/api.php"
    TVMAZE_URL = "https://api.tvmaze.com"
    TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/"

    # NOTE: w1280 is a *backdrop* size.  TMDB poster sizes are
    # w92 w154 w185 w342 w500 w780 original  (the old code used w1280 for
    # posters, which does not exist and returns 404).
    POSTER_SIZE = "w780"
    BACKDROP_SIZE = "w1280"
    STILL_SIZE = "w780"
    PROFILE_SIZE = "w185"
    LOGO_SIZE = "w500"

    # Global Shared Caches across instances
    _imdb_cache = TTLCache(ttl=86400, negative_ttl=60, maxsize=4000)
    _imdb_id_cache = TTLCache(ttl=172800, negative_ttl=120, maxsize=3000)
    _tmdb_cache = TTLCache(ttl=172800, negative_ttl=120, maxsize=4000)
    _page_cache = TTLCache(ttl=21600, negative_ttl=300, maxsize=1500)
    _coalescer = RequestCoalescer()
    _vocab = TitleVocabulary(SEED_TITLES)

    def __init__(
        self,
        tmdb_api_key: Optional[str] = None,
        timeout: int = 15,
        language: str = "en-US",
        ai_corrector: Optional[Callable[[str], Any]] = None,
        imdb_page_fallback: bool = True,
        max_workers: int = 6,
        region: str = "IN",
        use_wikipedia: bool = True,
        use_tvmaze: bool = True,
    ):
        """
        tmdb_api_key       TMDB v3 key *or* v4 read-access token (also read from
                           IMDBKIT_TMDB_API_KEY / TMDB_API_KEY).
        language           TMDB language for plot/titles, e.g. "en-US", "hi-IN".
        ai_corrector       Optional callable(query) -> str | list[str].  Plug any LLM
                           here; it is only called when local spell-correction
                           could not find a good match.
        region             Country code for "streaming_on" (Netflix, JioHotstar, MX Player ...).
        use_wikipedia      Spell rescue through Wikipedia's search suggestions.  Knows
                           Hollywood, Bollywood, Korean and OTT-original titles even when
                           the typo is too big for IMDb's prefix search.
        use_tvmaze         Extra series source (Netflix / Prime / Hotstar / MX Player shows).
        imdb_page_fallback Read rating / plot / runtime from the public IMDb title
                           page (JSON-LD) - gives real IMDb rating and works even
                           without a TMDB key.
        """
        self.tmdb_api_key = (
            tmdb_api_key
            or os.getenv("IMDBKIT_TMDB_API_KEY")
            or os.getenv("TMDB_API_KEY")
        )
        if self.tmdb_api_key:
            self.tmdb_api_key = self.tmdb_api_key.strip()

        self.timeout = timeout
        self.language = language or "en-US"
        self.ai_corrector = ai_corrector
        self.imdb_page_fallback = imdb_page_fallback
        self.max_workers = max(1, int(max_workers))
        self.region = (region or "IN").upper()
        self.use_wikipedia = use_wikipedia
        self.use_tvmaze = use_tvmaze

        # v4 "API Read Access Token" is a JWT, v3 key is 32 hex chars.
        self._tmdb_bearer = bool(
            self.tmdb_api_key
            and (self.tmdb_api_key.startswith("eyJ") or len(self.tmdb_api_key) > 60)
        )

        if self.tmdb_api_key:
            logger.info("IMDBKit: TMDB API key loaded successfully.")
        else:
            logger.warning(
                "IMDBKit: TMDB API key not configured. Using IMDb fallback only "
                "(no backdrops, no season/episode data)."
            )

    # ============================================================
    # HTTP
    # ============================================================

    def _http_get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retries: int = 1,
    ) -> Optional[str]:
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url = f"{url}{'&' if '?' in url else '?'}{query}"

        request_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
        request_headers.update(headers or {})

        safe_url = url.split("?")[0]

        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")

            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                    try:
                        delay = float(exc.headers.get("Retry-After", 1))
                    except Exception:
                        delay = 1.0
                    time.sleep(min(max(delay, 0.3), 3.0))
                    continue
                logger.error("IMDBKit HTTP error %s for %s", exc.code, safe_url)
                return None

            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                if attempt < retries:
                    time.sleep(0.4)
                    continue
                logger.error(
                    "IMDBKit network/timeout error for %s: %s",
                    safe_url,
                    getattr(exc, "reason", exc),
                )
                return None

            except Exception as exc:
                logger.exception("IMDBKit unexpected request error for %s: %s", safe_url, exc)
                return None

        return None

    def _request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        body = self._http_get(url, params, headers)
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error("IMDBKit invalid JSON from %s: %s", url.split("?")[0], exc)
            return None

    def _cached_request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        cache_tier: str = "imdb",
    ) -> Optional[Dict[str, Any]]:
        cache_key = url
        if params:
            cache_key += "?" + urllib.parse.urlencode(sorted(params.items()))

        target_cache = (
            self._imdb_cache if cache_tier == "imdb"
            else self._imdb_id_cache if cache_tier == "imdb_id"
            else self._tmdb_cache
        )

        found, cached_val = target_cache.get(cache_key)
        if found:
            return cached_val

        def fetch():
            res = self._request_json(url, params)
            target_cache.set(cache_key, res)
            return res

        return self._coalescer.execute(cache_key, fetch)

    def _tmdb_get(self, path: str, **params) -> Optional[Dict[str, Any]]:
        """Cached + coalesced TMDB GET.  The API key is never part of cache keys/logs."""
        if not self.tmdb_api_key:
            return None

        params = {k: v for k, v in params.items() if v is not None}
        cache_key = "tmdb:" + path + "?" + urllib.parse.urlencode(sorted(params.items()))

        found, value = self._tmdb_cache.get(cache_key)
        if found:
            return value

        def fetch():
            query = dict(params)
            headers = None
            if self._tmdb_bearer:
                headers = {"Authorization": f"Bearer {self.tmdb_api_key}"}
            else:
                query["api_key"] = self.tmdb_api_key
            res = self._request_json(f"{self.TMDB_BASE_URL}{path}", query, headers)
            self._tmdb_cache.set(cache_key, res)
            return res

        return self._coalescer.execute(cache_key, fetch)

    @staticmethod
    def _pmap(func: Callable, items: Iterable, max_workers: int = 6) -> List[Any]:
        items = list(items)
        if not items:
            return []
        if len(items) == 1:
            return [func(items[0])]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
            return list(pool.map(func, items))

    # ============================================================
    # Generic helpers
    # ============================================================

    @staticmethod
    def _devanagari_to_roman(text: str) -> str:
        from .textutils import devanagari_to_roman
        return devanagari_to_roman(text)

    @staticmethod
    def _normalize_imdb_id(imdb_id: Any) -> Optional[str]:
        if imdb_id is None:
            return None

        value = str(imdb_id).strip()

        if not value:
            return None

        if value.startswith("tt"):
            return value

        if value.isdigit():
            return f"tt{value}"

        match = re.search(r"(tt\d+)", value)

        if match:
            return match.group(1)

        return value

    @staticmethod
    def _clean_text(value: Any) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            return value or None

        return str(value).strip() or None

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _year_from_date(value: Any) -> Optional[int]:
        if not value:
            return None
        match = re.search(r"\b((?:18|19|20)\d{2})\b", str(value))
        return int(match.group(1)) if match else None

    @staticmethod
    def _normalize_search_text(value: Any) -> str:
        return normalize_text(value)

    @staticmethod
    def _tokenize_search_text(value: Any) -> List[str]:
        return normalize_text(value).split()

    @staticmethod
    def _extract_year_from_query(value: Any) -> Optional[int]:
        return parse_media_query(value).get("year")

    @staticmethod
    def clean_media_title(value: Any) -> str:
        """'Pushpa.2021.1080p.WEB-DL.x264' -> 'Pushpa'  (also Devanagari input)."""
        return parse_media_query(value).get("title") or ""

    @staticmethod
    def parse_media_structure(value: Any) -> Dict[str, Any]:
        """title / year / season / episode / is_series parsed from a query or file name."""
        parsed = parse_media_query(value)
        return {
            "title": parsed["title"],
            "title_punct": parsed.get("title_punct") or parsed["title"],
            "year": parsed["year"],
            "season": parsed["season"],
            "episode": parsed["episode"],
            "is_series": parsed["is_series"],
        }

    @staticmethod
    def _normalize_kind(kind: Any) -> Optional[str]:
        if not kind:
            return None

        value = re.sub(r"[\s_\-]+", "", str(kind).lower())

        mapping = {
            "movie": "Movie",
            "feature": "Movie",
            "featurefilm": "Movie",
            "film": "Movie",
            "tvseries": "Web Series",
            "series": "Web Series",
            "webseries": "Web Series",
            "tvminiseries": "Mini Series",
            "tvlimitedseries": "Mini Series",
            "miniseries": "Mini Series",
            "tvmovie": "TV Movie",
            "tvspecial": "TV Special",
            "short": "Short",
            "tvshort": "TV Short",
            "video": "Video",
            "videogame": "Games",
            "games": "Games",
            "game": "Games",
            "podcast": "Podcasts",
            "podcasts": "Podcasts",
            "podcastseries": "Podcasts",
            "podcastepisode": "Podcast Episode",
            "tvepisode": "Episode",
            "episode": "Episode",
            "musicvideo": "Music Video",
        }

        return mapping.get(value, str(kind).strip().title())

    @staticmethod
    def _filter_unwanted_kinds(kind: Optional[str]) -> bool:
        """True when the kind is a real movie / series."""
        return kind in ALLOWED_KINDS

    @staticmethod
    def imdb_image(url: Optional[str], width: Optional[int] = None) -> Optional[str]:
        """Resize an m.media-amazon.com IMDb image to the given width."""
        if not url or not width or "media-amazon.com" not in url:
            return url
        return re.sub(
            r"\._V1_.*?(\.(?:jpg|jpeg|png|webp))$",
            lambda m: f"._V1_UX{int(width)}_{m.group(1)}",
            url,
        )

    def _tmdb_img(self, path: Optional[str], size: str) -> Optional[str]:
        if not path:
            return None
        return f"{self.TMDB_IMAGE_URL}{size}{path}"

    @staticmethod
    def _dedupe(values: Iterable[Any]) -> List[Any]:
        seen = set()
        out = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    # ============================================================
    # Scoring
    # ============================================================

    @staticmethod
    def _base_similarity(query: str, candidate: str) -> float:
        """
        0-100 similarity between two *normalised* titles.

        Exact (ignoring leading article / spaces) is always 100, so "dune" ranks
        "Dune" above "Dune: Part Two".  Longer titles that merely contain the query
        score lower the longer they are.
        """
        qa, ca = strip_article(query), strip_article(candidate)
        qc, cc = qa.replace(" ", ""), ca.replace(" ", "")
        if not qc or not cc:
            return 0.0
        if qc == cc:
            return 100.0

        pq, pc = phonetic_key(qa), phonetic_key(ca)
        if pq and pq == pc:
            return 98.0

        scores = [
            fuzz.ratio(qa, ca),
            fuzz.ratio(qc, cc),
            0.97 * fuzz.ratio(pq, pc),
            0.95 * fuzz.token_sort_ratio(qa, ca),
        ]

        q_tokens, c_tokens = set(qa.split()), set(ca.split())
        if q_tokens and q_tokens < c_tokens:
            scores.append(68 + 28 * len(qc) / len(cc))

        if len(qc) >= 3 and cc.startswith(qc):
            scores.append(70 + 28 * len(qc) / len(cc))
        elif len(qc) >= 4 and len(qc) < len(cc):
            scores.append(0.88 * fuzz.ratio(qc, cc[: len(qc) + 1]))

        if len(pq) >= 3 and len(pq) < len(pc) and pc.startswith(pq):
            scores.append(66 + 28 * len(pq) / len(pc))

        return float(max(scores))

    @staticmethod
    def _spelling_similarity(qa: str, ca: str) -> float:
        """Pure spelling similarity (no prefix / containment bonus) of two normalised titles."""
        qc, cc = qa.replace(" ", ""), ca.replace(" ", "")
        if not qc or not cc:
            return 0.0
        return float(max(
            fuzz.ratio(qa, ca),
            fuzz.ratio(qc, cc),
            0.97 * fuzz.ratio(phonetic_key(qa), phonetic_key(ca)),
        ))

    def _classify_match(self, parsed: Dict[str, Any], item: Title) -> str:
        """
        exact | phonetic | close | partial | weak   (see Title.match_type)

        Only the user's OWN text is used here - corrections coming from the vocabulary,
        Wikipedia or an AI never make a title "exact", so a wrong correction can never
        be auto-applied.

        A title is checked under EVERY name it is known by (IMDb spelling, TMDB spelling,
        original title ...), so "nayi naveli" and "nayyi navelli" both find the same film.
        """
        order = {"weak": 0, "partial": 1, "close": 2, "phonetic": 3, "exact": 4}
        best = "weak"
        for name in item.names or [item.title]:
            kind = self._classify_name(parsed, name)
            if order[kind] > order[best]:
                best = kind
        return best

    def _similarity(self, parsed: Dict[str, Any], item: Title) -> float:
        """Best 0-100 spelling similarity of the user's text to ANY name of the title.

        Same pronunciation counts as 98, the same text as 100.  A different sequel number
        gives 0 ("tiger 3" must never auto-correct to "tiger 2").
        """
        queries = [
            strip_article(normalize_text(parsed.get(key)))
            for key in ("title", "title_loose", "title_with_year")
        ] + [strip_article(normalize_text(a)) for a in parsed.get("alt_titles", [])]
        queries = [q for q in queries if q]
        best = 0.0
        for name in item.names or [item.title]:
            candidate = strip_article(normalize_text(name))
            cc = candidate.replace(" ", "")
            if not cc:
                continue
            c_nums = set(re.findall(r"\d+", candidate))
            for qa in queries:
                qc = qa.replace(" ", "")
                if not qc or set(re.findall(r"\d+", qa)) != c_nums:
                    continue
                if qc == cc:
                    return 100.0
                if len(qc) >= 4 and phonetic_key(qa) == phonetic_key(candidate):
                    best = max(best, 98.0)
                else:
                    best = max(best, self._spelling_similarity(qa, candidate))
        return round(best, 1)

    def _classify_name(self, parsed: Dict[str, Any], name: str) -> str:
        candidate = strip_article(normalize_text(name))
        cc = candidate.replace(" ", "")
        if not cc:
            return "weak"

        queries = [
            normalize_text(parsed.get(key))
            for key in ("title", "title_loose", "title_with_year")
        ] + [normalize_text(alt) for alt in parsed.get("alt_titles", [])]
        queries = [strip_article(q) for q in queries if q]

        c_nums = set(re.findall(r"\d+", candidate))
        best = "weak"
        order = {"weak": 0, "partial": 1, "close": 2, "phonetic": 3, "exact": 4}

        for qa in queries:
            qc = qa.replace(" ", "")
            if not qc:
                continue
            q_nums = set(re.findall(r"\d+", qa))
            kind = "weak"
            if qc == cc:
                kind = "exact"
            elif q_nums != c_nums:
                # "tiger 3" is not "tiger 2" / "tiger", whatever the spelling similarity
                kind = "partial" if self._base_similarity(qa, candidate) >= 58 else "weak"
            elif len(qc) >= 4 and phonetic_key(qa) == phonetic_key(candidate):
                kind = "phonetic"
            else:
                spell = self._spelling_similarity(qa, candidate)
                same_words = abs(len(qa.split()) - len(candidate.split())) <= 1
                if spell >= 88 and same_words:
                    kind = "close"
                elif self._base_similarity(qa, candidate) >= 58:
                    kind = "partial"
            if order[kind] > order[best]:
                best = kind
        return best

    @staticmethod
    def _year_match(year: Optional[int], item: Title) -> Optional[str]:
        if not year or not item.year:
            return None
        last = item.end_year or item.year
        if year == item.year:
            return "exact"
        if item.year < year <= last:
            return "within"
        if abs(year - item.year) == 1 or year - last == 1:
            return "near"
        return "off"

    def _score_title(
        self,
        parsed: Dict[str, Any],
        extra_queries: List[str],
        item: Title,
    ) -> Tuple[float, float]:
        """Returns (final_score, base_similarity)."""
        candidates = [normalize_text(n) for n in (item.names or [item.title])]
        candidates = [c for c in candidates if c]
        if not candidates:
            return 0.0, 0.0
        # the name that matches the query best drives the score
        candidate = max(
            candidates,
            key=lambda c: max(
                [self._base_similarity(normalize_text(parsed.get(k)), c)
                 for k in ("title", "title_loose", "title_with_year")
                 if normalize_text(parsed.get(k))] or [0.0]
            ),
        )

        main_queries = [
            normalize_text(parsed.get(key))
            for key in ("title", "title_loose", "title_with_year")
        ] + [normalize_text(alt) for alt in parsed.get("alt_titles", [])]
        main_queries = [q for q in main_queries if q]

        base = max(
            [self._base_similarity(q, c) for q in main_queries for c in candidates] or [0.0]
        )
        for corrected in extra_queries:
            # A correction (vocabulary / AI) is slightly less trustworthy than the
            # user's own text.
            base = max(base, 0.94 * max(self._base_similarity(corrected, c) for c in candidates))

        score = base

        # ---- numbers (sequel numbers) ----
        year = parsed.get("year")
        main = main_queries[0] if main_queries else ""
        q_nums = set(re.findall(r"\d+", main))
        c_nums = set(re.findall(r"\d+", candidate))
        if q_nums:
            score += 8.0 if q_nums <= c_nums else -25.0
        elif c_nums and not (year and str(year) in c_nums):
            score -= 4.0   # query asked for the original, not "... 2"

        # ---- year ----
        if year and item.year:
            last = item.end_year or item.year
            if str(year) in c_nums:
                pass
            elif item.year <= year <= last:
                score += 24.0 if year == item.year else 18.0
            elif abs(year - item.year) == 1 or (year - last) == 1:
                score += 8.0
            else:
                score -= 18.0
        elif year:
            score -= 5.0

        # ---- movie / series hint ----
        hint = parsed.get("kind_hint")
        if hint == "series":
            score += 14.0 if item.kind in SERIES_KINDS else -14.0
        elif hint == "movie":
            score += 8.0 if item.kind in ("Movie", "TV Movie") else -8.0
        if item.kind == "TV Movie":
            score -= 3.0

        # ---- popularity (IMDb rank: lower = more popular, or TMDB popularity) ----
        popularity = 0.0
        if item.rank and item.rank > 0:
            popularity = max(0.0, 1.0 - math.log10(item.rank) / 6.0)
        elif item.popularity:
            popularity = min(math.log10(item.popularity + 1.0) / 3.0, 1.0)
        score += 8.0 * popularity

        # ---- data quality ----
        if not item.image_url:
            score -= 3.0
        if not item.year:
            score -= 6.0

        return max(score, 0.0), base

    def _rank(
        self,
        parsed: Dict[str, Any],
        titles: Iterable[Title],
        kind_filter: Optional[set],
        include_upcoming: bool,
        extra_queries: Optional[List[str]] = None,
        relaxed: bool = False,
    ) -> List[Tuple[float, float, Title]]:
        extra_queries = extra_queries or []
        scored: List[Tuple[float, float, Title]] = []
        year = parsed.get("year")

        for item in titles:
            if not self._filter_unwanted_kinds(item.kind):
                continue
            if kind_filter and item.kind not in kind_filter:
                continue
            if not include_upcoming and item.is_upcoming:
                continue
            score, base = self._score_title(parsed, extra_queries, item)
            item.match_type = self._classify_match(parsed, item)
            item.similarity = self._similarity(parsed, item)
            item.year_match = self._year_match(year, item)
            scored.append((score, base, item))

        if not scored:
            return []

        # TITLE FIRST, YEAR SECOND: a title that really is what the user typed always
        # stays above fuzzy look-alikes, even if the typed year is wrong / missing.
        tier = {"exact": 4, "phonetic": 3, "close": 2, "partial": 1, "weak": 0}
        scored.sort(
            key=lambda row: (tier[row[2].match_type], row[0], row[2].year or 0),
            reverse=True,
        )

        if relaxed:
            # Nothing matched well: still give the user the closest titles to pick from.
            return [row for row in scored if row[1] >= 30.0][:8]

        top_score, top_base = scored[0][0], scored[0][1]
        min_base = 70.0 if top_base >= 95 else 58.0

        kept = [
            row for row in scored
            if (row[1] >= min_base or row[2].match_type in ("exact", "phonetic", "close"))
            and row[0] >= top_score - 40.0 - (30.0 if row[2].match_type == "exact" else 0.0)
        ]

        # ---- franchise / sibling titles ----
        # "Dhurandhar" and "Dhurandhar: The Revenge" should both be offered, whichever
        # one the user typed.  Titles that start with the best hit (or that the best
        # hit starts with) are kept right behind it.
        if top_base >= 80:
            anchor = strip_article(normalize_text(scored[0][2].title)).replace(" ", "")
            kept_ids = {id(row[2]) for row in kept}
            if len(anchor) >= 4:
                for score, base, item in scored[1:]:
                    if id(item) in kept_ids:
                        continue
                    form = strip_article(normalize_text(item.title)).replace(" ", "")
                    related = form.startswith(anchor) or (len(form) >= 5 and anchor.startswith(form))
                    if related and item.kind in ALLOWED_KINDS:
                        if item.match_type == "weak":
                            item.match_type = "partial"
                        kept.append((max(score, top_score - 12.0), base, item))
                kept.sort(
                    key=lambda row: (tier[row[2].match_type], row[0], row[2].year or 0),
                    reverse=True,
                )

        for row in kept:
            if row[2].match_type == "weak":
                row[2].match_type = "partial"
        return kept

    # ============================================================
    # IMDb suggestion API
    # ============================================================

    def _imdb_suggest_raw(self, query: str, cache_tier: str = "imdb") -> List[Dict[str, Any]]:
        query = (query or "").strip().lower()
        if not query:
            return []

        first = next((c for c in query if c.isalnum()), "x")
        if not ("a" <= first <= "z" or first.isdigit()):
            first = "x"

        url = (
            f"{self.IMDb_SUGGESTION_URL}{first}/"
            f"{urllib.parse.quote(query, safe='')}.json"
        )
        data = self._cached_request_json(url, cache_tier=cache_tier)
        items = data.get("d", []) if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _parse_suggestion(self, item: Dict[str, Any]) -> Optional[Title]:
        imdb_id = self._normalize_imdb_id(item.get("id"))
        if not imdb_id or not imdb_id.startswith("tt"):
            return None   # people (nm...), companies, keywords ...

        name = self._clean_text(item.get("l") or item.get("title"))
        if not name:
            return None

        year = self._safe_int(item.get("y") or item.get("year"))
        end_year = None
        span = re.match(r"\s*(\d{4})(?:\s*[-–]\s*(\d{4})?)?", str(item.get("yr") or ""))
        if span:
            year = year or int(span.group(1))
            end_year = int(span.group(2)) if span.group(2) else None

        image = item.get("i")
        image_url = image.get("imageUrl") if isinstance(image, dict) else None

        poster = self.imdb_image(image_url, 500)
        return Title(
            imdb_id=imdb_id,
            title=name,
            imdb_title=name,
            imdb_poster_url=poster,
            sources=["imdb"],
            year=year,
            kind=self._normalize_kind(item.get("qid") or item.get("q") or item.get("kind")),
            image_url=poster,
            end_year=end_year,
            cast_text=self._clean_text(item.get("s")),
            rank=self._safe_int(item.get("rank")),
        )

    def _imdb_suggest_titles(self, query: str) -> List[Title]:
        """Movies/series only (junk kinds are dropped)."""
        try:
            titles = []
            for item in self._imdb_suggest_raw(query):
                title = self._parse_suggestion(item)
                if title and self._filter_unwanted_kinds(title.kind):
                    titles.append(title)
            return titles
        except Exception as exc:
            logger.exception("IMDBKit suggestion lookup failed for %r: %s", query, exc)
            return []

    def _find_imdb_title(self, imdb_id: str) -> Optional[Title]:
        imdb_id = self._normalize_imdb_id(imdb_id)
        if not imdb_id:
            return None

        for item in self._imdb_suggest_raw(imdb_id, cache_tier="imdb_id"):
            if self._normalize_imdb_id(item.get("id")) == imdb_id:
                return self._parse_suggestion(item)
        return None

    # ============================================================
    # TMDB search (adds upcoming titles + better coverage)
    # ============================================================

    def _tmdb_external_imdb_id(self, media_type: str, tmdb_id: Any) -> Optional[str]:
        data = self._tmdb_get(f"/{media_type}/{tmdb_id}/external_ids")
        value = data.get("imdb_id") if isinstance(data, dict) else None
        return self._normalize_imdb_id(value) if value else None

    def _tmdb_search_titles(self, parsed: Dict[str, Any], queries: List[str]) -> List[Title]:
        if not self.tmdb_api_key:
            return []

        try:
            year = parsed.get("year")
            found: Dict[Tuple[str, int], Dict[str, Any]] = {}

            for query in [q for q in queries if q]:
                data = self._tmdb_get(
                    "/search/multi", query=query, include_adult="false",
                    language=self.language, page=1,
                )
                for row in (data or {}).get("results", []):
                    if row.get("media_type") in ("movie", "tv") and row.get("id"):
                        found[(row["media_type"], row["id"])] = row

                if year:
                    for media_type, path, year_param in (
                        ("movie", "/search/movie", "year"),
                        ("tv", "/search/tv", "first_air_date_year"),
                    ):
                        data = self._tmdb_get(
                            path, query=query, include_adult="false",
                            language=self.language, **{year_param: year},
                        )
                        for row in (data or {}).get("results", []):
                            if row.get("id"):
                                row = dict(row, media_type=media_type)
                                found.setdefault((media_type, row["id"]), row)

            qnorms = [
                normalize_text(parsed.get(k)) for k in ("title", "title_loose", "title_with_year")
            ] + [normalize_text(q) for q in queries]
            qnorms = [q for q in qnorms if q]

            drafts = []
            for (media_type, tmdb_id), row in found.items():
                name = row.get("title") or row.get("name")
                if not name:
                    continue
                names = [normalize_text(name)]
                original = row.get("original_title") or row.get("original_name")
                if original:
                    names.append(normalize_text(original))
                base = max(
                    self._base_similarity(q, n) for q in qnorms for n in names if n
                ) if qnorms else 0.0
                if base < 70:
                    continue

                release = row.get("release_date") or row.get("first_air_date")
                tmdb_poster = self._tmdb_img(row.get("poster_path"), "w500")
                draft = Title(
                    imdb_id="",
                    title=name,
                    tmdb_title=name,
                    original_title=self._clean_text(original) if original else None,
                    tmdb_poster_url=tmdb_poster,
                    sources=["tmdb"],
                    year=self._year_from_date(release),
                    kind="Movie" if media_type == "movie" else "Web Series",
                    image_url=tmdb_poster,
                    release_date=release or None,
                    tmdb_id=tmdb_id,
                    backdrop_url=self._tmdb_img(row.get("backdrop_path"), self.BACKDROP_SIZE),
                    overview=self._clean_text(row.get("overview")),
                    popularity=self._safe_float(row.get("popularity")),
                )
                drafts.append((base, row.get("popularity") or 0.0, media_type, draft))

            drafts.sort(key=lambda d: (d[0], d[1]), reverse=True)
            drafts = drafts[:5]

            def resolve(entry):
                _, _, media_type, draft = entry
                # Titles TMDB knows but that have no IMDb page yet (new / regional OTT
                # releases) are still returned, with id "tmdb:tv:123" / "tmdb:movie:123".
                # get_movie(), get_season() ... accept that id.
                draft.imdb_id = (
                    self._tmdb_external_imdb_id(media_type, draft.tmdb_id)
                    or f"tmdb:{media_type}:{draft.tmdb_id}"
                )
                return draft

            return [t for t in self._pmap(resolve, drafts, 4) if t]

        except Exception as exc:
            logger.exception("IMDBKit TMDB search failed: %s", exc)
            return []

    # ============================================================
    # Extra sources: Wikipedia (spelling) + TVMaze (OTT series)
    # ============================================================

    _WIKI_NOT_MEDIA = re.compile(r"\(([^)]*)\)\s*$")
    _WIKI_MEDIA_HINT = re.compile(r"film|series|miniseries|tv|drama|show|anime|serial|web|season", re.I)

    def _wiki_corrections(self, query: str) -> List[str]:
        """
        Wikipedia knows almost every film / series name, in every language and from
        every platform, and its search is typo tolerant.  We only use it to learn the
        *correct spelling*; the real data still comes from IMDb / TMDB.
        """
        query = (query or "").strip()
        if not self.use_wikipedia or len(query) < 3:
            return []
        try:
            data = self._cached_request_json(
                self.WIKI_API_URL,
                {
                    "action": "query", "list": "search", "srsearch": query,
                    "srnamespace": 0, "srlimit": 5, "srinfo": "suggestion",
                    "srprop": "", "format": "json", "utf8": 1,
                },
            )
            block = (data or {}).get("query", {}) if isinstance(data, dict) else {}
            names: List[str] = []

            suggestion = (block.get("searchinfo") or {}).get("suggestion")
            if suggestion:
                names.append(suggestion)

            for row in block.get("search", [])[:5]:
                title = row.get("title") or ""
                note = self._WIKI_NOT_MEDIA.search(title)
                if note and not self._WIKI_MEDIA_HINT.search(note.group(1)):
                    continue      # "(politician)", "(surname)" ...
                title = self._WIKI_NOT_MEDIA.sub("", title).strip()
                if title:
                    names.append(title)

            return self._dedupe(normalize_text(n) for n in names)[:4]
        except Exception as exc:
            logger.warning("IMDBKit Wikipedia lookup failed: %s", exc)
            return []

    _TVMAZE_SKIP_TYPES = {"talk show", "news", "sports", "variety", "panel show", "award show"}

    def _tvmaze_titles(self, query: str) -> List[Title]:
        if not self.use_tvmaze or not query:
            return []
        try:
            rows = self._cached_request_json(f"{self.TVMAZE_URL}/search/shows", {"q": query})
            titles: List[Title] = []
            for row in rows if isinstance(rows, list) else []:
                show = row.get("show") if isinstance(row, dict) else None
                if not isinstance(show, dict):
                    continue
                if str(show.get("type") or "").lower() in self._TVMAZE_SKIP_TYPES:
                    continue
                imdb_id = (show.get("externals") or {}).get("imdb")
                name = self._clean_text(show.get("name"))
                if not imdb_id or not name:
                    continue
                channel = show.get("webChannel") or show.get("network") or {}
                image = show.get("image") or {}
                titles.append(
                    Title(
                        imdb_id=self._normalize_imdb_id(imdb_id),
                        title=name,
                        year=self._year_from_date(show.get("premiered")),
                        kind="Web Series",
                        image_url=image.get("original") or image.get("medium"),
                        end_year=self._year_from_date(show.get("ended")),
                        release_date=show.get("premiered") or None,
                        platform=self._clean_text(channel.get("name")),
                    )
                )
            return titles
        except Exception as exc:
            logger.warning("IMDBKit TVMaze lookup failed: %s", exc)
            return []

    @classmethod
    def add_titles(cls, titles: Iterable[str]) -> None:
        """
        Teach the spell checker your own catalogue (e.g. the titles you scraped from
        MX Player / JioHotstar).  Misspellings of these titles are then corrected too.
        """
        cls._vocab.add_many(titles)

    @classmethod
    def save_vocabulary(cls, path: str) -> None:
        cls._vocab.save(path)

    @classmethod
    def load_vocabulary(cls, path: str) -> None:
        cls._vocab.load(path)

    # ============================================================
    # Spell rescue
    # ============================================================

    def _correction_queries(self, parsed: Dict[str, Any]) -> List[str]:
        """Corrected spellings from the vocabulary and the optional AI corrector."""
        main = normalize_text(parsed.get("title")) or normalize_text(parsed.get("title_loose"))
        corrections: List[str] = []

        if main:
            for title in self._vocab.correct(main, limit=3):
                corrections.append(normalize_text(title))

        if main:
            corrections.extend(self._wiki_corrections(parsed.get("title") or main))

        if self.ai_corrector:
            try:
                suggestion = self.ai_corrector(parsed.get("title") or parsed.get("raw") or "")
                if isinstance(suggestion, str):
                    suggestion = [suggestion]
                for value in suggestion or []:
                    value = normalize_text(value)
                    if value:
                        corrections.append(value)
            except Exception as exc:
                logger.warning("IMDBKit ai_corrector failed: %s", exc)

        return self._dedupe(corrections)

    @staticmethod
    def _is_confident(parsed: Dict[str, Any], ranked: List[Tuple[float, float, Title]]) -> bool:
        """True when the best hit is good enough that spell rescue is unnecessary."""
        if not ranked:
            return False
        _, base, top = ranked[0]
        if base >= GOOD_MATCH:
            return True
        year = parsed.get("year")
        return bool(year and top.year == year and base >= 80)

    @staticmethod
    def _compact_forms(parsed: Dict[str, Any]) -> set:
        forms = set()
        for key in ("title", "title_loose", "title_with_year"):
            norm = normalize_text(parsed.get(key))
            if norm:
                forms.add(strip_article(norm).replace(" ", ""))
        for alt in parsed.get("alt_titles", []):
            norm = normalize_text(alt)
            if norm:
                forms.add(strip_article(norm).replace(" ", ""))
        return forms

    _MERGE_ATTRS = (
        "year", "kind", "image_url", "end_year", "cast_text", "rank",
        "popularity", "release_date", "tmdb_id", "backdrop_url", "overview",
        "platform", "imdb_title", "tmdb_title", "original_title",
        "imdb_poster_url", "tmdb_poster_url",
    )

    @classmethod
    def _absorb(cls, current: Title, item: Title) -> None:
        """Copy everything `item` knows into `current` (same film, other database)."""
        for attr in cls._MERGE_ATTRS:
            if getattr(current, attr) in (None, "") and getattr(item, attr) not in (None, ""):
                setattr(current, attr, getattr(item, attr))
        # every spelling is kept, never thrown away
        current.alt_titles = unique_names(
            list(current.alt_titles or []) + list(item.alt_titles or []) + [item.title]
        )
        for src in item.sources or []:
            if src not in current.sources:
                current.sources.append(src)

    @classmethod
    def _merge(cls, store: Dict[str, Title], groups: Iterable[Iterable[Title]]) -> None:
        for group in groups:
            for item in group:
                if not item.imdb_id:
                    continue
                current = store.get(item.imdb_id)
                if current is None:
                    store[item.imdb_id] = item
                    continue
                if current is not item:
                    cls._absorb(current, item)

    @staticmethod
    def _title_forms(item: Title) -> set:
        forms = set()
        for name in item.names or [item.title]:
            norm = strip_article(normalize_text(name))
            if norm:
                forms.add(norm.replace(" ", ""))
                key = phonetic_key(norm)
                if key:
                    forms.add("p:" + key)
        return forms

    def _collapse_duplicates(self, store: Dict[str, Title]) -> None:
        """
        TMDB sometimes only knows a film as "tmdb:movie:123" while IMDb has "tt..." for the
        very same film (or the other way round), so it shows up twice with two spellings.
        Same year (+-1) + same kind + same (or same-sounding) name  ->  one entry, both
        names.  Only a "tmdb:..." placeholder is ever folded into another entry.
        """
        def bucket(item: Title):
            return "series" if item.kind in SERIES_KINDS else "movie"

        # real IMDb ids first, so they win as the primary entry
        keys = sorted(store, key=lambda k: (not k.startswith("tt"), k))
        kept: List[str] = []
        for key in keys:
            item = store[key]
            twin = None
            for other_key in kept:
                if key.startswith("tt") and other_key.startswith("tt"):
                    continue          # two real IMDb pages are two different titles
                other = store[other_key]
                if bucket(other) != bucket(item):
                    continue
                if item.tmdb_id and item.tmdb_id == other.tmdb_id:
                    twin = other
                    break
                if item.year and other.year and abs(item.year - other.year) > 1:
                    continue
                if (item.year is None) != (other.year is None):
                    continue
                if self._title_forms(item) & self._title_forms(other):
                    twin = other
                    break
            if twin is None:
                kept.append(key)
            else:
                self._absorb(twin, item)
                del store[key]

    def _enrich_titles(self, titles: List[Title]) -> None:
        """
        Make every top result carry BOTH databases' data (name + poster).

        * IMDb hit  -> TMDB /find/<tt id>      gives the TMDB spelling + TMDB poster
        * TMDB hit  -> IMDb suggestion by id   gives the IMDb spelling + IMDb poster
        """
        def work(item: Title) -> Title:
            try:
                if item.imdb_id.startswith("tt"):
                    if self.tmdb_api_key and not item.tmdb_title:
                        find = self._tmdb_find(item.imdb_id)
                        ref = self._pick_tmdb_ref(find)
                        if ref and ref[0] in ("movie", "tv") and find:
                            rows = find.get("movie_results" if ref[0] == "movie" else "tv_results") or []
                            row = rows[0] if rows else {}
                            name = self._clean_text(row.get("title") or row.get("name"))
                            original = self._clean_text(
                                row.get("original_title") or row.get("original_name")
                            )
                            poster = self._tmdb_img(row.get("poster_path"), "w500")
                            if name:
                                item.tmdb_title = name
                            if original and not item.original_title:
                                item.original_title = original
                            if poster and not item.tmdb_poster_url:
                                item.tmdb_poster_url = poster
                            item.tmdb_id = item.tmdb_id or self._safe_int(row.get("id"))
                            if not item.backdrop_url:
                                item.backdrop_url = self._tmdb_img(
                                    row.get("backdrop_path"), self.BACKDROP_SIZE
                                )
                            if not item.overview:
                                item.overview = self._clean_text(row.get("overview"))
                            if not item.image_url and poster:
                                item.image_url = poster
                            if "tmdb" not in item.sources:
                                item.sources.append("tmdb")
                    if not item.imdb_title:
                        found = self._find_imdb_title(item.imdb_id)
                        if found:
                            item.imdb_title = found.title
                            item.imdb_poster_url = item.imdb_poster_url or found.image_url
                            item.image_url = item.image_url or found.image_url
                            item.cast_text = item.cast_text or found.cast_text
                            if "imdb" not in item.sources:
                                item.sources.append("imdb")
                item.alt_titles = unique_names(
                    list(item.alt_titles or [])
                    + [item.imdb_title, item.tmdb_title, item.original_title]
                )
            except Exception as exc:
                logger.warning("IMDBKit enrich failed for %s: %s", item.imdb_id, exc)
            return item

        self._pmap(work, titles, 4)

    # ============================================================
    # Search
    # ============================================================

    def search_title(
        self,
        query: str,
        results: int = 10,
        year: Optional[int] = None,
        kind: Optional[str] = None,
        include_upcoming: bool = True,
    ) -> SearchResult:
        """
        Spell-tolerant title search.

        query             Free text.  "interstelar", "pathan 2023", "panchayat s03e02",
                          "मिर्जापुर", "Pushpa.2021.1080p.WEB-DL" all work.
        year              Optional year filter/boost (past or future).
        kind              None | "movie" | "series".
        include_upcoming  Include titles that are not released yet.
        """
        raw_query = "" if query is None else str(query).strip()
        parsed = parse_media_query(raw_query)
        if year:
            parsed["year"] = self._safe_int(year)

        empty = SearchResult(
            [], query=raw_query, year=parsed["year"],
            season=parsed["season"], episode=parsed["episode"],
        )
        if not (parsed["title"] or parsed["title_loose"]):
            return empty

        results = max(1, min(self._safe_int(results) or 10, 20))

        kind_filter = None
        wanted = str(kind or "").strip().lower()
        if wanted in ("movie", "film", "movies"):
            kind_filter = {"Movie", "TV Movie"}
        elif wanted in ("series", "tv", "show", "web series", "tvseries"):
            kind_filter = set(SERIES_KINDS)

        candidates: Dict[str, Title] = {}
        primary = build_primary_queries(parsed)
        corrections: List[str] = []

        tmdb_pool = None
        tmdb_future = None
        if self.tmdb_api_key:
            tmdb_pool = ThreadPoolExecutor(max_workers=1)
            main_norm = normalize_text(parsed["title"]) or normalize_text(parsed["title_loose"])
            tmdb_queries = self._dedupe(primary[:2] + double_letter_variants(main_norm, 2))
            tmdb_future = tmdb_pool.submit(self._tmdb_search_titles, parsed, tmdb_queries)

        try:
            # ---- wave 1: user text + Roman-Hindi respellings, in parallel ----
            self._merge(candidates, self._pmap(self._imdb_suggest_titles, primary, self.max_workers))

            ranked = self._rank(parsed, candidates.values(), kind_filter, include_upcoming)
            weak = not self._is_confident(parsed, ranked)

            # ---- wave 2: spell rescue (vocabulary / AI / prefixes) ----
            if weak:
                corrections = self._correction_queries(parsed)
                main = normalize_text(parsed["title"]) or normalize_text(parsed["title_loose"])
                retrieval = self._dedupe(
                    corrections + double_letter_variants(main) + prefix_variants(main) + word_variants(main)
                )
                retrieval = [q for q in retrieval if q not in primary][:10]
                if retrieval:
                    self._merge(
                        candidates,
                        self._pmap(self._imdb_suggest_titles, retrieval, self.max_workers),
                    )

            if weak and self.use_tvmaze and kind_filter != {"Movie", "TV Movie"}:
                main_q = normalize_text(parsed["title"]) or normalize_text(parsed["title_loose"])
                tv_queries = self._dedupe([main_q] + corrections[:2])
                self._merge(candidates, self._pmap(self._tvmaze_titles, tv_queries, self.max_workers))

            if tmdb_future is not None:
                try:
                    self._merge(candidates, [tmdb_future.result(timeout=self.timeout * 3)])
                except Exception as exc:
                    logger.warning("IMDBKit TMDB search skipped: %s", exc)

            ranked = self._rank(
                parsed, candidates.values(), kind_filter, include_upcoming, corrections
            )

            # TMDB second chance with corrected spelling
            if self.tmdb_api_key and corrections and not self._is_confident(parsed, ranked):
                self._merge(candidates, [self._tmdb_search_titles(parsed, corrections[:2])])
                ranked = self._rank(
                    parsed, candidates.values(), kind_filter, include_upcoming, corrections
                )

            # ---- both databases for every candidate ----------------------------------
            # IMDb and TMDB often spell the same film differently ("Nayi Naveli" /
            # "Nayyi Navelli"). Give each top candidate the other database's name and
            # poster, then rank again so the film is found under either spelling.
            self._collapse_duplicates(candidates)
            top = self._rank(
                parsed, candidates.values(), kind_filter, include_upcoming,
                corrections, relaxed=True,
            )
            self._enrich_titles([row[2] for row in top[:6]])
            self._collapse_duplicates(candidates)
            ranked = self._rank(
                parsed, candidates.values(), kind_filter, include_upcoming, corrections
            )
        finally:
            if tmdb_pool:
                tmdb_pool.shutdown(wait=False)

        # Nothing passed the quality bar -> still return the closest titles (flagged
        # "weak") so the caller can show suggestions instead of "not found".
        if not ranked:
            ranked = self._rank(
                parsed, candidates.values(), kind_filter, include_upcoming,
                corrections, relaxed=True,
            )

        titles: List[Title] = []
        for score, _, item in ranked[:results]:
            item.score = round(score, 1)
            titles.append(item)

        # "Did you mean": only for a real spelling correction (never for a partial or
        # weak look-alike like  "the mongoose" -> "The Mouse").
        corrected = None
        if titles and titles[0].match_type in ("phonetic", "close"):
            top_form = strip_article(normalize_text(titles[0].title)).replace(" ", "")
            if top_form not in self._compact_forms(parsed):
                corrected = titles[0].title

        year_found = None
        if parsed["year"]:
            good = [t for t in titles if t.match_type in ("exact", "phonetic", "close")] or titles
            year_found = any(t.year_match in ("exact", "within") for t in good)

        if titles:
            # only trustworthy titles teach the spell-check vocabulary
            self._vocab.add_many(
                t.title for t in titles if t.match_type in ("exact", "phonetic", "close")
            )

        return SearchResult(
            titles,
            query=raw_query,
            corrected_query=corrected,
            year=parsed["year"],
            season=parsed["season"],
            episode=parsed["episode"],
            parsed_title=parsed["title"] or parsed["title_loose"],
            year_found=year_found,
        )

    def search_movie(self, title: str, results: int = 10) -> SearchResult:
        """Backward compatible entry point (same as search_title)."""
        return self.search_title(title, results=results)

    def search(self, title: str, results: int = 10) -> SearchResult:
        return self.search_title(title, results=results)

    def suggestions(self, query: str, limit: int = 6, **kwargs) -> List[Dict[str, Any]]:
        """Compact dicts for inline buttons / autocomplete."""
        result = self.search_title(query, results=limit, **kwargs)
        return [
            {
                "imdb_id": t.imdb_id,
                "title": t.title,
                "year": t.year,
                "year_text": t.year_text,
                "kind": t.kind,
                "label": t.label,
                "poster": t.image_url,
                "backdrop": t.backdrop_url,
                "cast": t.cast_text,
                "upcoming": t.is_upcoming,
            }
            for t in result.titles
        ]

    # ============================================================
    # IMDb title page (JSON-LD) - rating / plot / runtime fallback
    # ============================================================

    @staticmethod
    def _ld_people(value: Any) -> List[str]:
        if not value:
            return []
        if not isinstance(value, list):
            value = [value]
        names = []
        for entry in value:
            if isinstance(entry, dict) and entry.get("@type", "Person") == "Person":
                name = entry.get("name")
                if name:
                    names.append(html_lib.unescape(str(name)))
        return names

    @staticmethod
    def _iso_duration_minutes(value: Any) -> Optional[int]:
        match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?", str(value or ""))
        if not match or not (match.group(1) or match.group(2)):
            return None
        return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)

    def _imdb_page_data(self, imdb_id: str) -> Dict[str, Any]:
        """
        Best-effort read of the public IMDb title page (schema.org JSON-LD).
        Gives the *real* IMDb rating/votes and works without TMDB.
        Never raises; returns {} when IMDb blocks the request.
        """
        imdb_id = self._normalize_imdb_id(imdb_id)
        if not imdb_id or not self.imdb_page_fallback:
            return {}

        cache_key = f"page:{imdb_id}"
        found, cached = self._page_cache.get(cache_key)
        if found:
            return cached or {}

        def fetch():
            data: Dict[str, Any] = {}
            try:
                body = self._http_get(
                    f"{self.IMDb_TITLE_URL}{imdb_id}/",
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                original_title = None
                om = re.search(
                    r'"originalTitleText"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"', body or ""
                )
                if om:
                    try:
                        original_title = json.loads(f'"{om.group(1)}"')
                    except ValueError:
                        original_title = om.group(1)
                match = re.search(
                    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                    body or "",
                    re.S | re.I,
                )
                if match:
                    ld = json.loads(match.group(1))
                    if isinstance(ld, list) and ld:
                        ld = ld[0]
                    if isinstance(ld, dict):
                        agg = ld.get("aggregateRating") or {}
                        genre = ld.get("genre")
                        if isinstance(genre, str):
                            genre = [genre]
                        data = {
                            "type": ld.get("@type"),
                            "title": html_lib.unescape(ld["name"]) if ld.get("name") else None,
                            "plot": html_lib.unescape(ld["description"]) if ld.get("description") else None,
                            "image": ld.get("image") if isinstance(ld.get("image"), str) else None,
                            "rating": self._safe_float(agg.get("ratingValue")),
                            "votes": self._safe_int(agg.get("ratingCount")),
                            "runtime": self._iso_duration_minutes(ld.get("duration")),
                            "genres": [g for g in (genre or []) if g],
                            "date": ld.get("datePublished"),
                            "certificate": ld.get("contentRating"),
                            "directors": self._ld_people(ld.get("director")),
                            "writers": self._ld_people(ld.get("creator")),
                            "stars": self._ld_people(ld.get("actor")),
                            "original_title": html_lib.unescape(
                                original_title or ld.get("alternateName") or ""
                            ) or None,
                        }
                elif original_title:
                    data = {"original_title": html_lib.unescape(original_title)}
            except Exception as exc:
                logger.warning("IMDBKit IMDb page parse failed for %s: %s", imdb_id, exc)
                data = {}
            self._page_cache.set(cache_key, data)
            return data

        return self._coalescer.execute(cache_key, fetch) or {}

    # ============================================================
    # TMDB lookups
    # ============================================================

    def _tmdb_find(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        if not self.tmdb_api_key:
            return None
        return self._tmdb_get(
            f"/find/{imdb_id}", external_source="imdb_id", language=self.language
        )

    @staticmethod
    def _pick_tmdb_ref(find: Optional[Dict[str, Any]]) -> Optional[Tuple[str, Any, Optional[Dict[str, Any]]]]:
        """(media_type, tmdb_id, episode_row) from a /find response."""
        if not find:
            return None
        if find.get("movie_results"):
            return "movie", find["movie_results"][0].get("id"), None
        if find.get("tv_results"):
            return "tv", find["tv_results"][0].get("id"), None
        if find.get("tv_episode_results"):
            row = find["tv_episode_results"][0]
            return "tv", row.get("show_id"), row
        if find.get("tv_season_results"):
            row = find["tv_season_results"][0]
            return "tv", row.get("show_id"), None
        return None

    def _tmdb_details(self, media_type: str, tmdb_id: Any) -> Optional[Dict[str, Any]]:
        if not self.tmdb_api_key or not tmdb_id:
            return None

        lang2 = self.language.split("-")[0]
        languages = ",".join(self._dedupe([lang2, "en", "hi", "null"]))
        append = (
            "credits,external_ids,alternative_titles,images,videos,release_dates,watch/providers"
            if media_type == "movie"
            else "credits,external_ids,alternative_titles,images,videos,content_ratings,watch/providers"
        )

        data = self._tmdb_get(
            f"/{media_type}/{tmdb_id}",
            language=self.language,
            append_to_response=append,
            include_image_language=languages,
            include_video_language=languages,
        )

        # Plot missing in the requested language -> fall back to English.
        if data and not data.get("overview") and not self.language.lower().startswith("en"):
            english = self._tmdb_get(f"/{media_type}/{tmdb_id}", language="en-US")
            if english:
                data = dict(data)
                data["overview"] = english.get("overview")
                data["tagline"] = data.get("tagline") or english.get("tagline")

        return data

    # ============================================================
    # Movie builder
    # ============================================================

    def _pick_images(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Best poster (portrait) + backdrop (landscape) + logo, with extras."""
        images = data.get("images") or {}
        lang2 = self.language.split("-")[0]
        preferred = {lang2, "en", data.get("original_language")}

        def votes(row):
            return (-(row.get("vote_average") or 0), -(row.get("vote_count") or 0))

        def poster_key(row):
            lang = row.get("iso_639_1")
            return ((0 if lang in preferred else 1 if lang is None else 2),) + votes(row)

        def backdrop_key(row):
            return ((0 if row.get("iso_639_1") is None else 1),) + votes(row)

        posters = sorted([r for r in images.get("posters", []) if r.get("file_path")], key=poster_key)
        backdrops = sorted([r for r in images.get("backdrops", []) if r.get("file_path")], key=backdrop_key)
        logos = sorted(
            [r for r in images.get("logos", []) if r.get("file_path")],
            key=lambda r: ((0 if r.get("iso_639_1") in preferred else 1),) + votes(r),
        )

        # TMDB's own default image always goes first.
        poster_paths = self._dedupe([data.get("poster_path")] + [r["file_path"] for r in posters])[:8]
        backdrop_paths = self._dedupe([data.get("backdrop_path")] + [r["file_path"] for r in backdrops])[:8]

        return {
            "poster": self._tmdb_img(poster_paths[0], self.POSTER_SIZE) if poster_paths else None,
            "backdrop": self._tmdb_img(backdrop_paths[0], self.BACKDROP_SIZE) if backdrop_paths else None,
            "posters": [self._tmdb_img(p, self.POSTER_SIZE) for p in poster_paths],
            "backdrops": [self._tmdb_img(p, self.BACKDROP_SIZE) for p in backdrop_paths],
            "logo": self._tmdb_img(logos[0]["file_path"], self.LOGO_SIZE) if logos else None,
        }

    @staticmethod
    def _pick_trailer(data: Dict[str, Any]) -> Optional[str]:
        videos = [
            v for v in (data.get("videos") or {}).get("results", [])
            if v.get("site") == "YouTube" and v.get("key")
        ]
        if not videos:
            return None

        def rank(video):
            kind = {"Trailer": 0, "Teaser": 1}.get(video.get("type"), 2)
            return (kind, 0 if video.get("official") else 1)

        videos.sort(key=rank)
        return f"https://www.youtube.com/watch?v={videos[0]['key']}"

    @staticmethod
    def _tmdb_certificates(data: Dict[str, Any], media_type: Optional[str]) -> List[str]:
        out: List[str] = []
        if media_type == "movie":
            for entry in (data.get("release_dates") or {}).get("results", []):
                region = entry.get("iso_3166_1")
                if region not in ("IN", "US"):
                    continue
                for release in entry.get("release_dates", []):
                    cert = (release.get("certification") or "").strip()
                    if cert:
                        out.append(f"{region}:{cert}")
                        break
        else:
            for entry in (data.get("content_ratings") or {}).get("results", []):
                region = entry.get("iso_3166_1")
                cert = (entry.get("rating") or "").strip()
                if region in ("IN", "US") and cert:
                    out.append(f"{region}:{cert}")
        # India first
        out.sort(key=lambda value: 0 if value.startswith("IN") else 1)
        return IMDBKit._dedupe(out)

    def _episode_from_tmdb(
        self, row: Optional[Dict[str, Any]], season: Optional[int] = None
    ) -> Optional[Episode]:
        if not isinstance(row, dict):
            return None
        number = self._safe_int(row.get("episode_number"))
        season_number = self._safe_int(row.get("season_number"))
        season_number = season_number if season_number is not None else season
        if number is None or season_number is None:
            return None

        external = row.get("external_ids") or {}
        votes = self._safe_int(row.get("vote_count"))
        return Episode(
            season=season_number,
            episode=number,
            title=self._clean_text(row.get("name")),
            plot=self._clean_text(row.get("overview")),
            air_date=row.get("air_date") or None,
            runtime=self._safe_int(row.get("runtime")),
            rating=self._safe_float(row.get("vote_average")) if votes else None,
            votes=votes,
            still_url=self._tmdb_img(row.get("still_path"), self.STILL_SIZE),
            imdb_id=self._normalize_imdb_id(external.get("imdb_id")) if external.get("imdb_id") else None,
            tmdb_id=self._safe_int(row.get("id")),
        )

    def _build_movie(
        self,
        imdb_id: str,
        imdb_title: Optional[Title],
        tmdb_data: Optional[Dict[str, Any]],
        media_type: Optional[str],
        page: Optional[Dict[str, Any]] = None,
    ) -> Movie:
        page = page or {}
        data = tmdb_data or {}

        if not media_type and page.get("type") in ("TVSeries", "TVMiniSeries"):
            media_type = "tv"
        is_tv = media_type == "tv"

        # ---------- titles ----------
        title = (
            (imdb_title.title if imdb_title else None)
            or page.get("title")
            or data.get("title")
            or data.get("name")
        )
        tmdb_title = data.get("title") or data.get("name")
        localized_title = tmdb_title if tmdb_title and tmdb_title != title else None
        # TMDB original title first, else the one IMDb shows ("Original title: ...")
        original_title = (
            data.get("original_title") or data.get("original_name") or page.get("original_title")
        )
        imdb_name = (imdb_title.title if imdb_title else None) or page.get("title")

        # ---------- dates ----------
        release_date = data.get("release_date") or data.get("first_air_date") or page.get("date")
        year = (
            (imdb_title.year if imdb_title else None)
            or self._year_from_date(release_date)
        )

        # ---------- kind ----------
        imdb_kind = imdb_title.kind if imdb_title else None
        if is_tv:
            kind = imdb_kind if imdb_kind in SERIES_KINDS else "Web Series"
        elif media_type == "movie":
            kind = "TV Movie" if imdb_kind == "TV Movie" else "Movie"
        else:
            kind = imdb_kind

        # ---------- rating ----------
        tmdb_votes = self._safe_int(data.get("vote_count"))
        tmdb_rating = self._safe_float(data.get("vote_average")) if tmdb_votes else None
        if tmdb_rating is not None:
            tmdb_rating = round(tmdb_rating, 1)

        if page.get("rating") is not None:
            rating, votes, rating_source = page["rating"], page.get("votes"), "IMDb"
        elif tmdb_rating is not None:
            rating, votes, rating_source = tmdb_rating, tmdb_votes, "TMDB"
        else:
            rating, votes, rating_source = None, None, None

        # ---------- runtime ----------
        runtime = self._safe_int(data.get("runtime"))
        if not runtime and is_tv:
            episode_runtime = data.get("episode_run_time")
            if isinstance(episode_runtime, list) and episode_runtime:
                runtime = self._safe_int(episode_runtime[0])
            runtime = (
                runtime
                or self._safe_int((data.get("last_episode_to_air") or {}).get("runtime"))
                or self._safe_int((data.get("next_episode_to_air") or {}).get("runtime"))
            )
        runtime = runtime or page.get("runtime")

        # ---------- status ----------
        status = data.get("status") or None
        if not status:
            status = "Upcoming" if (year and year > date.today().year) else "Released"

        # ---------- images ----------
        pics = self._pick_images(data)
        imdb_image = (
            self.imdb_image(page.get("image"), 800)
            or (self.imdb_image(imdb_title.image_url, 800) if imdb_title and imdb_title.image_url else None)
        )
        cover_url = pics["poster"] or imdb_image
        backdrop_url = pics["backdrop"]

        # ---------- people ----------
        cast: List[str] = []
        cast_detail: List[Dict[str, Any]] = []
        directors: List[str] = []
        writers: List[str] = []
        producers: List[str] = []
        composers: List[str] = []
        cinematographers: List[str] = []
        music_team: List[str] = []

        credits = data.get("credits") or {}
        for person in (credits.get("cast") or [])[:20]:
            name = person.get("name") if isinstance(person, dict) else None
            if name:
                cast.append(name)
                cast_detail.append(
                    {
                        "name": name,
                        "character": person.get("character") or None,
                        "photo": self._tmdb_img(person.get("profile_path"), self.PROFILE_SIZE),
                    }
                )

        for person in credits.get("crew") or []:
            if not isinstance(person, dict) or not person.get("name"):
                continue
            name = person["name"]
            department = (person.get("department") or "").lower()
            job = (person.get("job") or "").lower()

            if department == "directing" and job == "director":
                directors.append(name)
            elif department == "writing":
                writers.append(name)
            elif department == "production" and job in {"producer", "executive producer"}:
                producers.append(name)
            elif department == "sound":
                if "composer" in job:
                    composers.append(name)
                else:
                    music_team.append(name)
            elif department == "camera" and "photography" in job:
                cinematographers.append(name)

        creators = [c.get("name") for c in data.get("created_by") or [] if isinstance(c, dict)]

        directors = directors or page.get("directors") or []
        writers = writers or page.get("writers") or creators
        cast = cast or page.get("stars") or []

        # ---------- misc lists ----------
        countries = [
            c.get("name") for c in data.get("production_countries") or []
            if isinstance(c, dict) and c.get("name")
        ] or list(data.get("origin_country") or [])

        languages = [
            (l.get("english_name") or l.get("name") or l.get("iso_639_1"))
            for l in data.get("spoken_languages") or [] if isinstance(l, dict)
        ]
        languages = [l for l in languages if l] or (
            [data["original_language"]] if data.get("original_language") else []
        )

        genres = [
            g.get("name") for g in data.get("genres") or []
            if isinstance(g, dict) and g.get("name")
        ] or page.get("genres") or []

        alt = data.get("alternative_titles") or {}
        title_akas = self._dedupe(
            (row.get("title") or row.get("name"))
            for row in (alt.get("titles") or alt.get("results") or [])
            if isinstance(row, dict)
        )
        certificates = self._tmdb_certificates(data, media_type)
        if not certificates and page.get("certificate"):
            certificates = [str(page["certificate"])]

        # ---------- series ----------
        seasons: List[Dict[str, Any]] = []
        if is_tv:
            today = date.today().isoformat()
            for row in data.get("seasons") or []:
                number = self._safe_int(row.get("season_number")) if isinstance(row, dict) else None
                if not number or number < 1:       # skip "Specials" (season 0)
                    continue
                air_date = row.get("air_date") or None
                seasons.append(
                    {
                        "season": number,
                        "code": f"S{number:02d}",
                        "name": row.get("name") or f"Season {number}",
                        "episodes": row.get("episode_count"),
                        "air_date": air_date,
                        "poster_url": self._tmdb_img(row.get("poster_path"), "w500"),
                        "plot": self._clean_text(row.get("overview")),
                        "upcoming": bool(not air_date or air_date > today),
                    }
                )

        providers = (((data.get("watch/providers") or {}).get("results") or {}).get(self.region) or {})
        streaming_on = self._dedupe(
            p.get("provider_name")
            for group in ("flatrate", "free", "ads")
            for p in providers.get(group, []) or []
            if isinstance(p, dict)
        )

        box_office = None
        if media_type == "movie":
            box_office = data.get("revenue") or None

        imdb_url = f"{self.IMDb_TITLE_URL}{imdb_id}/" if imdb_id else None

        return Movie(
            release_date=release_date,
            year=year,
            plot=self._clean_text(data.get("overview")) or page.get("plot"),
            imdb_id=imdb_id,
            title=title,
            votes=votes,
            title_akas=title_akas,
            info_series=SeriesInfo(display_seasons=seasons),
            worldwide_gross=box_office,
            title_localized=localized_title,
            kind=kind,
            stars=cast,
            duration=runtime,
            countries=countries,
            certificates=certificates,
            languages=languages,
            directors=self._dedupe(directors),
            writers=self._dedupe(writers),
            producers=self._dedupe(producers),
            composers=self._dedupe(composers),
            cinematographers=self._dedupe(cinematographers),
            music_team=self._dedupe(music_team),
            distributors=[],
            genres=genres,
            rating=rating,
            cover_url=cover_url,
            url=imdb_url,
            original_title=original_title,
            tagline=self._clean_text(data.get("tagline")),
            status=status,
            episodes=self._safe_int(data.get("number_of_episodes")),
            backdrop_url=backdrop_url,
            posters=pics["posters"],
            backdrops=pics["backdrops"],
            logo_url=pics["logo"],
            trailer_url=self._pick_trailer(data),
            imdb_poster_url=imdb_image,
            tmdb_poster_url=pics["poster"],
            imdb_title=imdb_name,
            tmdb_title=tmdb_title,
            tmdb_id=self._safe_int(data.get("id")),
            media_type=media_type,
            original_language=data.get("original_language"),
            rating_source=rating_source,
            tmdb_rating=tmdb_rating,
            tmdb_votes=tmdb_votes,
            creators=self._dedupe(creators),
            networks=self._dedupe(n.get("name") for n in data.get("networks") or [] if isinstance(n, dict)),
            production_companies=self._dedupe(
                c.get("name") for c in data.get("production_companies") or [] if isinstance(c, dict)
            ),
            cast_detail=cast_detail,
            streaming_on=streaming_on,
            streaming_region=self.region if streaming_on else None,
            number_of_seasons=self._safe_int(data.get("number_of_seasons")),
            next_episode=self._episode_from_tmdb(data.get("next_episode_to_air")),
            last_episode=self._episode_from_tmdb(data.get("last_episode_to_air")),
        )

    # ============================================================
    # Get Movie
    # ============================================================

    @staticmethod
    def _result_or(future: Any, default: Any = None) -> Any:
        try:
            return future.result()
        except Exception as exc:
            logger.warning("IMDBKit background lookup failed: %s", exc)
            return default

    @staticmethod
    def _parse_tmdb_id(value: Any) -> Optional[Tuple[str, int]]:
        """'tmdb:tv:123' -> ('tv', 123)"""
        match = re.match(r"^tmdb:(movie|tv):(\d+)$", str(value or "").strip())
        return (match.group(1), int(match.group(2))) if match else None

    def _get_movie_from_tmdb(self, media_type: str, tmdb_id: int) -> Movie:
        data = self._tmdb_details(media_type, tmdb_id)
        if not data:
            return Movie()

        imdb_id = (data.get("external_ids") or {}).get("imdb_id")
        if imdb_id:                       # IMDb page appeared in the meantime
            return self.get_movie(imdb_id)

        movie = self._build_movie(f"tmdb:{media_type}:{tmdb_id}", None, data, media_type, {})
        movie.url = f"https://www.themoviedb.org/{media_type}/{tmdb_id}"
        return movie

    def get_movie(self, movie_id: Any) -> Movie:
        """
        Fetch complete movie/series metadata (IMDb id or numeric id).

        For an *episode* IMDb id the returned object describes the series and has
        .episode_info filled (kind == "Episode").
        """
        tmdb_ref = self._parse_tmdb_id(movie_id)
        if tmdb_ref:
            return self._get_movie_from_tmdb(*tmdb_ref)

        imdb_id = self._normalize_imdb_id(movie_id)

        if not imdb_id:
            return Movie()

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_title = pool.submit(self._find_imdb_title, imdb_id)
            f_find = pool.submit(self._tmdb_find, imdb_id)
            f_page = pool.submit(self._imdb_page_data, imdb_id)
            imdb_title = self._result_or(f_title)
            tmdb_find = self._result_or(f_find)
            page = self._result_or(f_page, {}) or {}

        ref = self._pick_tmdb_ref(tmdb_find)
        tmdb_data = None
        media_type = None
        episode_row = None

        if ref:
            media_type, tmdb_id, episode_row = ref
            tmdb_data = self._tmdb_details(media_type, tmdb_id)

        if episode_row and tmdb_data:
            series_imdb = (tmdb_data.get("external_ids") or {}).get("imdb_id") or imdb_id
            movie = self._build_movie(series_imdb, None, tmdb_data, "tv", {})
            movie.series_title = movie.title
            movie.imdb_id = imdb_id
            movie.url = f"{self.IMDb_TITLE_URL}{imdb_id}/"
            movie.kind = "Episode"
            movie.episode_info = self._episode_from_tmdb(dict(episode_row, external_ids={"imdb_id": imdb_id}))
            if movie.episode_info:
                if movie.episode_info.still_url:
                    movie.backdrop_url = movie.episode_info.still_url
                movie.plot = movie.episode_info.plot or movie.plot
                movie.duration = movie.episode_info.runtime or movie.duration
            return movie

        return self._build_movie(
            imdb_id=imdb_id,
            imdb_title=imdb_title,
            tmdb_data=tmdb_data,
            media_type=media_type,
            page=page,
        )

    # ============================================================
    # Seasons / Episodes
    # ============================================================

    def _tmdb_series_id(self, movie_id: Any) -> Optional[Any]:
        if isinstance(movie_id, Movie):
            if movie_id.tmdb_id and movie_id.media_type == "tv":
                return movie_id.tmdb_id
            movie_id = movie_id.imdb_id
        elif isinstance(movie_id, Title):
            movie_id = movie_id.imdb_id

        tmdb_ref = self._parse_tmdb_id(movie_id)
        if tmdb_ref:
            return tmdb_ref[1] if tmdb_ref[0] == "tv" else None

        imdb_id = self._normalize_imdb_id(movie_id)
        if not imdb_id:
            return None

        ref = self._pick_tmdb_ref(self._tmdb_find(imdb_id))
        if ref and ref[0] == "tv":
            return ref[1]
        return None

    def get_season(self, movie_id: Any, season: int) -> Optional[Season]:
        """All episodes of one season (needs a TMDB key)."""
        season = self._safe_int(season)
        tmdb_id = self._tmdb_series_id(movie_id)
        if season is None or not tmdb_id:
            return None

        data = self._tmdb_get(f"/tv/{tmdb_id}/season/{season}", language=self.language)
        if not data:
            return None

        episodes = [
            ep for ep in (self._episode_from_tmdb(row, season) for row in data.get("episodes") or [])
            if ep
        ]
        return Season(
            season=season,
            name=self._clean_text(data.get("name")) or f"Season {season}",
            plot=self._clean_text(data.get("overview")),
            air_date=data.get("air_date") or None,
            poster_url=self._tmdb_img(data.get("poster_path"), self.POSTER_SIZE),
            episodes=episodes,
        )

    def get_episode(self, movie_id: Any, season: int, episode: int) -> Optional[Episode]:
        """One episode with title, plot, runtime, air date, still image and IMDb id."""
        season, episode = self._safe_int(season), self._safe_int(episode)
        tmdb_id = self._tmdb_series_id(movie_id)
        if season is None or episode is None or not tmdb_id:
            return None

        data = self._tmdb_get(
            f"/tv/{tmdb_id}/season/{season}/episode/{episode}",
            language=self.language,
            append_to_response="external_ids",
        )
        return self._episode_from_tmdb(data, season) if data else None

    # ============================================================
    # One-call convenience
    # ============================================================

    def lookup(
        self,
        query: str,
        year: Optional[int] = None,
        kind: Optional[str] = None,
    ) -> Optional[Movie]:
        """
        Search + fetch details of the best match.

        "Panchayat S03E02"  -> series Movie with .episode_info (S03E02)
        "Panchayat season 3" -> series Movie with .season_info
        """
        found = self.search_title(query, results=5, year=year, kind=kind)
        best = found.best
        if not best:
            return None

        movie = self.get_movie(best.imdb_id)

        if movie.is_series and found.season is not None:
            if found.episode is not None:
                movie.episode_info = self.get_episode(movie, found.season, found.episode)
            else:
                movie.season_info = self.get_season(movie, found.season)

        return movie

    # ============================================================
    # Legacy Compatibility
    # ============================================================

    def update(
        self,
        movie: Movie,
        info: Optional[List[str]] = None,
        *args,
        **kwargs,
    ) -> Movie:
        return movie

    def get_movie_details(self, movie_id: Any) -> Dict[str, Any]:
        return self.get_movie(movie_id).to_dict()
