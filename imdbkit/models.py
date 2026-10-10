import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

_UPCOMING_STATUSES = {
    "planned", "in production", "post production", "rumored", "upcoming",
}

SERIES_KINDS = {"Web Series", "Mini Series"}


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def format_runtime(minutes: Any) -> Optional[str]:
    """90 -> '1h 30m', 45 -> '45m'."""
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


@dataclass
class Credit:
    """
    Cinemagoer-compatible person/company object.

    Supports:
        person.name
        str(person)
    """

    name: str

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

    def get(self, key: str, default: Any = None):
        if key == "name":
            return self.name
        return default

    def __getitem__(self, key: str):
        if key == "name":
            return self.name
        raise KeyError(key)


@dataclass
class Title:
    """
    Lightweight search result.

    Compatible with:
        result.titles
        title.imdb_id
        title.movieID
        title.year
        title.kind
        title.title
        title.image_url
    """

    imdb_id: str
    title: str
    year: Optional[int] = None
    kind: Optional[str] = None
    image_url: Optional[str] = None

    # --- extra info (all optional) ---
    end_year: Optional[int] = None          # series: last year, None if ongoing/unknown
    cast_text: Optional[str] = None         # e.g. "Matthew McConaughey, Anne Hathaway"
    rank: Optional[int] = None              # IMDb popularity rank (lower = more popular)
    popularity: Optional[float] = None      # TMDB popularity
    release_date: Optional[str] = None
    tmdb_id: Optional[int] = None
    backdrop_url: Optional[str] = None
    overview: Optional[str] = None
    score: float = 0.0                      # relevance score of the last search
    platform: Optional[str] = None          # e.g. "Netflix" (when known)

    @property
    def movieID(self) -> str:
        """Legacy Cinemagoer-compatible numeric IMDb ID."""
        return self.imdb_id.replace("tt", "", 1)

    @property
    def cover_url(self) -> Optional[str]:
        return self.image_url

    @property
    def is_series(self) -> bool:
        return self.kind in SERIES_KINDS

    @property
    def is_upcoming(self) -> bool:
        """True for titles that are not released yet (future)."""
        release = _parse_date(self.release_date)
        if release:
            return release > date.today()
        return bool(self.year and self.year > date.today().year)

    @property
    def status(self) -> str:
        return "Upcoming" if self.is_upcoming else "Released"

    @property
    def year_text(self) -> str:
        if not self.year:
            return ""
        if self.is_series and self.end_year and self.end_year != self.year:
            return f"{self.year}–{self.end_year}"
        return str(self.year)

    @property
    def label(self) -> str:
        """Ready to show in buttons: 'Interstellar (2014) • Movie'."""
        text = self.title
        if self.year_text:
            text += f" ({self.year_text})"
        if self.kind:
            text += f" • {self.kind}"
        if self.platform:
            text += f" • {self.platform}"
        if self.is_upcoming:
            text += " • Upcoming"
        return text

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dictionary-style access."""
        mapping = {
            "imdb_id": self.imdb_id,
            "imdbID": self.movieID,
            "movieID": self.movieID,
            "title": self.title,
            "year": self.year,
            "kind": self.kind,
            "image_url": self.image_url,
            "cover_url": self.cover_url,
            "end_year": self.end_year,
            "cast": self.cast_text,
            "rank": self.rank,
            "release_date": self.release_date,
            "tmdb_id": self.tmdb_id,
            "backdrop_url": self.backdrop_url,
            "overview": self.overview,
            "status": self.status,
            "label": self.label,
            "platform": self.platform,
        }

        return mapping.get(key, default)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)

        if value is None and key not in {
            "imdb_id",
            "imdbID",
            "movieID",
            "title",
            "year",
            "kind",
            "image_url",
            "cover_url",
            "end_year",
            "cast",
            "rank",
            "release_date",
            "tmdb_id",
            "backdrop_url",
            "overview",
        }:
            raise KeyError(key)

        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "imdb_id": self.imdb_id,
            "title": self.title,
            "year": self.year,
            "end_year": self.end_year,
            "kind": self.kind,
            "image_url": self.image_url,
            "backdrop_url": self.backdrop_url,
            "cast": self.cast_text,
            "status": self.status,
            "is_upcoming": self.is_upcoming,
            "label": self.label,
            "tmdb_id": self.tmdb_id,
            "platform": self.platform,
        }


@dataclass
class SearchResult:
    """
    Search response returned by IMDBKit.search_movie().
    """

    titles: List[Title] = field(default_factory=list)

    query: Optional[str] = None
    corrected_query: Optional[str] = None   # "Did you mean ..." (None if query was fine)
    year: Optional[int] = None              # year parsed from the query
    season: Optional[int] = None            # season parsed from the query (S01 -> 1)
    episode: Optional[int] = None           # episode parsed from the query (E02 -> 2)

    def __iter__(self):
        return iter(self.titles)

    def __len__(self):
        return len(self.titles)

    def __getitem__(self, index):
        return self.titles[index]

    @property
    def best(self) -> Optional[Title]:
        return self.titles[0] if self.titles else None

    @property
    def season_code(self) -> Optional[str]:
        """'S01' / 'S01E02' when the query contained season/episode info."""
        if self.season is None and self.episode is None:
            return None
        code = f"S{self.season:02d}" if self.season is not None else ""
        if self.episode is not None:
            code += f"E{self.episode:02d}"
        return code


@dataclass
class Episode:
    """Single TV episode."""

    season: int
    episode: int
    title: Optional[str] = None
    plot: Optional[str] = None
    air_date: Optional[str] = None
    runtime: Optional[int] = None
    rating: Optional[float] = None
    votes: Optional[int] = None
    still_url: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None

    @property
    def code(self) -> str:
        """'S01E02'"""
        return f"S{int(self.season):02d}E{int(self.episode):02d}"

    @property
    def runtime_text(self) -> Optional[str]:
        return format_runtime(self.runtime)

    @property
    def is_upcoming(self) -> bool:
        air = _parse_date(self.air_date)
        return bool(air and air > date.today())

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "season": self.season,
            "episode": self.episode,
            "title": self.title,
            "plot": self.plot,
            "air_date": self.air_date,
            "runtime": self.runtime,
            "runtime_text": self.runtime_text,
            "rating": self.rating,
            "votes": self.votes,
            "still_url": self.still_url,
            "imdb_id": self.imdb_id,
            "is_upcoming": self.is_upcoming,
        }


@dataclass
class Season:
    """One TV season with its episode list."""

    season: int
    name: Optional[str] = None
    plot: Optional[str] = None
    air_date: Optional[str] = None
    poster_url: Optional[str] = None
    episodes: List[Episode] = field(default_factory=list)

    @property
    def code(self) -> str:
        return f"S{int(self.season):02d}"

    @property
    def episode_count(self) -> int:
        return len(self.episodes)

    def get_episode(self, number: int) -> Optional[Episode]:
        for item in self.episodes:
            if item.episode == number:
                return item
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "season": self.season,
            "name": self.name,
            "plot": self.plot,
            "air_date": self.air_date,
            "poster_url": self.poster_url,
            "episode_count": self.episode_count,
            "episodes": [e.to_dict() for e in self.episodes],
        }


@dataclass
class SeriesInfo:
    """
    Series information used by the existing bot.

    display_seasons items are dicts:
        {"season": 1, "code": "S01", "name": "Season 1", "episodes": 8,
         "air_date": "2019-01-01", "poster_url": "...", "plot": "...",
         "upcoming": False}
    """

    display_seasons: List[Any] = field(default_factory=list)


@dataclass
class Movie:
    """
    Common movie/TV metadata model.

    The attributes here intentionally match the fields currently
    consumed by the bot's Imdbposter.py and utils.py.
    """

    release_date: Optional[str] = None
    year: Optional[int] = None
    plot: Optional[str] = None

    imdb_id: Optional[str] = None
    title: Optional[str] = None
    votes: Optional[int] = None

    title_akas: List[Any] = field(default_factory=list)

    info_series: SeriesInfo = field(default_factory=SeriesInfo)

    worldwide_gross: Any = None
    title_localized: Optional[str] = None

    kind: Optional[str] = None

    stars: List[Any] = field(default_factory=list)

    duration: Any = None

    countries: List[Any] = field(default_factory=list)
    certificates: List[Any] = field(default_factory=list)
    languages: List[Any] = field(default_factory=list)

    directors: List[Any] = field(default_factory=list)
    writers: List[Any] = field(default_factory=list)
    producers: List[Any] = field(default_factory=list)
    composers: List[Any] = field(default_factory=list)
    cinematographers: List[Any] = field(default_factory=list)
    music_team: List[Any] = field(default_factory=list)
    distributors: List[Any] = field(default_factory=list)

    genres: List[Any] = field(default_factory=list)

    rating: Optional[float] = None

    cover_url: Optional[str] = None
    url: Optional[str] = None

    original_title: Optional[str] = None
    tagline: Optional[str] = None
    status: Optional[str] = None
    episodes: Optional[int] = None

    # ---------------- new in 1.1 ----------------
    backdrop_url: Optional[str] = None      # landscape image (1280px wide)
    posters: List[str] = field(default_factory=list)    # extra portrait posters
    backdrops: List[str] = field(default_factory=list)  # extra landscape images
    logo_url: Optional[str] = None
    trailer_url: Optional[str] = None
    imdb_poster_url: Optional[str] = None   # the poster IMDb itself uses

    tmdb_id: Optional[int] = None
    media_type: Optional[str] = None        # "movie" | "tv"
    original_language: Optional[str] = None
    rating_source: Optional[str] = None     # "IMDb" | "TMDB"
    tmdb_rating: Optional[float] = None
    tmdb_votes: Optional[int] = None

    creators: List[Any] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    production_companies: List[str] = field(default_factory=list)
    cast_detail: List[Dict[str, Any]] = field(default_factory=list)
    number_of_seasons: Optional[int] = None
    next_episode: Optional[Episode] = None
    last_episode: Optional[Episode] = None

    streaming_on: List[str] = field(default_factory=list)   # e.g. ["Netflix", "MX Player"]
    streaming_region: Optional[str] = None                   # region of streaming_on, e.g. "IN"

    series_title: Optional[str] = None      # set when the object describes an episode
    season_info: Optional[Season] = None    # set by IMDBKit.lookup("show S02")
    episode_info: Optional[Episode] = None  # set by IMDBKit.lookup("show S02E05")

    def __post_init__(self):
        """
        Normalize credit fields to Cinemagoer-compatible objects.

        This keeps compatibility with older bot code that expects:

            person.name

        while IMDBKit internally may receive plain strings.
        """

        credit_fields = (
            "stars",
            "directors",
            "writers",
            "producers",
            "composers",
            "cinematographers",
            "music_team",
            "distributors",
            "creators",
        )

        for field_name in credit_fields:
            values = getattr(self, field_name, None)

            if not values:
                setattr(self, field_name, [])
                continue

            if not isinstance(values, (list, tuple, set)):
                values = [values]

            normalized = []

            for value in values:
                if isinstance(value, Credit):
                    normalized.append(value)

                elif isinstance(value, str):
                    value = value.strip()

                    if value:
                        normalized.append(
                            Credit(name=value)
                        )

                elif isinstance(value, dict):
                    name = (
                        value.get("name")
                        or value.get("original_name")
                        or value.get("title")
                        or ""
                    )

                    if name:
                        normalized.append(
                            Credit(name=str(name).strip())
                        )

                elif hasattr(value, "name"):
                    name = getattr(value, "name", None)

                    if name:
                        normalized.append(
                            Credit(name=str(name).strip())
                        )

                else:
                    text = str(value).strip()

                    if text:
                        normalized.append(
                            Credit(name=text)
                        )

            setattr(
                self,
                field_name,
                normalized,
            )

    @property
    def movieID(self) -> Optional[str]:
        """Legacy Cinemagoer-compatible numeric IMDb ID."""

        if not self.imdb_id:
            return None

        return self.imdb_id.replace("tt", "", 1)

    @property
    def imdbID(self) -> Optional[str]:
        return self.movieID

    @property
    def poster_url(self) -> Optional[str]:
        return self.cover_url

    @property
    def is_series(self) -> bool:
        return self.kind in SERIES_KINDS or self.media_type == "tv"

    @property
    def runtime(self) -> Optional[int]:
        """Runtime in minutes (episode runtime for series)."""
        try:
            value = int(self.duration)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @property
    def runtime_text(self) -> Optional[str]:
        return format_runtime(self.runtime)

    @property
    def is_upcoming(self) -> bool:
        """True when the title is not released yet."""
        if self.status and self.status.strip().lower() in _UPCOMING_STATUSES:
            return True
        release = _parse_date(self.release_date)
        if release:
            return release > date.today()
        return bool(self.year and self.year > date.today().year)

    @property
    def description(self) -> Optional[str]:
        """Alias of plot."""
        return self.plot

    def get(self, key: str, default: Any = None) -> Any:
        """
        Dictionary-style compatibility.

        This allows older code such as:

            movie.get("title")
            movie.get("year")
            movie.get("genres")
        """
        aliases = {
            "imdbID": self.imdb_id,
            "movieID": self.movieID,

            "original air date": self.release_date,
            "release_date": self.release_date,

            "year": self.year,
            "plot": self.plot,
            "plot outline": self.plot,

            "title": self.title,
            "localized title": self.title_localized,

            "genres": self.genres,
            "rating": self.rating,
            "cover_url": self.cover_url,
            "full-size cover url": self.cover_url,
            "poster": self.cover_url,
            "backdrop": self.backdrop_url,

            "runtimes": self.duration,
            "runtime": self.duration,

            "countries": self.countries,
            "certificates": self.certificates,
            "languages": self.languages,

            "director": self.directors,
            "writer": self.writers,
            "producer": self.producers,
            "composer": self.composers,
            "cinematographer": self.cinematographers,

            "music department": self.music_team,
            "distributors": self.distributors,

            "cast": self.stars,

            "box office": self.worldwide_gross,

            "kind": self.kind,
            "votes": self.votes,
            "url": self.url,

            "akas": self.title_akas,
            "title akas": self.title_akas,

            "seasons": self.info_series.display_seasons,
        }

        if key in aliases:
            return aliases[key]

        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)

        if value is None and not hasattr(self, key):
            raise KeyError(key)

        return value

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def to_dict(self) -> Dict[str, Any]:
        """Return metadata as a normal dictionary."""

        return {
            "release_date": self.release_date,
            "year": self.year,
            "plot": self.plot,
            "imdb_id": self.imdb_id,
            "title": self.title,
            "votes": self.votes,
            "title_akas": self.title_akas,
            "seasons": self.info_series.display_seasons,
            "worldwide_gross": self.worldwide_gross,
            "title_localized": self.title_localized,
            "kind": self.kind,
            "stars": self.stars,
            "duration": self.duration,
            "runtime": self.runtime,
            "runtime_text": self.runtime_text,
            "countries": self.countries,
            "certificates": self.certificates,
            "languages": self.languages,
            "directors": self.directors,
            "writers": self.writers,
            "producers": self.producers,
            "composers": self.composers,
            "cinematographers": self.cinematographers,
            "music_team": self.music_team,
            "distributors": self.distributors,
            "genres": self.genres,
            "rating": self.rating,
            "rating_source": self.rating_source,
            "cover_url": self.cover_url,
            "backdrop_url": self.backdrop_url,
            "posters": self.posters,
            "backdrops": self.backdrops,
            "logo_url": self.logo_url,
            "trailer_url": self.trailer_url,
            "url": self.url,
            "original_title": self.original_title,
            "tagline": self.tagline,
            "status": self.status,
            "is_upcoming": self.is_upcoming,
            "episodes": self.episodes,
            "number_of_seasons": self.number_of_seasons,
            "creators": self.creators,
            "networks": self.networks,
            "streaming_on": self.streaming_on,
            "streaming_region": self.streaming_region,
            "tmdb_id": self.tmdb_id,
            "media_type": self.media_type,
            "original_language": self.original_language,
            "series_title": self.series_title,
            "next_episode": self.next_episode.to_dict() if self.next_episode else None,
            "last_episode": self.last_episode.to_dict() if self.last_episode else None,
            "season_info": self.season_info.to_dict() if self.season_info else None,
            "episode_info": self.episode_info.to_dict() if self.episode_info else None,
        }
