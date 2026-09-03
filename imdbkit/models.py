from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
    Lightweight IMDb search result.

    Compatible with:
        result.titles
        title.imdb_id
        title.movieID
        title.year
        title.kind
        title.title
    """

    imdb_id: str
    title: str
    year: Optional[int] = None
    kind: Optional[str] = None
    image_url: Optional[str] = None

    @property
    def movieID(self) -> str:
        """Legacy Cinemagoer-compatible numeric IMDb ID."""
        return self.imdb_id.replace("tt", "", 1)

    @property
    def cover_url(self) -> Optional[str]:
        return self.image_url

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
        }:
            raise KeyError(key)

        return value


@dataclass
class SearchResult:
    """
    Search response returned by IMDBKit.search_movie().
    """

    titles: List[Title] = field(default_factory=list)

    def __iter__(self):
        return iter(self.titles)

    def __len__(self):
        return len(self.titles)

    def __getitem__(self, index):
        return self.titles[index]


@dataclass
class SeriesInfo:
    """
    Series information used by the existing bot.
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
            "cover_url": self.cover_url,
            "url": self.url,
        }
