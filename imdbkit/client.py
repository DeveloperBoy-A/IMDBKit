import json
import logging
import os
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from .models import Movie, SearchResult, SeriesInfo, Title

logger = logging.getLogger(__name__)


# ============================================================
# Smart Caching & Concurrency Protection Decorators / Helpers
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
            current_time = time.time()
            
            is_empty = value is None or (isinstance(value, (list, dict)) and not value)
            current_ttl = self.negative_ttl if is_empty else self.ttl

            if current_time - timestamp > current_ttl:
                del self._cache[key]
                return False, None
            return True, value

    def set(self, key: str, value: Any):
        with self._lock:
            if len(self._cache) >= self.maxsize:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            self._cache[key] = (time.time(), value)


class RequestCoalescer:
    """Protects against duplicate concurrent requests for the same key (Dogpile protection)."""
    def __init__(self):
        self._in_flight: Dict[str, threading.Event] = {}
        self._results: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def execute(self, key: str, fetch_func, *args, **kwargs):
        with self._lock:
            if key in self._in_flight:
                event = self._in_flight[key]
                is_waiting = True
            else:
                event = threading.Event()
                self._in_flight[key] = event
                is_waiting = False

        if is_waiting:
            event.wait()
            with self._lock:
                return self._results.get(key)

        try:
            result = fetch_func(*args, **kwargs)
            with self._lock:
                self._results[key] = result
            return result
        finally:
            with self._lock:
                self._in_flight.pop(key, None)
                self._results.pop(key, None)
                event.set()


