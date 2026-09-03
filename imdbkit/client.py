import json
import logging
import os
import re
import socket
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from .models import Movie, SearchResult, SeriesInfo, Title


logger = logging.getLogger(__name__)


class IMDBKit:
    """
    Lightweight IMDb + TMDB metadata client.

    Compatible with:

        from imdbkit import IMDBKit

        imdb = IMDBKit()

        result = imdb.search_movie("interstellar")
        movie = imdb.get_movie("tt0816692")
    """

    IMDb_SUGGESTION_URL = (
        "https://v3.sg.media-imdb.com/suggestion/x/"
    )

    TMDB_BASE_URL = (
        "https://api.themoviedb.org/3"
    )

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
            logger.info(
                "IMDBKit: TMDB API key loaded successfully."
            )
        else:
            logger.warning(
                "IMDBKit: TMDB API key not configured. "
                "Using IMDb fallback only."
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

                separator = (
                    "&"
                    if "?" in url
                    else "?"
                )

                url = (
                    f"{url}"
                    f"{separator}"
                    f"{query}"
                )

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
                data = (
                    response
                    .read()
                    .decode("utf-8")
                )

            return json.loads(data)

        except urllib.error.HTTPError as exc:
            logger.error(
                "IMDBKit HTTP error %s for %s",
                exc.code,
                url.split("?")[0],
            )

            try:
                error_body = (
                    exc
                    .read()
                    .decode("utf-8")
                )

                logger.error(
                    "IMDBKit HTTP response: %s",
                    error_body[:500],
                )

            except Exception:
                pass

            return None

        except (
            urllib.error.URLError,
            socket.timeout,
            TimeoutError,
        ) as exc:
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

    # ============================================================
    # Helpers
    # ============================================================

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

        return (
            str(value).strip()
            or None
        )

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
                return int(
                    match.group(0)
                )
            except Exception:
                pass

        return None

    @staticmethod
    def _normalize_search_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        value = unicodedata.normalize(
            "NFKC",
            str(value),
        ).lower()

        # Common title separators.
        value = value.replace(
            "&",
            " and ",
        )

        value = value.replace(
            "@",
            " at ",
        )

        # Remove apostrophes without
        # splitting words.
        value = value.replace(
            "'",
            "",
        )

        value = value.replace(
            "’",
            "",
        )

        # Convert punctuation and separators
        # to spaces.
        value = re.sub(
            r"[^a-z0-9]+",
            " ",
            value,
        )

        return re.sub(
            r"\s+",
            " ",
            value,
        ).strip()

    @staticmethod
    def _tokenize_search_text(
        value: Any,
    ) -> List[str]:
        normalized = (
            IMDBKit._normalize_search_text(
                value
            )
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
            return int(
                match.group(0)
            )
        except Exception:
            return None

    # ============================================================
    # TITLE SCORING
    # ============================================================

    @staticmethod
    def _calculate_title_score(
        query: str,
        item: Title,
    ) -> float:
        """
        Calculate a strong fuzzy title relevance score.

        Features:
            - Year-aware title matching
            - Exact title matching
            - Compact title matching
            - Fuzzy character matching
            - Fuzzy token matching
            - Token overlap
            - Sequel/number awareness
            - Movie/series relevance
            - Low-priority IMDb type penalty
            - Strong typo tolerance

        Release year is intentionally removed from
        title similarity calculations.

        The year is handled separately during
        result ranking.
        """

        query_normalized = (
            IMDBKit._normalize_search_text(
                query
            )
        )

        query_year = (
            IMDBKit._extract_year_from_query(
                query
            )
        )

        # --------------------------------------------------------
        # REMOVE RELEASE YEAR FROM TITLE MATCHING
        # --------------------------------------------------------

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
            return 0.0

        query_compact = (
            query_normalized.replace(
                " ",
                "",
            )
        )

        title_compact = (
            title_normalized.replace(
                " ",
                "",
            )
        )

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

        # --------------------------------------------------------
        # BASIC FUZZY SCORES
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # EXACT TOKEN OVERLAP
        # --------------------------------------------------------

        overlap = 0.0

        if query_tokens:
            overlap = (
                len(
                    query_tokens
                    & title_tokens
                )
                / len(query_tokens)
            ) * 100.0

        # --------------------------------------------------------
        # FUZZY TOKEN MATCHING
        # --------------------------------------------------------

        fuzzy_token_matches = 0

        if query_tokens and title_tokens:
            for query_token in query_tokens:
                best_token_ratio = 0.0

                for title_token in title_tokens:

                    # Avoid matching tiny words
                    # against unrelated long words.
                    if (
                        len(query_token) <= 2
                        or len(title_token) <= 2
                    ):
                        if query_token != title_token:
                            continue

                    token_ratio = fuzz.ratio(
                        query_token,
                        title_token,
                    )

                    if (
                        token_ratio
                        > best_token_ratio
                    ):
                        best_token_ratio = (
                            token_ratio
                        )

                if best_token_ratio >= 86:
                    fuzzy_token_matches += 1

        if query_tokens:
            fuzzy_token_coverage = (
                fuzzy_token_matches
                / len(query_tokens)
            ) * 100.0
        else:
            fuzzy_token_coverage = 0.0

        # --------------------------------------------------------
        # LENGTH SIMILARITY
        # --------------------------------------------------------

        length_score = 0.0

        if (
            query_compact
            and title_compact
        ):
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

        # --------------------------------------------------------
        # BASE SCORE
        # --------------------------------------------------------

        score = (
            (ratio * 0.20)
            + (compact_ratio * 0.20)
            + (partial * 0.12)
            + (token_sort * 0.14)
            + (token_set * 0.12)
            + (overlap * 0.06)
            + (length_score * 0.06)
            + (
                fuzzy_token_coverage
                * 0.10
            )
        )

        # --------------------------------------------------------
        # EXACT MATCH BONUSES
        # --------------------------------------------------------

        if (
            query_normalized
            == title_normalized
        ):
            score += 45.0

        if (
            query_compact
            == title_compact
        ):
            score += 25.0

        # --------------------------------------------------------
        # PREFIX MATCH
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # QUERY TOKENS FULLY PRESENT
        # --------------------------------------------------------

        if (
            query_tokens
            and query_tokens.issubset(
                title_tokens
            )
        ):
            score += 22.0

        # --------------------------------------------------------
        # NUMBER / SEQUEL AWARENESS
        # --------------------------------------------------------

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
            if query_numbers.issubset(
                title_numbers
            ):
                score += 40.0
            else:
                score -= 18.0

        if (
            query_numbers
            and not title_numbers
        ):
            score -= 30.0

        # --------------------------------------------------------
        # BASE TITLE MATCH WITHOUT NUMBERS
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # MEDIA TYPE RELEVANCE
        # --------------------------------------------------------

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
            "tv series",
            "tv-series",
            "series",
            "tv mini-series",
            "tv miniseries",
            "tvminiseries",
            "tv mini series",
            "tv limited series",
            "tv limited-series",
            "tvseries",
        }

        low_priority_types = {
            "short",
            "video",
            "video game",
            "videogame",
            "podcast",
            "podcastseries",
            "podcast series",
            "tv short",
            "tv movie",
            "tvmovie",
            "tv episode",
            "tvepisode",
            "episode",
        }

        if kind in movie_types:
            score += 18.0

        elif kind in series_types:
            score += 10.0

        elif kind in low_priority_types:
            score -= 28.0

        # --------------------------------------------------------
        # SERIES QUERY HINT
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # STRONG CLOSE MATCH
        # --------------------------------------------------------

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

    # ============================================================
    # SEARCH MATCH VALIDATION
    # ============================================================

    @staticmethod
    def _is_good_search_match(
        query: str,
        item: Title,
        score: float,
    ) -> bool:
        query_normalized = (
            IMDBKit._normalize_search_text(
                query
            )
        )

        query_year = (
            IMDBKit._extract_year_from_query(
                query
            )
        )

        # Remove year from title relevance
        # checking.
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

        # Exact normalized title.
        if (
            query_normalized
            == title_normalized
        ):
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

        # --------------------------------------------------------
        # EXACT TOKEN OVERLAP
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # COMPACT TYPO MATCH
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # YEAR-AWARE VALIDATION
        # --------------------------------------------------------

        if query_year:
            # Because the year has already been
            # removed from title matching, allow
            # a lower score than normal searches,
            # but don't make it excessively loose.
            if score >= 28:
                return True

            # Strong fuzzy token match for short
            # typo queries such as:
            #
            #   godfater 1972
            #   harry poter 2001
            #   spideman 2002
            #
            if (
                query_tokens
                and title_tokens
            ):
                fuzzy_matches = 0

                for query_token in query_tokens:
                    best_ratio = 0.0

                    for title_token in title_tokens:

                        if (
                            len(query_token) <= 2
                            or len(title_token) <= 2
                        ):
                            if query_token != title_token:
                                continue

                        token_ratio = fuzz.ratio(
                            query_token,
                            title_token,
                        )

                        best_ratio = max(
                            best_ratio,
                            token_ratio,
                        )

                    if best_ratio >= 86:
                        fuzzy_matches += 1

                if (
                    fuzzy_matches >= 1
                    and len(query_tokens) <= 2
                    and score >= 22
                ):
                    return True

            return False

        # Normal search threshold.
        return score >= 62

    # ============================================================
    # MEDIA TITLE CLEANING
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

        # File extensions.
        text = re.sub(
            r"\.(mkv|mp4|avi|mov|webm|m4v)$",
            "",
            text,
            flags=re.I,
        )

        # Common filename separators.
        text = re.sub(
            r"[_\.]+",
            " ",
            text,
        )

        # Technical metadata.
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

        # Season packs.
        text = re.sub(
            r"\bS\d{1,3}"
            r"(?:[-_ ]?S\d{1,3})?\b",
            " ",
            text,
            flags=re.I,
        )

        # S01E01.
        text = re.sub(
            r"\bS\d{1,3}E\d{1,3}\b",
            " ",
            text,
            flags=re.I,
        )

        # 1x01.
        text = re.sub(
            r"\b\d{1,2}x\d{1,3}\b",
            " ",
            text,
            flags=re.I,
        )

        # Release years.
        text = re.sub(
            r"\b(?:19|20)\d{2}\b",
            " ",
            text,
        )

        # Remaining punctuation.
        text = re.sub(
            r"[^A-Za-z0-9]+",
            " ",
            text,
        )

        return re.sub(
            r"\s+",
            " ",
            text,
        ).strip()

    # ============================================================
    # MEDIA STRUCTURE
    # ============================================================

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

        year = (
            IMDBKit._extract_year_from_query(
                text
            )
        )

        season = None
        episode = None

        # S01E02.
        match = re.search(
            r"\bS(\d{1,3})E(\d{1,3})\b",
            text,
            flags=re.I,
        )

        if match:
            season = int(
                match.group(1)
            )

            episode = int(
                match.group(2)
            )

        else:
            # 1x02.
            match = re.search(
                r"\b(\d{1,3})x(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(
                    match.group(1)
                )

                episode = int(
                    match.group(2)
                )

        # Season 1.
        if season is None:
            match = re.search(
                r"\bSeason[\s._-]*"
                r"(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(
                    match.group(1)
                )

        # S1.
        if season is None:
            match = re.search(
                r"\bS(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(
                    match.group(1)
                )

        is_series = (
            season is not None
            or episode is not None
        )

        clean_title = (
            IMDBKit.clean_media_title(
                text
            )
        )

        return {
            "title": clean_title,
            "year": year,
            "season": season,
            "episode": episode,
            "is_series": is_series,
        }

    # ============================================================
    # KIND NORMALIZATION
    # ============================================================

    @staticmethod
    def _normalize_kind(
        kind: Any,
    ) -> Optional[str]:
        if not kind:
            return None

        value = (
            str(kind)
            .strip()
            .lower()
        )

        mapping = {
            "movie": "movie",
            "feature": "movie",
            "feature film": "movie",

            "tvseries": "tv series",
            "tv series": "tv series",
            "series": "tv series",

            "tvminiseries": "tvMiniSeries",
            "tv mini series": "tvMiniSeries",
            "tv mini-series": "tvMiniSeries",

            "tv movie": "tvMovie",
            "tvmovie": "tvMovie",

            "short": "short",
            "video": "video",
            "video game": "video game",
            "videogame": "video game",

            "podcast": "podcast",
            "podcastseries": "podcastseries",
            "podcast series": "podcastseries",

            "tv short": "tv short",

            "tv episode": "tvEpisode",
            "tvepisode": "tvEpisode",

            "episode": "tvEpisode",
        }

        return mapping.get(
            value,
            value,
        )

    # ============================================================
    # NAME HELPERS
    # ============================================================

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

        if not isinstance(
            values,
            list,
        ):
            values = [values]

        result = []

        for value in values:
            name = (
                IMDBKit._extract_name(
                    value
                )
            )

            if name:
                result.append(name)

        return result

    # ============================================================
    # SEARCH
    # ============================================================

    def search_movie(
        self,
        title: str,
        results: int = 10,
    ) -> SearchResult:
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

        data = self._request_json(
            url
        )

        if not data:
            return SearchResult([])

        raw_results = data.get(
            "d",
            [],
        )

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

            imdb_id = (
                self._normalize_imdb_id(
                    item.get("id")
                )
            )

            if (
                not imdb_id
                or not imdb_id.startswith(
                    "tt"
                )
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

            image_url = (
                item.get(
                    "i",
                    {},
                ).get(
                    "imageUrl"
                )
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

            # ----------------------------------------------------
            # TITLE SCORE
            # ----------------------------------------------------

            score = (
                self._calculate_title_score(
                    title,
                    result,
                )
            )

            # ----------------------------------------------------
            # MATCH VALIDATION
            # ----------------------------------------------------

            if not self._is_good_search_match(
                title,
                result,
                score,
            ):
                continue

            # ----------------------------------------------------
            # YEAR RANKING
            # ----------------------------------------------------

            if (
                query_year
                and year
                and year == query_year
            ):
                # Strong exact-year bonus.
                score += 35.0

            elif (
                query_year
                and year
                and year != query_year
            ):
                # Penalize wrong-year results,
                # but don't completely discard them.
                score -= 12.0

            # ----------------------------------------------------
            # FINAL TYPE INFORMATION
            # ----------------------------------------------------

            normalized_kind = (
                str(kind or "")
                .strip()
                .lower()
            )

            if normalized_kind in {
                "movie",
                "feature",
                "film",
            }:
                score += 3.0

            # Keep only first occurrence.
            seen_ids.add(imdb_id)

            candidates.append(
                (
                    score,
                    result,
                )
            )

        # --------------------------------------------------------
        # RESULT SORTING
        # --------------------------------------------------------

        def result_sort_key(
            pair,
        ):
            score, item = pair

            kind = (
                str(item.kind or "")
                .strip()
                .lower()
            )

            exact_year = (
                query_year is not None
                and item.year == query_year
            )

            is_movie = (
                kind in {
                    "movie",
                    "feature",
                    "film",
                }
            )

            is_series = (
                kind in {
                    "tv series",
                    "tv-series",
                    "series",
                    "tvminiSeries",
                    "tv miniseries",
                    "tvminiseries",
                    "tv mini-series",
                    "tv mini series",
                    "tvseries",
                }
            )

            is_low_priority = (
                kind in {
                    "short",
                    "video",
                    "video game",
                    "videogame",
                    "podcast",
                    "podcastseries",
                    "podcast series",
                    "tv short",
                    "tv movie",
                    "tvmovie",
                    "tv episode",
                    "tvepisode",
                    "episode",
                }
            )

            # Exact year is strongest only when
            # the user explicitly provided a year.
            if query_year is None:
                exact_year = False

            return (
                exact_year,
                is_movie,
                not is_low_priority,
                is_series,
                score,
                item.year or 0,
            )

        candidates.sort(
            key=result_sort_key,
            reverse=True,
        )

        titles = [
            item
            for _, item in candidates[:results]
        ]

        return SearchResult(
            titles
        )

    # ============================================================
    # FIND IMDb TITLE
    # ============================================================

    def _find_imdb_title(
        self,
        imdb_id: str,
    ) -> Optional[Title]:

        imdb_id = (
            self._normalize_imdb_id(
                imdb_id
            )
        )

        if not imdb_id:
            return None

        data = self._request_json(
            f"{self.IMDb_SUGGESTION_URL}"
            f"{imdb_id}.json"
        )

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

            item_id = (
                self._normalize_imdb_id(
                    item.get("id")
                )
            )

            if item_id != imdb_id:
                continue

            return Title(
                imdb_id=imdb_id,
                title=(
                    item.get("l")
                    or ""
                ),
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
                    ).get(
                        "imageUrl"
                    )
                    if isinstance(
                        item.get("i"),
                        dict,
                    )
                    else None
                ),
            )

        return None

    # ============================================================
    # TMDB
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

        return self._request_json(
            f"{self.TMDB_BASE_URL}"
            f"/find/{imdb_id}",
            {
                "api_key": self.tmdb_api_key,
                "external_source": "imdb_id",
            },
        )

    def _tmdb_details(
        self,
        media_type: str,
        tmdb_id: Any,
    ) -> Optional[Dict[str, Any]]:

        if not self.tmdb_api_key:
            return None

        if not tmdb_id:
            return None

        return self._request_json(
            f"{self.TMDB_BASE_URL}"
            f"/{media_type}/{tmdb_id}",
            {
                "api_key": self.tmdb_api_key,
                "append_to_response": (
                    "credits,external_ids,"
                    "alternative_titles"
                ),
            },
        )

    # ============================================================
    # BUILD MOVIE
    # ============================================================

    def _build_movie(
        self,
        imdb_id: str,
        imdb_title: Optional[Title],
        tmdb_data: Optional[
            Dict[str, Any]
        ],
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

        # ========================================================
        # TMDB DATA
        # ========================================================

        if tmdb_data:

            title = (
                tmdb_data.get("title")
                or tmdb_data.get("name")
                or title
            )

            original_title = (
                tmdb_data.get(
                    "original_title"
                )
                or tmdb_data.get(
                    "original_name"
                )
            )

            release_date = (
                tmdb_data.get(
                    "release_date"
                )
                or tmdb_data.get(
                    "first_air_date"
                )
            )

            year = (
                self._year_from_date(
                    release_date
                )
                or year
            )

            plot = (
                tmdb_data.get(
                    "overview"
                )
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
                    runtime = (
                        episode_runtime[0]
                    )

            poster_path = (
                tmdb_data.get(
                    "poster_path"
                )
            )

            if poster_path:
                poster_url = (
                    "https://image.tmdb.org/t/p/w1280"
                    + poster_path
                )

            # ----------------------------------------------------
            # COUNTRIES
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # LANGUAGES
            # ----------------------------------------------------

            languages = [
                (
                    item.get(
                        "english_name"
                    )
                    or item.get(
                        "name"
                    )
                    or item.get(
                        "iso_639_1"
                    )
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

            # ----------------------------------------------------
            # GENRES
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # ALTERNATIVE TITLES
            # ----------------------------------------------------

            alternative_titles = (
                tmdb_data.get(
                    "alternative_titles"
                )
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
                    for item in (
                        alternative_title_list
                    ):

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

            # ----------------------------------------------------
            # CREDITS
            # ----------------------------------------------------

            credits = tmdb_data.get(
                "credits",
                {},
            )

            if isinstance(
                credits,
                dict,
            ):

                # Cast.
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
                        cast.append(
                            name
                        )

                # Crew.
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

                    # Directors.
                    if (
                        department
                        == "directing"
                        and job
                        == "director"
                    ):
                        directors.append(
                            name
                        )

                    # Writers.
                    elif (
                        department
                        == "writing"
                        or "writer" in job
                    ):
                        writers.append(
                            name
                        )

                    # Producers.
                    elif (
                        department
                        == "production"
                        and job in {
                            "producer",
                            "executive producer",
                        }
                    ):
                        producers.append(
                            name
                        )

                    # Sound / composers.
                    elif (
                        department
                        == "sound"
                    ):
                        composers.append(
                            name
                        )

                    # Camera.
                    elif (
                        department
                        == "camera"
                    ):
                        cinematographers.append(
                            name
                        )

            # ----------------------------------------------------
            # TV SERIES
            # ----------------------------------------------------

            if media_type == "tv":
                kind = "tv series"

                for season in tmdb_data.get(
                    "seasons",
                    [],
                ):

                    if not isinstance(
                        season,
                        dict,
                    ):
                        continue

                    season_number = (
                        season.get(
                            "season_number"
                        )
                    )

                    if (
                        season_number
                        is None
                    ):
                        continue

                    seasons.append(
                        {
                            "season": (
                                season_number
                            ),
                            "name": (
                                season.get(
                                    "name"
                                )
                                or (
                                    f"Season "
                                    f"{season_number}"
                                )
                            ),
                            "episodes": (
                                season.get(
                                    "episode_count"
                                )
                            ),
                            "air_date": (
                                season.get(
                                    "air_date"
                                )
                            ),
                        }
                    )

            # ----------------------------------------------------
            # MOVIE
            # ----------------------------------------------------

            elif media_type == "movie":
                kind = "movie"

                box_office = (
                    tmdb_data.get(
                        "revenue"
                    )
                    or None
                )

        # ========================================================
        # IMDb FALLBACK
        # ========================================================

        if imdb_title:

            if not poster_url:
                poster_url = (
                    imdb_title.image_url
                )

            if not title:
                title = (
                    imdb_title.title
                )

            if not year:
                year = (
                    imdb_title.year
                )

            if not kind:
                kind = (
                    imdb_title.kind
                )

        # ========================================================
        # IMDb URL
        # ========================================================

        imdb_url = (
            f"https://www.imdb.com/title/"
            f"{imdb_id}/"
            if imdb_id
            else None
        )

        # ========================================================
        # SERIES INFO
        # ========================================================

        info_series = SeriesInfo(
            display_seasons=seasons
        )

        # ========================================================
        # MOVIE OBJECT
        # ========================================================

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
    # GET MOVIE
    # ============================================================

    def get_movie(
        self,
        movie_id: Any,
    ) -> Movie:

        imdb_id = (
            self._normalize_imdb_id(
                movie_id
            )
        )

        if not imdb_id:
            return Movie()

        # IMDb basic information.
        imdb_title = (
            self._find_imdb_title(
                imdb_id
            )
        )

        # TMDB IMDb lookup.
        tmdb_find = (
            self._tmdb_find(
                imdb_id
            )
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

            # Prefer movie.
            if movies:

                media_type = "movie"

                tmdb_id = (
                    movies[0].get(
                        "id"
                    )
                )

                tmdb_data = (
                    self._tmdb_details(
                        "movie",
                        tmdb_id,
                    )
                )

            elif tv_results:

                media_type = "tv"

                tmdb_id = (
                    tv_results[0].get(
                        "id"
                    )
                )

                tmdb_data = (
                    self._tmdb_details(
                        "tv",
                        tmdb_id,
                    )
                )

        return self._build_movie(
            imdb_id=imdb_id,
            imdb_title=imdb_title,
            tmdb_data=tmdb_data,
            media_type=media_type,
        )

    # ============================================================
    # UPDATE
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
    # MOVIE DETAILS
    # ============================================================

    def get_movie_details(
        self,
        movie_id: Any,
    ) -> Dict[str, Any]:

        movie = self.get_movie(
            movie_id
        )

        return movie.to_dict()

    # ============================================================
    # GENERIC SEARCH
    # ============================================================

    def search(
        self,
        title: str,
        results: int = 10,
    ) -> SearchResult:
        return self.search_movie(
            title,
            results=results,
        )
