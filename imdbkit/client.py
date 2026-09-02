import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .models import Movie, SearchResult, SeriesInfo, Title


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

        except Exception:
            return None

    # ============================================================
    # Helpers
    # ============================================================

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

        match = re.search(r"\b(19|20)\d{2}\b", str(value))

        if match:
            try:
                return int(match.group(0))
            except Exception:
                pass

        return None

    @staticmethod
    def _normalize_kind(kind: Any) -> Optional[str]:
        if not kind:
            return None

        value = str(kind).strip().lower()

        mapping = {
            "movie": "movie",
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

        return mapping.get(value, value)

    @staticmethod
    def _extract_name(value: Any) -> str:
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
    def _extract_names(values: Any) -> List[str]:
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
        Search IMDb titles.

        Returns:

            SearchResult(
                titles=[
                    Title(...)
                ]
            )
        """

        if not title:
            return SearchResult([])

        title = str(title).strip()

        if not title:
            return SearchResult([])

        encoded = urllib.parse.quote(title, safe="")

        url = f"{self.IMDb_SUGGESTION_URL}{encoded}.json"

        data = self._request_json(url)

        if not data:
            return SearchResult([])

        raw_results = data.get("d", [])

        titles = []

        for item in raw_results:
            if not isinstance(item, dict):
                continue

            imdb_id = self._normalize_imdb_id(
                item.get("id")
            )

            if not imdb_id:
                continue

            item_title = (
                item.get("l")
                or item.get("title")
                or ""
            )

            year = self._safe_int(
                item.get("y")
                or item.get("year")
            )

            kind = self._normalize_kind(
                item.get("q")
                or item.get("kind")
            )

            image_url = (
                item.get("i", {}).get("imageUrl")
                if isinstance(item.get("i"), dict)
                else None
            )

            titles.append(
                Title(
                    imdb_id=imdb_id,
                    title=item_title,
                    year=year,
                    kind=kind,
                    image_url=image_url,
                )
            )

            if len(titles) >= results:
                break

        # --------------------------------------------------------
        # Fallback local fuzzy sorting
        # --------------------------------------------------------

        try:
            from rapidfuzz import fuzz

            query = title.lower()

            titles.sort(
                key=lambda item: fuzz.ratio(
                    query,
                    item.title.lower(),
                ),
                reverse=True,
            )

        except Exception:
            pass

        return SearchResult(titles)

    # ============================================================
    # IMDb ID Search
    # ============================================================

    def _find_imdb_title(
        self,
        imdb_id: str,
    ) -> Optional[Title]:
        imdb_id = self._normalize_imdb_id(imdb_id)

        if not imdb_id:
            return None

        data = self._request_json(
            f"https://v3.sg.media-imdb.com/suggestion/x/{imdb_id}.json"
        )

        if not data:
            return None

        for item in data.get("d", []):
            if not isinstance(item, dict):
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
                    item.get("i", {}).get("imageUrl")
                    if isinstance(item.get("i"), dict)
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
        if not self.tmdb_api_key or not tmdb_id:
            return None

        return self._request_json(
            f"{self.TMDB_BASE_URL}/{media_type}/{tmdb_id}",
            {
                "api_key": self.tmdb_api_key,
                "append_to_response": "credits,external_ids",
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
                self._year_from_date(release_date)
                or year
            )

            plot = (
                tmdb_data.get("overview")
                or None
            )

            rating = self._safe_float(
                tmdb_data.get("vote_average")
            )

            votes = self._safe_int(
                tmdb_data.get("vote_count")
            )

            runtime = (
                tmdb_data.get("runtime")
                or (
                    tmdb_data.get("episode_run_time", [None])[0]
                    if tmdb_data.get("episode_run_time")
                    else None
                )
            )

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
                    []
                )
                if isinstance(item, dict)
                and item.get("name")
            ]

            # ----------------------------------------------------
            # Languages
            # ----------------------------------------------------

            languages = [
                item.get("english_name")
                or item.get("name")
                or item.get("iso_639_1")
                for item in tmdb_data.get(
                    "spoken_languages",
                    []
                )
                if isinstance(item, dict)
            ]

            # ----------------------------------------------------
            # Genres
            # ----------------------------------------------------

            genres = [
                item.get("name")
                for item in tmdb_data.get(
                    "genres",
                    []
                )
                if isinstance(item, dict)
                and item.get("name")
            ]

            # ----------------------------------------------------
            # Alternative titles
            # ----------------------------------------------------

            title_akas = []

            alternative_titles = tmdb_data.get(
                "alternative_titles"
            )

            if isinstance(alternative_titles, dict):

                alternative_title_list = (
                    alternative_titles.get("titles", [])
                )

                if isinstance(
                    alternative_title_list,
                    list
                ):

                    for item in alternative_title_list:

                        if not isinstance(
                            item,
                            dict
                        ):
                            continue

                        value = (
                            item.get("title")
                            or item.get("name")
                        )

                        if value:
                            title_akas.append(value)
            # ----------------------------------------------------
            # Credits
            # ----------------------------------------------------

            credits = tmdb_data.get(
                "credits",
                {}
            )

            if isinstance(credits, dict):

                for person in credits.get(
                    "cast",
                    []
                )[:20]:

                    if not isinstance(person, dict):
                        continue

                    name = person.get("name")

                    if name:
                        cast.append(name)

                for person in credits.get(
                    "crew",
                    []
                ):

                    if not isinstance(person, dict):
                        continue

                    name = person.get("name")

                    if not name:
                        continue

                    department = (
                        person.get("department")
                        or ""
                    ).lower()

                    job = (
                        person.get("job")
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
                        cinematographers.append(name)

            # ----------------------------------------------------
            # Series
            # ----------------------------------------------------

            if media_type == "tv":

                kind = "tv series"

                for season in tmdb_data.get(
                    "seasons",
                    []
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
                                or f"Season {season_number}"
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
                    tmdb_data.get("revenue")
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
            f"https://www.imdb.com/title/{imdb_id}/"
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
                []
            )

            tv_results = tmdb_find.get(
                "tv_results",
                []
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

        movie = self.get_movie(movie_id)

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