class IMDBKit:
    """
    Advanced IMDb + TMDB metadata client with comprehensive media filtering,
    Roman-Hindi/Devanagari text intelligence, auto-transliteration, caching, and thread-safe concurrency.
    """

    IMDb_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/x/"
    TMDB_BASE_URL = "https://api.themoviedb.org/3"

    # Global Shared Caches across instances
    _imdb_cache = TTLCache(ttl=86400, negative_ttl=300, maxsize=3000)
    _imdb_id_cache = TTLCache(ttl=172800, negative_ttl=600, maxsize=3000)
    _tmdb_cache = TTLCache(ttl=172800, negative_ttl=600, maxsize=3000)
    _coalescer = RequestCoalescer()

    def __init__(
        self,
        tmdb_api_key: Optional[str] = None,
        timeout: int = 15,
    ):
        self.tmdb_api_key = (
            tmdb_api_key
            or os.getenv("IMDBKIT_TMDB_API_KEY")
            or os.getenv("TMDB_API_KEY")
        )

        self.timeout = timeout

        if self.tmdb_api_key:
            logger.info("IMDBKit: TMDB API key loaded successfully.")
        else:
            logger.warning(
                "IMDBKit: TMDB API key not configured. Using IMDb fallback only."
            )

    # ============================================================
    # HTTP
    # ============================================================

    def _request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            if params:
                query = urllib.parse.urlencode(
                    {
                        key: value
                        for key, value in params.items()
                        if value is not None
                    }
                )

                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{query}"

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "Chrome/120 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                data = response.read().decode("utf-8")

            return json.loads(data)

        except urllib.error.HTTPError as exc:
            logger.error(
                "IMDBKit HTTP error %s for %s",
                exc.code,
                url.split("?")[0],
            )
            try:
                error_body = exc.read().decode("utf-8")
                logger.error(
                    "IMDBKit HTTP response: %s",
                    error_body[:500],
                )
            except Exception:
                pass
            return None

        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            logger.error(
                "IMDBKit network/timeout error for %s: %s",
                url.split("?")[0],
                getattr(exc, "reason", exc),
            )
            return None

        except json.JSONDecodeError as exc:
            logger.error(
                "IMDBKit invalid JSON response from %s: %s",
                url.split("?")[0],
                exc,
            )
            return None

        except Exception as exc:
            logger.exception(
                "IMDBKit unexpected request error for %s: %s",
                url.split("?")[0],
                exc,
            )
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

    # ============================================================
    # Helpers & Text Intelligence (Devanagari Transliteration)
    # ============================================================

    @staticmethod
    def _devanagari_to_roman(text: str) -> str:
        """Converts Hindi Devanagari script queries to Roman/English phonetics for IMDb lookup."""
        if not text:
            return ""
        
        # Mapping for common Hindi characters/words
        mapping = {
            "पठान": "pathaan",
            "स्त्री": "stree",
            "पद्maवत": "padmaavat",
            "दृश्यम": "drishyam",
            "जवान": "jawan",
            "गदर": "gadar",
            "एनिमल": "animal",
            "आरआरआर": "rrr",
            "दंगल": "dangal",
            "शोले": "sholay"
        }
        
        # Check direct word matches first
        words = text.split()
        converted = []
        for w in words:
            if w in mapping:
                converted.append(mapping[w])
            else:
                # Basic fallback transliteration mapping for basic Devanagari letters
                dev_map = {
                    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ee', 'उ': 'u', 'ऊ': 'oo', 'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au',
                    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'n',
                    'च': 'ch', 'छ': 'chh', 'ज': 'j', 'झ': 'jh', 'ञ': 'n',
                    'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh', 'ण': 'n',
                    'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n',
                    'प': 'p', 'फ': 'f', 'ब': 'b', 'भ': 'bh', 'म': 'm',
                    'य': 'y', 'र': 'r', 'ल': 'l', 'व': 'v',
                    'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h',
                    'ा': 'aa', 'ि': 'i', 'ी': 'ee', 'ु': 'u', 'ू': 'oo', 'े': 'e', 'ै': 'ai', 'ो': 'o', 'ौ': 'au', '्': ''
                }
                w_conv = "".join([dev_map.get(char, char) for char in w])
                converted.append(w_conv)
        return " ".join(converted)

    @staticmethod
    def _normalize_imdb_id(
        imdb_id: Any,
    ) -> Optional[str]:
        if imdb_id is None:
            return None

        value = str(imdb_id).strip()

        if not value:
            return None

        if value.startswith("tt"):
            return value

        if value.isdigit():
            return f"tt{value}"

        match = re.search(
            r"(tt\d+)",
            value,
        )

        if match:
            return match.group(1)

        return value

    @staticmethod
    def _clean_text(
        value: Any,
    ) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            return value or None

        return str(value).strip() or None

    @staticmethod
    def _safe_int(
        value: Any,
    ) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _year_from_date(
        value: Any,
    ) -> Optional[int]:
        if not value:
            return None

        match = re.search(
            r"\b(19|20)\d{2}\b",
            str(value),
        )

        if match:
            try:
                return int(match.group(0))
            except Exception:
                pass

        return None

    @staticmethod
    def _roman_hindi_phonetic_normalize(text: str) -> str:
        if not text:
            return ""
        t = text.lower()
        t = re.sub(r"\b(sh|s)ree\b", "shri", t)
        t = re.sub(r"\bk(?:h)?a(?:n)?g(?:h)?a?r\b", "khangar", t)
        t = t.replace("ph", "f")
        t = t.replace("oo", "u").replace("ee", "i")
        t = re.sub(r"([bcdfghjklmnpqrstvwxyz])\1+", r"\1", t)
        return t

    @staticmethod
    def _normalize_search_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        # Auto-transliterate Devanagari to Roman if present
        value = IMDBKit._devanagari_to_roman(str(value))

        value = unicodedata.normalize(
            "NFKC",
            str(value),
        ).lower()

        value = value.replace("&", " and ")
        value = value.replace("@", " at ")

        value = value.replace("'", "")
        value = value.replace("’", "")

        value = re.sub(
            r"[^a-z0-9]+",
            " ",
            value,
        )

        normalized = re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

        return IMDBKit._roman_hindi_phonetic_normalize(normalized)

    @staticmethod
    def _tokenize_search_text(
        value: Any,
    ) -> List[str]:
        normalized = (
            IMDBKit._normalize_search_text(value)
        )

        if not normalized:
            return []

        return normalized.split()

    @staticmethod
    def _extract_year_from_query(
        value: Any,
    ) -> Optional[int]:
        if not value:
            return None

        match = re.search(
            r"\b(19|20)\d{2}\b",
            str(value),
        )

        if not match:
            return None

        try:
            return int(match.group(0))
        except Exception:
            return None

    @staticmethod
    def _calculate_title_score(
        query: str,
        item: Title,
    ) -> float:
        query_normalized = (
            IMDBKit._normalize_search_text(query)
        )

        query_year = (
            IMDBKit._extract_year_from_query(
                query
            )
        )

        if query_year:
            query_normalized = re.sub(
                rf"\b{query_year}\b",
                "",
                query_normalized,
            )

            query_normalized = re.sub(
                r"\s+",
                " ",
                query_normalized,
            ).strip()

        title_normalized = (
            IMDBKit._normalize_search_text(
                item.title
            )
        )
        if not query_normalized or not title_normalized:
            return 0.0

        query_compact = query_normalized.replace(" ", "")
        title_compact = title_normalized.replace(" ", "")

        query_tokens = set(
            IMDBKit._tokenize_search_text(
                query_normalized
            )
        )

        title_tokens = set(
            IMDBKit._tokenize_search_text(
                title_normalized
            )
        )

        ratio = fuzz.ratio(
            query_normalized,
            title_normalized,
        )

        compact_ratio = fuzz.ratio(
            query_compact,
            title_compact,
        )

        partial = fuzz.partial_ratio(
            query_normalized,
            title_normalized,
        )

        token_sort = fuzz.token_sort_ratio(
            query_normalized,
            title_normalized,
        )

        token_set = fuzz.token_set_ratio(
            query_normalized,
            title_normalized,
        )

        overlap = 0.0
        if query_tokens:
            overlap = (
                len(query_tokens & title_tokens)
                / len(query_tokens)
            ) * 100.0

        length_score = 0.0
        if query_compact and title_compact:
            max_length = max(
                len(query_compact),
                len(title_compact),
            )
            difference = abs(
                len(query_compact)
                - len(title_compact)
            )
            if max_length:
                length_score = max(
                    0.0,
                    100.0
                    - (
                        difference
                        / max_length
                    )
                    * 100.0,
                )

        score = (
            (ratio * 0.23)
            + (compact_ratio * 0.22)
            + (partial * 0.12)
            + (token_sort * 0.15)
            + (token_set * 0.14)
            + (overlap * 0.08)
            + (length_score * 0.06)
        )

        if query_normalized == title_normalized:
            score += 45.0

        if query_compact == title_compact:
            score += 25.0

        if (
            len(query_normalized) >= 4
            and title_normalized.startswith(
                query_normalized
            )
        ):
            score += 28.0

        if (
            len(query_compact) >= 4
            and title_compact.startswith(
                query_compact
            )
        ):
            score += 18.0

        if (
            query_tokens
            and query_tokens.issubset(title_tokens)
        ):
            score += 22.0

        query_numbers = set(
            re.findall(
                r"\b\d+\b",
                query_normalized,
            )
        )

        title_numbers = set(
            re.findall(
                r"\b\d+\b",
                title_normalized,
            )
        )

        if query_numbers:
            if query_numbers.issubset(title_numbers):
                score += 40.0
            else:
                score -= 18.0

        if (
            query_numbers
            and not title_numbers
        ):
            score -= 30.0

        query_without_numbers = re.sub(
            r"\b\d+\b",
            "",
            query_normalized,
        ).strip()

        title_without_numbers = re.sub(
            r"\b\d+\b",
            "",
            title_normalized,
        ).strip()

        if (
            query_without_numbers
            and title_without_numbers
        ):
            base_ratio = fuzz.ratio(
                query_without_numbers,
                title_without_numbers,
            )

            if base_ratio >= 92:
                score += 18.0
            elif base_ratio >= 85:
                score += 10.0

        kind = (
            str(item.kind or "")
            .strip()
            .lower()
        )

        movie_types = {
            "movie",
            "feature",
            "film",
        }

        series_types = {
            "web series",
            "tv series",
            "tv-series",
            "series",
            "tv mini-series",
            "tv miniseries",
            "tv limited series",
            "tvseries",
            "tvminiseries",
            "mini series",
        }

        low_priority_types = {
            "short",
            "video",
            "video game",
            "videogame",
            "podcast",
            "podcastseries",
            "tv short",
            "tv movie",
            "tvmovie",
            "musicvideo",
        }

        if kind in movie_types:
            score += 12.0
        elif kind in series_types:
            score += 10.0
        elif kind in low_priority_types:
            score -= 22.0

        query_has_series_hint = any(
            word in query_tokens
            for word in {
                "series",
                "season",
                "episode",
                "tv",
            }
        )

        if query_has_series_hint:
            if kind in series_types:
                score += 20.0
            elif kind in movie_types:
                score -= 8.0

        if (
            len(query_compact) >= 5
            and len(title_compact) >= 5
        ):
            compact_difference = abs(
                len(query_compact)
                - len(title_compact)
            )

            if (
                compact_ratio >= 92
                and compact_difference <= 3
            ):
                score += 30.0
            elif (
                compact_ratio >= 88
                and compact_difference <= 3
            ):
                score += 22.0
            elif (
                compact_ratio >= 84
                and compact_difference <= 2
            ):
                score += 12.0

        return min(
            max(score, 0.0),
            180.0,
        )

    @staticmethod
    def _is_good_search_match(
        query: str,
        item: Title,
        score: float,
    ) -> bool:
        query_normalized = (
            IMDBKit._normalize_search_text(query)
        )

        query_year = (
            IMDBKit._extract_year_from_query(
                query
            )
        )

        if query_year:
            query_normalized = re.sub(
                rf"\b{query_year}\b",
                "",
                query_normalized,
            )
            query_normalized = re.sub(
                r"\s+",
                " ",
                query_normalized,
            ).strip()

        title_normalized = (
            IMDBKit._normalize_search_text(
                item.title
            )
        )

        if (
            not query_normalized
            or not title_normalized
        ):
            return False

        if query_normalized == title_normalized:
            return True

        query_tokens = set(
            IMDBKit._tokenize_search_text(
                query_normalized
            )
        )

        title_tokens = set(
            IMDBKit._tokenize_search_text(
                title_normalized
            )
        )

        if query_tokens:
            overlap = (
                len(
                    query_tokens
                    & title_tokens
                )
                / len(query_tokens)
            )

            if (
                overlap >= 0.50
                and score >= 45
            ):
                return True

        compact_query = (
            query_normalized.replace(
                " ",
                "",
            )
        )

        compact_title = (
            title_normalized.replace(
                " ",
                "",
            )
        )

        typo_ratio = fuzz.ratio(
            compact_query,
            compact_title,
        )

        if (
            typo_ratio >= 85
            and abs(
                len(compact_query)
                - len(compact_title)
            ) <= 3
        ):
            return True

        if query_year:
            return score >= 20

        return score >= 62

    # ============================================================
    # Smart Media Title Parser & Granular Media Filtering
    # ============================================================

    @staticmethod
    def clean_media_title(
        value: Any,
    ) -> str:
        if not value:
            return ""

        text = unicodedata.normalize(
            "NFKC",
            str(value),
        )

        text = re.sub(
            r"\.(mkv|mp4|avi|mov|webm|m4v)$",
            "",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"[_\.]+",
            " ",
            text,
        )

        text = re.sub(
            r"\b(?:"
            r"\d{3,4}p|"
            r"\d{3,4}x\d{3,4}|"
            r"4k|8k|"
            r"2160p|1080p|720p|480p|"
            r"10bit|8bit|"
            r"x264|x265|h264|h265|hevc|av1|"
            r"web[- ]?dl|web[- ]?rip|webrip|"
            r"blu[- ]?ray|brrip|brip|"
            r"hdrip|hdtv|dvdrip|"
            r"cam|hdcam|ts|telesync|"
            r"aac|ac3|ddp|dd5\.1|"
            r"atmos|"
            r"proper|repack|remastered|"
            r"extended|uncut|"
            r"dual[ -]?audio|multi[ -]?audio|"
            r"nf|amzn|prime|"
            r"season|episode"
            r")\b",
            " ",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\bS\d{1,3}(?:[-_ ]?S\d{1,3})?\b",
            " ",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\bS\d{1,3}E\d{1,3}\b",
            " ",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\b\d{1,2}x\d{1,3}\b",
            " ",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\b(?:19|20)\d{2}\b",
            " ",
            text,
        )

        text = re.sub(
            r"[^A-Za-z0-9\u0900-\u097F]+",
            " ",
            text,
        )

        return re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

    @staticmethod
    def parse_media_structure(
        value: Any,
    ) -> Dict[str, Any]:
        if not value:
            return {
                "title": "",
                "year": None,
                "season": None,
                "episode": None,
                "is_series": False,
            }

        text = str(value)

        year = IMDBKit._extract_year_from_query(
            text
        )

        season = None
        episode = None

        match = re.search(
            r"\bS(\d{1,3})E(\d{1,3})\b",
            text,
            flags=re.I,
        )

        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
        else:
            match = re.search(
                r"\bS(\d{1,3})\s+E(\d{1,3})\b",
                text,
                flags=re.I,
            )
            if match:
                season = int(match.group(1))
                episode = int(match.group(2))
            else:
                match = re.search(
                    r"\b(\d{1,3})x(\d{1,3})\b",
                    text,
                    flags=re.I,
                )
                if match:
                    season = int(match.group(1))
                    episode = int(match.group(2))

        if season is None:
            match = re.search(
                r"\bSeason[\s._-]*(\d{1,3})\b",
                text,
                flags=re.I,
            )
            if match:
                season = int(match.group(1))

        if season is None:
            match = re.search(
                r"\bS(\d{1,3})\b",
                text,
                flags=re.I,
            )
            if match:
                season = int(match.group(1))

        if episode is None:
            match = re.search(
                r"\b(?:Episode|EP)[\s._-]*(\d{1,3})\b",
                text,
                flags=re.I,
            )
            if match:
                episode = int(match.group(1))

        is_series = (
            season is not None
            or episode is not None
            or bool(re.search(r"\b(season|series|episode)\b", text, flags=re.I))
        )

        clean_title = (
            IMDBKit.clean_media_title(text)
        )

        return {
            "title": clean_title,
            "year": year,
            "season": season,
            "episode": episode,
            "is_series": is_series,
        }

    @staticmethod
    def _normalize_kind(
        kind: Any,
    ) -> Optional[str]:
        if not kind:
            return None

        value = str(kind).strip().lower()

        mapping = {
            "movie": "Movie",
            "feature": "Movie",
            "feature film": "Movie",
            "tvseries": "Web Series",
            "tv series": "Web Series",
            "series": "Web Series",
            "tvminiseries": "Mini Series",
            "tv mini series": "Mini Series",
            "tv mini-series": "Mini Series",
            "tv limited series": "Mini Series",
            "tvmovie": "TV Movie",
            "tv movie": "TV Movie",
            "short": "Short",
            "video": "Video",
            "video game": "Games",
            "videogame": "Games",
            "podcast": "Podcasts",
            "podcastseries": "Podcasts",
            "tvepisode": "Episode",
            "tv episode": "Episode",
            "musicvideo": "Music Video",
        }

        return mapping.get(
            value,
            value.title(),
        )

    @staticmethod
    def _filter_unwanted_kinds(kind: Optional[str]) -> bool:
        if not kind:
            return True
        k = kind.lower()
        unwanted = {
            "games", "game", "video game", "videogame",
            "podcast", "podcastseries", "podcasts",
            "short", "tv short", "video", "music video",
            "musicvideo", "episode", "tvepisode", "tv episode"
        }
        return k not in unwanted

    @staticmethod
    def _extract_name(
        value: Any,
    ) -> str:
        if isinstance(value, str):
            return value

        if isinstance(value, dict):
            return (
                value.get("name")
                or value.get("original_name")
                or value.get("title")
                or value.get("original_title")
                or ""
            )

        return str(value)

    @staticmethod
    def _extract_names(
        values: Any,
    ) -> List[str]:
        if not values:
            return []

        if not isinstance(values, list):
            values = [values]

        result = []

        for value in values:
            name = IMDBKit._extract_name(value)

            if name:
                result.append(name)

        return result

    # ============================================================
    # IMDb Search
    # ============================================================

    def search_movie(
        self,
        title: str,
        results: int = 10,
    ) -> SearchResult:
        """
        Search IMDb titles with Devanagari auto-transliteration, Roman-Hindi correction,
        sequel matching, year tracking, and filtering of non-media types.
        """

        if not title:
            return SearchResult([])

        title = str(title).strip()

        if not title:
            return SearchResult([])

        try:
            results = int(results)
        except Exception:
            results = 10

        results = max(
            1,
            min(results, 10),
        )

        encoded = urllib.parse.quote(
            title,
            safe="",
        )

        url = (
            f"{self.IMDb_SUGGESTION_URL}"
            f"{encoded}.json"
        )

        data = self._cached_request_json(url, cache_tier="imdb")

        raw_results = data.get("d", []) if data else []
        candidates = []
        seen_ids = set()

        query_year = (
            self._extract_year_from_query(
                title
            )
        )

        for item in raw_results:
            if not isinstance(
                item,
                dict,
            ):
                continue

            imdb_id = self._normalize_imdb_id(
                item.get("id")
            )

            if (
                not imdb_id
                or not imdb_id.startswith("tt")
            ):
                continue

            if imdb_id in seen_ids:
                continue

            item_title = (
                item.get("l")
                or item.get("title")
                or ""
            )

            if not item_title:
                continue

            year = self._safe_int(
                item.get("y")
                or item.get("year")
            )

            kind = self._normalize_kind(
                item.get("q")
                or item.get("kind")
            )

            if not self._filter_unwanted_kinds(kind):
                continue

            image_url = (
                item.get(
                    "i",
                    {},
                ).get("imageUrl")
                if isinstance(
                    item.get("i"),
                    dict,
                )
                else None
            )

            result = Title(
                imdb_id=imdb_id,
                title=item_title,
                year=year,
                kind=kind,
                image_url=image_url,
            )

            score = self._calculate_title_score(
                title,
                result,
            )

            if not self._is_good_search_match(
                title,
                result,
                score,
            ):
                continue

            if (
                query_year
                and year
                and year == query_year
            ):
                score += 25.0

            seen_ids.add(imdb_id)

            candidates.append(
                (
                    score,
                    result,
                )
            )

        candidates.sort(
            key=lambda pair: (
                pair[0],
                pair[1].year or 0,
            ),
            reverse=True,
        )

        titles = [
            item
            for _, item in candidates[:results]
        ]

        return SearchResult(titles)

    # ============================================================
    # IMDb ID Search
    # ============================================================

    def _find_imdb_title(
        self,
        imdb_id: str,
    ) -> Optional[Title]:

        imdb_id = self._normalize_imdb_id(
            imdb_id
        )

        if not imdb_id:
            return None

        url = f"{self.IMDb_SUGGESTION_URL}{imdb_id}.json"
        data = self._cached_request_json(url, cache_tier="imdb_id")

        if not data:
            return None

        for item in data.get(
            "d",
            [],
        ):

            if not isinstance(
                item,
                dict,
            ):
                continue

            item_id = self._normalize_imdb_id(
                item.get("id")
            )

            if item_id != imdb_id:
                continue

            return Title(
                imdb_id=imdb_id,
                title=item.get("l") or "",
                year=self._safe_int(
                    item.get("y")
                ),
                kind=self._normalize_kind(
                    item.get("q")
                ),
                image_url=(
                    item.get(
                        "i",
                        {},
                    ).get("imageUrl")
                    if isinstance(
                        item.get("i"),
                        dict,
                    )
                    else None
                ),
            )

        return None

    # ============================================================
    # TMDB (with Cache & Coalescing)
    # ============================================================

    def _tmdb_find(
        self,
        imdb_id: str,
    ) -> Optional[Dict[str, Any]]:

        if not self.tmdb_api_key:
            logger.warning(
                "IMDBKit: TMDB lookup skipped because "
                "API key is missing."
            )
            return None

        url = f"{self.TMDB_BASE_URL}/find/{imdb_id}"
        params = {
            "api_key": self.tmdb_api_key,
            "external_source": "imdb_id",
        }
        
        cache_key = url + "?" + urllib.parse.urlencode(sorted(params.items()))
        found, val = self._tmdb_cache.get(cache_key)
        if found:
            return val

        def fetch():
            res = self._request_json(url, params)
            self._tmdb_cache.set(cache_key, res)
            return res

        return self._coalescer.execute(cache_key, fetch)

    def _tmdb_details(
        self,
        media_type: str,
        tmdb_id: Any,
    ) -> Optional[Dict[str, Any]]:

        if not self.tmdb_api_key:
            return None

        if not tmdb_id:
            return None

        url = f"{self.TMDB_BASE_URL}/{media_type}/{tmdb_id}"
        params = {
            "api_key": self.tmdb_api_key,
            "append_to_response": (
                "credits,external_ids,"
                "alternative_titles"
            ),
        }

        cache_key = url + "?" + urllib.parse.urlencode(sorted(params.items()))
        found, val = self._tmdb_cache.get(cache_key)
        if found:
            return val

        def fetch():
            res = self._request_json(url, params)
            self._tmdb_cache.set(cache_key, res)
            return res

        return self._coalescer.execute(cache_key, fetch)

    # ============================================================
    # Movie Builder
    # ============================================================

    def _build_movie(
        self,
        imdb_id: str,
        imdb_title: Optional[Title],
        tmdb_data: Optional[Dict[str, Any]],
        media_type: Optional[str],
    ) -> Movie:

        title = (
            imdb_title.title
            if imdb_title
            else None
        )

        year = (
            imdb_title.year
            if imdb_title
            else None
        )

        kind = (
            imdb_title.kind
            if imdb_title
            else None
        )

        release_date = None
        plot = None
        rating = None
        votes = None
        runtime = None
        poster_url = None

        countries = []
        languages = []
        genres = []
        cast = []
        directors = []
        writers = []
        producers = []
        composers = []
        cinematographers = []
        distributors = []

        title_akas = []
        seasons = []

        box_office = None

        localized_title = None
        original_title = None

        if tmdb_data:
            title = (
                tmdb_data.get("title")
                or tmdb_data.get("name")
                or title
            )

            original_title = (
                tmdb_data.get("original_title")
                or tmdb_data.get("original_name")
            )

            release_date = (
                tmdb_data.get("release_date")
                or tmdb_data.get("first_air_date")
            )

            year = (
                self._year_from_date(
                    release_date
                )
                or year
            )

            plot = (
                tmdb_data.get("overview")
                or None
            )

            rating = self._safe_float(
                tmdb_data.get(
                    "vote_average"
                )
            )

            votes = self._safe_int(
                tmdb_data.get(
                    "vote_count"
                )
            )

            runtime = tmdb_data.get(
                "runtime"
            )

            if not runtime:
                episode_runtime = (
                    tmdb_data.get(
                        "episode_run_time"
                    )
                )

                if (
                    isinstance(
                        episode_runtime,
                        list,
                    )
                    and episode_runtime
                ):
                    runtime = episode_runtime[0]

            poster_path = tmdb_data.get(
                "poster_path"
            )

            if poster_path:
                poster_url = (
                    "https://image.tmdb.org/t/p/w1280"
                    + poster_path
                )

            countries = [
                item.get("name")
                for item in tmdb_data.get(
                    "production_countries",
                    [],
                )
                if isinstance(
                    item,
                    dict,
                )
                and item.get("name")
            ]

            languages = [
                (
                    item.get("english_name")
                    or item.get("name")
                    or item.get("iso_639_1")
                )
                for item in tmdb_data.get(
                    "spoken_languages",
                    [],
                )
                if isinstance(
                    item,
                    dict,
                )
            ]

            genres = [
                item.get("name")
                for item in tmdb_data.get(
                    "genres",
                    [],
                )
                if isinstance(
                    item,
                    dict,
                )
                and item.get("name")
            ]

            alternative_titles = tmdb_data.get(
                "alternative_titles"
            )

            if isinstance(
                alternative_titles,
                dict,
            ):
                alternative_title_list = (
                    alternative_titles.get(
                        "titles",
                        [],
                    )
                )

                if isinstance(
                    alternative_title_list,
                    list,
                ):
                    for item in alternative_title_list:
                        if not isinstance(
                            item,
                            dict,
                        ):
                            continue

                        value = (
                            item.get("title")
                            or item.get("name")
                        )

                        if value:
                            title_akas.append(
                                value
                            )

            credits = tmdb_data.get(
                "credits",
                {},
            )

            if isinstance(
                credits,
                dict,
            ):
                for person in credits.get(
                    "cast",
                    [],
                )[:20]:
                    if not isinstance(
                        person,
                        dict,
                    ):
                        continue

                    name = person.get(
                        "name"
                    )

                    if name:
                        cast.append(name)

                for person in credits.get(
                    "crew",
                    [],
                ):
                    if not isinstance(
                        person,
                        dict,
                    ):
                        continue

                    name = person.get(
                        "name"
                    )

                    if not name:
                        continue

                    department = (
                        person.get(
                            "department"
                        )
                        or ""
                    ).lower()

                    job = (
                        person.get(
                            "job"
                        )
                        or ""
                    ).lower()

                    if (
                        department == "directing"
                        and job == "director"
                    ):
                        directors.append(name)
                    elif (
                        department == "writing"
                        or "writer" in job
                    ):
                        writers.append(name)
                    elif (
                        department == "production"
                        and job in {
                            "producer",
                            "executive producer",
                        }
                    ):
                        producers.append(name)
                    elif department == "sound":
                        composers.append(name)
                    elif department == "camera":
                        cinematographers.append(
                            name
                        )

            if media_type == "tv":
                kind = "Web Series"

                for season in tmdb_data.get(
                    "seasons",
                    [],
                ):
                    if not isinstance(
                        season,
                        dict,
                    ):
                        continue

                    season_number = season.get(
                        "season_number"
                    )

                    if season_number is None:
                        continue

                    seasons.append(
                        {
                            "season": season_number,
                            "name": (
                                season.get("name")
                                or (
                                    f"Season "
                                    f"{season_number}"
                                )
                            ),
                            "episodes": season.get(
                                "episode_count"
                            ),
                            "air_date": season.get(
                                "air_date"
                            ),
                        }
                    )

            elif media_type == "movie":
                kind = "Movie"

                box_office = (
                    tmdb_data.get(
                        "revenue"
                    )
                    or None
                )

        if imdb_title:
            if not poster_url:
                poster_url = (
                    imdb_title.image_url
                )

            if not title:
                title = imdb_title.title

            if not year:
                year = imdb_title.year

            if not kind:
                kind = imdb_title.kind

        imdb_url = (
            f"https://www.imdb.com/title/"
            f"{imdb_id}/"
            if imdb_id
            else None
        )

        info_series = SeriesInfo(
            display_seasons=seasons
        )

        return Movie(
            release_date=release_date,
            year=year,
            plot=plot,
            imdb_id=imdb_id,
            title=title,
            votes=votes,
            title_akas=title_akas,
            info_series=info_series,
            worldwide_gross=box_office,
            title_localized=localized_title,
            kind=kind,
            stars=cast,
            duration=runtime,
            countries=countries,
            certificates=[],
            languages=languages,
            directors=directors,
            writers=writers,
            producers=producers,
            composers=composers,
            cinematographers=cinematographers,
            music_team=[],
            distributors=distributors,
            genres=genres,
            rating=rating,
            cover_url=poster_url,
            url=imdb_url,
            original_title=original_title,
        )

    # ============================================================
    # Get Movie
    # ============================================================

    def get_movie(
        self,
        movie_id: Any,
    ) -> Movie:
        """
        Fetch complete movie/series metadata with preserved existing compatibility.
        """

        imdb_id = self._normalize_imdb_id(
            movie_id
        )

        if not imdb_id:
            return Movie()

        imdb_title = self._find_imdb_title(
            imdb_id
        )

        tmdb_find = self._tmdb_find(
            imdb_id
        )

        tmdb_data = None
        media_type = None

        if tmdb_find:
            movies = tmdb_find.get(
                "movie_results",
                [],
            )

            tv_results = tmdb_find.get(
                "tv_results",
                [],
            )

            if movies:
                media_type = "movie"

                tmdb_id = movies[0].get(
                    "id"
                )

                tmdb_data = self._tmdb_details(
                    "movie",
                    tmdb_id,
                )

            elif tv_results:
                media_type = "tv"

                tmdb_id = tv_results[0].get(
                    "id"
                )

                tmdb_data = self._tmdb_details(
                    "tv",
                    tmdb_id,
                )

        return self._build_movie(
            imdb_id=imdb_id,
            imdb_title=imdb_title,
            tmdb_data=tmdb_data,
            media_type=media_type,
        )

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

    # ============================================================
    # Convenience
    # ============================================================

    def get_movie_details(
        self,
        movie_id: Any,
    ) -> Dict[str, Any]:
        movie = self.get_movie(
            movie_id
        )

        return movie.to_dict()

    def search(
        self,
        title: str,
        results: int = 10,
    ) -> SearchResult:
        return self.search_movie(
            title,
            results=results,
        )
