import json
import logging
import os
import re
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

    IMDb_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/x/"
    TMDB_BASE_URL = "https://api.themoviedb.org/3"

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

        except urllib.error.URLError as exc:
            logger.error(
                "IMDBKit network error for %s: %s",
                url.split("?")[0],
                exc.reason,
            )
            return None

        except TimeoutError:
            logger.error(
                "IMDBKit request timed out: %s",
                url.split("?")[0],
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
    def _normalize_search_text(
        value: Any,
    ) -> str:
        if value is None:
            return ""

        value = unicodedata.normalize(
            "NFKC",
            str(value),
        ).lower()

        # Common title separators
        value = value.replace("&", " and ")
        value = value.replace("@", " at ")

        # Remove apostrophes without splitting words
        value = value.replace("'", "")
        value = value.replace("’", "")

        # Convert all punctuation/separators to spaces
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
        """
        Advanced title relevance scoring.

        Handles:
        - Exact title matching
        - Spelling mistakes
        - Missing/extra letters
        - Joined/separated words
        - Token matching
        - Prefix/phrase matching
        - Sequel numbers
        - Movie/TV relevance
        - Future/upcoming titles
        - Short/video/game/podcast suppression
        """

        query_normalized = (
            IMDBKit._normalize_search_text(query)
        )

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

        # --------------------------------------------------
        # BASIC FUZZY SCORES
        # --------------------------------------------------

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

        # --------------------------------------------------
        # TOKEN OVERLAP
        # --------------------------------------------------

        overlap = 0.0

        if query_tokens:
            overlap = (
                len(query_tokens & title_tokens)
                / len(query_tokens)
            ) * 100.0

        # --------------------------------------------------
        # LENGTH SIMILARITY
        # --------------------------------------------------

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

        # --------------------------------------------------
        # BASE SCORE
        # --------------------------------------------------

        score = (
            (ratio * 0.23)
            + (compact_ratio * 0.22)
            + (partial * 0.12)
            + (token_sort * 0.15)
            + (token_set * 0.14)
            + (overlap * 0.08)
            + (length_score * 0.06)
        )

        # --------------------------------------------------
        # EXACT MATCH
        # --------------------------------------------------

        if query_normalized == title_normalized:
            score += 45.0

        if query_compact == title_compact:
            score += 25.0

        # --------------------------------------------------
        # PREFIX / PHRASE MATCH
        # --------------------------------------------------

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

        # Example:
        # "the vvaan"
        # ->
        # "the vvaan force of the forrest"

        if (
            query_tokens
            and query_tokens.issubset(title_tokens)
        ):
            score += 22.0

        # --------------------------------------------------
        # NUMERIC / SEQUEL MATCH
        # --------------------------------------------------

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

        # Example:
        # "sardar 2"
        # must prefer "Sardar 2"
        # over "Sardar"

        if (
            query_numbers
            and not title_numbers
        ):
            score -= 30.0

        # --------------------------------------------------
        # BASE TITLE MATCH
        # --------------------------------------------------

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

        # --------------------------------------------------
        # MEDIA TYPE RELEVANCE
        # --------------------------------------------------

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
            "tv limited series",
            "tvseries",
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
        }

        # Default preference for actual movies/series.
        if kind in movie_types:
            score += 12.0

        elif kind in series_types:
            score += 10.0

        elif kind in low_priority_types:
            score -= 22.0

        # --------------------------------------------------
        # TITLE TYPE WORDS
        # --------------------------------------------------

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

        # --------------------------------------------------
        # VERY STRONG CLOSE MATCH
        # --------------------------------------------------

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
        """
        Decide whether an IMDb result is actually relevant
        to the user's search.
        """

        query_normalized = (
            IMDBKit._normalize_search_text(query)
        )

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

        # Exact title is always valid.
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

        # Strong typo match.
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

        return score >= 62

    # ============================================================
    # Smart Media Title Parser
    # ============================================================

    @staticmethod
    def clean_media_title(
        value: Any,
    ) -> str:
        """
        Clean a filename/search string and extract
        a useful movie or series title.
        """

        if not value:
            return ""

        text = unicodedata.normalize(
            "NFKC",
            str(value),
        )

        # Remove file extension
        text = re.sub(
            r"\.(mkv|mp4|avi|mov|webm|m4v)$",
            "",
            text,
            flags=re.I,
        )

        # Replace common separators
        text = re.sub(
            r"[_\.]+",
            " ",
            text,
        )

        # Remove release/technical metadata
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

        # Remove season/episode markers.
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

        # Remove year only when it looks like release metadata.
        text = re.sub(
            r"\b(?:19|20)\d{2}\b",
            " ",
            text,
        )

        # Normalize remaining punctuation.
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
    @staticmethod
    def parse_media_structure(
        value: Any,
    ) -> Dict[str, Any]:
        """
        Detect year, season and episode information
        from movie/series filenames or searches.
        """

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

        # S01E05 / s01e05
        match = re.search(
            r"\bS(\d{1,3})E(\d{1,3})\b",
            text,
            flags=re.I,
        )

        if match:
            season = int(match.group(1))
            episode = int(match.group(2))

        else:
            # 1x05
            match = re.search(
                r"\b(\d{1,3})x(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(match.group(1))
                episode = int(match.group(2))

        if season is None:
            # Season 1 / Season01
            match = re.search(
                r"\bSeason[\s._-]*(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(match.group(1))

        if season is None:
            # S01
            match = re.search(
                r"\bS(\d{1,3})\b",
                text,
                flags=re.I,
            )

            if match:
                season = int(match.group(1))

        is_series = (
            season is not None
            or episode is not None
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
            "movie": "movie",
            "feature": "movie",
            "feature film": "movie",
            "tvseries": "tv series",
            "tv series": "tv series",
            "series": "tv series",
            "tvminiseries": "tvMiniSeries",
            "tv mini series": "tvMiniSeries",
            "tv movie": "tvMovie",
            "tvmovie": "tvMovie",
            "short": "short",
            "video": "video",
            "tv episode": "tvEpisode",
            "tvepisode": "tvEpisode",
        }

        return mapping.get(
            value,
            value,
        )

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
        Search IMDb titles with advanced fuzzy matching.

        Maximum returned results: 10
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

        # Never allow more than 10 suggestions.
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

        data = self._request_json(url)

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

            imdb_id = self._normalize_imdb_id(
                item.get("id")
            )

            if (
                not imdb_id
                or not imdb_id.startswith("tt")
            ):
                continue

            # Remove duplicate IMDb IDs.
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

            # Year bonus when user explicitly searched a year.
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

        # Highest quality matches first.
        candidates.sort(
            key=lambda pair: (
                pair[0],
                pair[1].year or 0,
            ),
            reverse=True,
        )

        # Final maximum = 10.
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
            f"{self.TMDB_BASE_URL}/find/{imdb_id}",
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
            f"{self.TMDB_BASE_URL}/{media_type}/{tmdb_id}",
            {
                "api_key": self.tmdb_api_key,
                "append_to_response": (
                    "credits,external_ids,"
                    "alternative_titles"
                ),
            },
        )

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

            # ----------------------------------------------------
            # Basic
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # Runtime
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # Poster
            # ----------------------------------------------------

            poster_path = tmdb_data.get(
                "poster_path"
            )

            if poster_path:
                poster_url = (
                    "https://image.tmdb.org/t/p/w1280"
                    + poster_path
                )

            # ----------------------------------------------------
            # Countries
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
            # Languages
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # Genres
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
            # Alternative titles
            # ----------------------------------------------------

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

            # ----------------------------------------------------
            # Credits
            # ----------------------------------------------------

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

                    elif (
                        department == "sound"
                    ):
                        composers.append(name)

                    elif (
                        department == "camera"
                    ):
                        cinematographers.append(
                            name
                        )

            # ----------------------------------------------------
            # Series
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

            # ----------------------------------------------------
            # Movie
            # ----------------------------------------------------

            elif media_type == "movie":

                kind = "movie"

                box_office = (
                    tmdb_data.get(
                        "revenue"
                    )
                    or None
                )

        # --------------------------------------------------------
        # IMDb fallback data
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # IMDb URL
        # --------------------------------------------------------

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
        Fetch complete movie/series metadata.

        Accepts:

            tt0816692
            0816692
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
        """
        Legacy Cinemagoer-compatible method.

        Existing old code may call:

            imdb.update(movie, info=['main'])

        IMDBKit already loads metadata in get_movie(),
        so this simply returns the object.
        """

        return movie

    # ============================================================
    # Convenience
    # ============================================================

    def get_movie_details(
        self,
        movie_id: Any,
    ) -> Dict[str, Any]:
        """
        Return movie metadata as a dictionary.
        """

        movie = self.get_movie(
            movie_id
        )

        return movie.to_dict()

    def search(
        self,
        title: str,
        results: int = 10,
    ) -> SearchResult:
        """
        Alias for search_movie().
        """

        return self.search_movie(
            title,
            results=results,
        )
