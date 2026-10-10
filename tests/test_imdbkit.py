"""
Offline tests: IMDb / TMDB are replaced with a small fake backend, so these run
without network.   Run:  python -m unittest discover -s tests -v
"""
import json
import re
import threading
import unittest
import urllib.parse
from datetime import date
from unittest import mock

from imdbkit import IMDBKit
from imdbkit.client import RequestCoalescer, TTLCache
from imdbkit.models import Movie, Title
from imdbkit.textutils import devanagari_to_roman, parse_media_query, phonetic_key

NEXT_YEAR = date.today().year + 1
IMG = "https://m.media-amazon.com/images/M/{}@._V1_.jpg"


def item(imdb_id, name, qid, q, year, rank=None, yr=None, cast=None, img=True):
    d = {"id": imdb_id, "l": name, "qid": qid, "q": q, "y": year, "s": cast or ""}
    if rank:
        d["rank"] = rank
    if yr:
        d["yr"] = yr
    if img:
        d["i"] = {"imageUrl": IMG.format(imdb_id), "height": 3000, "width": 2000}
    return d


IMDB_DB = [
    item("tt0816692", "Interstellar", "movie", "feature", 2014, 150, cast="Matthew McConaughey, Anne Hathaway"),
    item("tt2325812", "Interstellar Marines", "videoGame", "video game", 2013, 90000),
    item("tt7000001", "Interstellar: Nolan's Odyssey", "video", "video", 2014, 70000),
    item("tt7000002", "Interstellar Wars", "short", "short", 2016, 99000),
    {"id": "nm0000001", "l": "Interstellar Person", "q": "", "y": 1970},
    item("tt0848228", "The Avengers", "movie", "feature", 2012, 300),
    item("tt4154796", "Avengers: Endgame", "movie", "feature", 2019, 120),
    item("tt4154756", "Avengers: Infinity War", "movie", "feature", 2018, 130),
    item("tt2395427", "Avengers: Age of Ultron", "movie", "feature", 2015, 400),
    item("tt2455546", "Avengers Assemble", "tvSeries", "TV series", 2013, 8000, yr="2013-2019"),
    item("tt0118661", "The Avengers", "movie", "feature", 1998, 60000),
    item("tt10811166", "Pathaan", "movie", "feature", 2023, 200),
    item("tt1160419", "Dune", "movie", "feature", 2021, 100),
    item("tt15239678", "Dune: Part Two", "movie", "feature", 2024, 90),
    item("tt0087182", "Dune", "movie", "feature", 1984, 5000),
    item("tt10466872", "Dune: Prophecy", "tvSeries", "TV series", 2024, 2000, yr="2024-"),
    item("tt8283844", "Mirzapur", "tvSeries", "TV series", 2018, 500, yr="2018-"),
    item("tt12004706", "Panchayat", "tvSeries", "TV series", 2020, 400, yr="2020-"),
    item("tt9000001", "Panchayat", "tvEpisode", "TV episode", 2020, 90000),
    item("tt9000002", "Panchayat Podcast", "podcastSeries", "podcast series", 2022, 90000),
    item("tt6468322", "Money Heist", "tvSeries", "TV series", 2017, 60, yr="2017-2021"),
    item("tt9990001", "Zorblax Returns", "movie", "feature", NEXT_YEAR, 9000),
    item("tt9990002", "Zorblax", "movie", "feature", 1999, 90000),
    item("tt0903747", "Breaking Bad", "tvSeries", "TV series", 2008, 20, yr="2008-2013"),
]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("'", "")).strip()


def fake_suggest(query):
    q = _norm(urllib.parse.unquote(query))
    if q.startswith("tt"):
        return [i for i in IMDB_DB if i["id"] == q]
    hits = []
    for entry in IMDB_DB:
        title = _norm(entry["l"])
        words = title.split()
        if title.startswith(q) or (len(q.split()) == 1 and any(w.startswith(q) for w in words)) \
                or title.replace(" ", "").startswith(q.replace(" ", "")):
            hits.append(entry)
    hits.sort(key=lambda e: e.get("rank", 10 ** 9))
    return hits[:8]


JSON_LD = {
    "@type": "Movie", "name": "Interstellar",
    "description": "When Earth becomes uninhabitable, a team of explorers travels through a wormhole.",
    "image": "https://m.media-amazon.com/images/M/MV5BInterstellar@._V1_.jpg",
    "aggregateRating": {"ratingValue": 8.7, "ratingCount": 2200000},
    "duration": "PT2H49M", "genre": ["Adventure", "Drama", "Sci-Fi"],
    "datePublished": "2014-11-07", "contentRating": "PG-13",
    "director": [{"@type": "Person", "name": "Christopher Nolan"}],
    "actor": [{"@type": "Person", "name": "Matthew McConaughey"}],
}

TMDB_MOVIE = {
    "id": 157336, "title": "Interstellar", "original_title": "Interstellar", "original_language": "en",
    "overview": "The adventures of a group of explorers who make use of a newly discovered wormhole.",
    "tagline": "Mankind was born on Earth. It was never meant to die here.",
    "release_date": "2014-11-05", "runtime": 169, "status": "Released", "vote_average": 8.4, "vote_count": 35000,
    "revenue": 701729206, "poster_path": "/poster_default.jpg", "backdrop_path": "/backdrop_default.jpg",
    "genres": [{"name": "Adventure"}, {"name": "Drama"}, {"name": "Science Fiction"}],
    "production_countries": [{"name": "United States of America"}],
    "spoken_languages": [{"english_name": "English", "iso_639_1": "en"}],
    "production_companies": [{"name": "Legendary Pictures"}],
    "credits": {
        "cast": [{"name": "Matthew McConaughey", "character": "Cooper", "profile_path": "/mm.jpg"}],
        "crew": [
            {"name": "Christopher Nolan", "department": "Directing", "job": "Director"},
            {"name": "Jonathan Nolan", "department": "Writing", "job": "Screenplay"},
            {"name": "Emma Thomas", "department": "Production", "job": "Producer"},
            {"name": "Hans Zimmer", "department": "Sound", "job": "Original Music Composer"},
            {"name": "Hoyte van Hoytema", "department": "Camera", "job": "Director of Photography"},
        ],
    },
    "external_ids": {"imdb_id": "tt0816692"},
    "alternative_titles": {"titles": [{"title": "Interestelar"}]},
    "images": {
        "posters": [
            {"file_path": "/poster_fr.jpg", "iso_639_1": "fr", "vote_average": 9, "vote_count": 9},
            {"file_path": "/poster_en.jpg", "iso_639_1": "en", "vote_average": 5.5, "vote_count": 20},
        ],
        "backdrops": [
            {"file_path": "/backdrop_text.jpg", "iso_639_1": "en", "vote_average": 6, "vote_count": 5},
            {"file_path": "/backdrop_clean.jpg", "iso_639_1": None, "vote_average": 5, "vote_count": 5},
        ],
        "logos": [{"file_path": "/logo.png", "iso_639_1": "en", "vote_average": 5, "vote_count": 1}],
    },
    "videos": {"results": [
        {"site": "YouTube", "key": "TEASER1", "type": "Teaser", "official": True},
        {"site": "YouTube", "key": "TRAILER1", "type": "Trailer", "official": True},
    ]},
    "release_dates": {"results": [
        {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
        {"iso_3166_1": "IN", "release_dates": [{"certification": ""}, {"certification": "UA"}]},
    ]},
}

TMDB_TV = {
    "id": 1234, "name": "Panchayat", "original_name": "पंचायत", "original_language": "hi",
    "overview": "A young engineering graduate joins as a panchayat secretary in a village.",
    "first_air_date": "2020-04-03", "status": "Returning Series", "vote_average": 8.9, "vote_count": 900,
    "episode_run_time": [], "last_episode_to_air": {
        "season_number": 4, "episode_number": 8, "name": "Finale", "runtime": 38, "air_date": "2025-07-02"},
    "next_episode_to_air": None,
    "number_of_seasons": 4, "number_of_episodes": 32,
    "created_by": [{"name": "Chandan Kumar"}], "networks": [{"name": "Amazon Prime Video"}],
    "poster_path": "/pp.jpg", "backdrop_path": "/pb.jpg",
    "genres": [{"name": "Comedy"}, {"name": "Drama"}],
    "origin_country": ["IN"], "spoken_languages": [{"english_name": "Hindi", "iso_639_1": "hi"}],
    "seasons": [
        {"season_number": 0, "name": "Specials", "episode_count": 2, "air_date": "2020-01-01"},
        {"season_number": 1, "name": "Season 1", "episode_count": 8, "air_date": "2020-04-03", "poster_path": "/s1.jpg"},
        {"season_number": 3, "name": "Season 3", "episode_count": 8, "air_date": "2024-05-28", "poster_path": "/s3.jpg"},
        {"season_number": 6, "name": "Season 6", "episode_count": 0, "air_date": None},
    ],
    "credits": {"cast": [{"name": "Jitendra Kumar", "character": "Abhishek"}], "crew": []},
    "external_ids": {"imdb_id": "tt12004706"},
    "alternative_titles": {"results": [{"title": "Panchayat (TV Series)"}]},
    "images": {"posters": [], "backdrops": [], "logos": []},
    "videos": {"results": []},
    "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-14"}, {"iso_3166_1": "IN", "rating": "UA 16+"}]},
}

SEASON3 = {"name": "Season 3", "overview": "Third season.", "air_date": "2024-05-28", "poster_path": "/s3.jpg",
           "episodes": [
               {"id": 1, "season_number": 3, "episode_number": 1, "name": "Ghar Wapsi", "overview": "Back home.",
                "air_date": "2024-05-28", "runtime": 36, "vote_average": 8.2, "vote_count": 10, "still_path": "/e1.jpg"},
               {"id": 2, "season_number": 3, "episode_number": 2, "name": "Chakka Jam", "overview": "",
                "air_date": "2024-05-28", "runtime": 35, "vote_average": 0, "vote_count": 0, "still_path": None},
           ]}


def fake_http(self, url, params=None, headers=None, retries=1):
    params = params or {}
    path = url.split("?")[0]

    if "media-imdb.com/suggestion" in path:
        query = path.rsplit("/", 1)[1][: -len(".json")]
        return json.dumps({"d": fake_suggest(query), "q": query})

    if path.startswith("https://www.imdb.com/title/"):
        if path.rstrip("/").endswith("tt0816692"):
            return f'<html><script type="application/ld+json">{json.dumps(JSON_LD)}</script></html>'
        return "<html></html>"

    if path.startswith(self.TMDB_BASE_URL):
        route = path[len(self.TMDB_BASE_URL):]
        if route == "/find/tt0816692":
            return json.dumps({"movie_results": [{"id": 157336}], "tv_results": []})
        if route == "/find/tt12004706":
            return json.dumps({"movie_results": [], "tv_results": [{"id": 1234}]})
        if route == "/find/tt5555555":
            return json.dumps({"movie_results": [], "tv_results": [], "tv_episode_results": [
                {"show_id": 1234, "season_number": 3, "episode_number": 2, "name": "Chakka Jam",
                 "overview": "Ep plot", "runtime": 35, "still_path": "/e2.jpg"}]})
        if route.startswith("/find/"):
            return json.dumps({"movie_results": [], "tv_results": []})
        if route == "/movie/157336":
            return json.dumps(TMDB_MOVIE)
        if route == "/tv/1234":
            return json.dumps(TMDB_TV)
        if route == "/tv/1234/season/3":
            return json.dumps(SEASON3)
        if route == "/tv/1234/season/3/episode/2":
            return json.dumps(dict(SEASON3["episodes"][1], external_ids={"imdb_id": "tt5555555"}))
        if route.startswith("/search/multi"):
            q = _norm(params.get("query", ""))
            if q.startswith("zorblax"):
                return json.dumps({"results": [
                    {"media_type": "movie", "id": 777, "title": "Zorblax Returns",
                     "release_date": f"{NEXT_YEAR}-06-01", "poster_path": "/z.jpg", "popularity": 50},
                    {"media_type": "person", "id": 5, "name": "Zorblax"}]})
            return json.dumps({"results": []})
        if route == "/movie/777/external_ids":
            return json.dumps({"imdb_id": "tt9990001"})
        return None
    return None


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("_imdb_cache", "_imdb_id_cache", "_tmdb_cache", "_page_cache"):
            setattr(IMDBKit, name, TTLCache())
        patcher = mock.patch.object(IMDBKit, "_http_get", fake_http)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.kit = IMDBKit(tmdb_api_key="0123456789abcdef0123456789abcdef")
        self.kit_no_tmdb = IMDBKit(tmdb_api_key=None)
        self.kit_no_tmdb.tmdb_api_key = None


class TextTests(unittest.TestCase):
    def test_devanagari(self):
        self.assertEqual(devanagari_to_roman("पठान"), "pathan")
        self.assertEqual(devanagari_to_roman("दंगल"), "dangal")
        self.assertEqual(devanagari_to_roman("मिर्जापुर"), "mirjapur")
        self.assertEqual(devanagari_to_roman("स्त्री", long_vowels=True), "stree")

    def test_phonetic_equivalence(self):
        self.assertEqual(phonetic_key("Pathaan"), phonetic_key("pathan"))
        self.assertEqual(phonetic_key("Mirzapur"), phonetic_key("mirjapur"))
        self.assertEqual(phonetic_key("Jawan"), phonetic_key("jawaan"))

    def test_parser(self):
        p = parse_media_query("Panchayat.S03E02.1080p.WEB-DL.x264-GRP")
        self.assertEqual((p["title"], p["season"], p["episode"]), ("Panchayat", 3, 2))
        p = parse_media_query("Pushpa 2021 Hindi Dubbed")
        self.assertEqual((p["title"], p["year"]), ("Pushpa", 2021))
        self.assertEqual(parse_media_query("Blade Runner 2049")["title"], "Blade Runner 2049")
        self.assertEqual(parse_media_query("1917")["title"], "1917")
        self.assertEqual(parse_media_query("The Truman Show")["title"], "The Truman Show")
        self.assertEqual(parse_media_query("Hindi Medium")["title"], "Hindi Medium")
        p = parse_media_query("money heist season 5")
        self.assertEqual((p["title"], p["season"], p["is_series"]), ("money heist", 5, True))
        p = parse_media_query("mirzapur 2 x 4")
        self.assertIsNone(p["episode"])


class SearchTests(Base):
    def titles(self, query, **kw):
        return [t.title for t in self.kit.search_title(query, **kw).titles]

    def test_exact_and_no_junk(self):
        res = self.kit.search_title("interstellar")
        self.assertEqual(res.best.imdb_id, "tt0816692")
        self.assertEqual(len(res), 1)               # game / video / short / person removed
        self.assertIsNone(res.corrected_query)

    def test_typo_gets_did_you_mean(self):
        res = self.kit.search_title("intersteler")
        self.assertEqual(res.best.title, "Interstellar")
        self.assertEqual(res.corrected_query, "Interstellar")

    def test_heavy_typo_vocab_rescue(self):
        res = self.kit.search_title("avngers endgam")
        self.assertEqual(res.best.title, "Avengers: Endgame")

    def test_roman_hindi_spelling(self):
        self.assertEqual(self.titles("pathan")[0], "Pathaan")

    def test_devanagari_query(self):
        self.assertEqual(self.titles("पठान")[0], "Pathaan")

    def test_exact_beats_longer_titles(self):
        out = self.titles("dune")
        self.assertEqual(out[0], "Dune")
        self.assertEqual(self.kit.search_title("dune").best.year, 2021)  # popular one first

    def test_year_selects_right_one(self):
        self.assertEqual(self.kit.search_title("dune 1984").best.year, 1984)
        self.assertEqual(self.kit.search_title("dune part two 2024").best.title, "Dune: Part Two")

    def test_series_hint_and_se_parsing(self):
        res = self.kit.search_title("panchayat s03e02")
        self.assertEqual((res.best.title, res.best.kind), ("Panchayat", "Web Series"))
        self.assertEqual((res.season, res.episode, res.season_code), (3, 2, "S03E02"))
        self.assertEqual(len(res), 1)               # episode + podcast filtered

    def test_series_year_span(self):
        t = self.kit.search_title("money heist").best
        self.assertEqual((t.year, t.end_year, t.year_text), (2017, 2021, "2017–2021"))
        self.assertIn("Web Series", t.label)

    def test_kind_filter(self):
        out = self.kit.search_title("dune", kind="series").titles
        self.assertTrue(out and all(t.is_series for t in out))
        out = self.kit.search_title("dune", kind="movie").titles
        self.assertTrue(out and not any(t.is_series for t in out))

    def test_upcoming_title_found_and_flagged(self):
        res = self.kit.search_title("zorblax returns")
        self.assertEqual(res.best.title, "Zorblax Returns")
        self.assertTrue(res.best.is_upcoming)
        self.assertIn("Upcoming", res.best.label)
        self.assertFalse(self.kit.search_title("zorblax")[0].is_upcoming)
        no_future = self.kit.search_title("zorblax returns", include_upcoming=False)
        self.assertNotIn("Zorblax Returns", [t.title for t in no_future])

    def test_tmdb_only_upcoming_title_is_merged(self):
        # present only on TMDB -> must be added through external_ids
        with mock.patch.dict("imdbkit.client.__dict__", {}):
            IMDB_DB_BACKUP = list(IMDB_DB)
            try:
                IMDB_DB[:] = [e for e in IMDB_DB if e["id"] != "tt9990001"]
                res = self.kit.search_title("zorblax returns")
                self.assertEqual(res.best.imdb_id, "tt9990001")
                self.assertTrue(res.best.is_upcoming)
            finally:
                IMDB_DB[:] = IMDB_DB_BACKUP

    def test_garbage_returns_nothing(self):
        self.assertEqual(len(self.kit.search_title("qqqxxzzv")), 0)
        self.assertEqual(len(self.kit.search_title("")), 0)
        self.assertEqual(len(self.kit.search_title(None)), 0)

    def test_legacy_api_shape(self):
        res = self.kit.search_movie("interstellar")
        t = res.titles[0]
        self.assertEqual((t.movieID, t["title"], t.get("year"), t.cover_url), ("0816692", "Interstellar", 2014, t.image_url))
        self.assertIn("_V1_UX500_", t.image_url)

    def test_ai_corrector_hook(self):
        calls = []

        def ai(q):
            calls.append(q)
            return "Breaking Bad"

        kit = IMDBKit(tmdb_api_key=None, ai_corrector=ai)
        kit.tmdb_api_key = None
        res = kit.search_title("walter white chemistry teacher show")
        self.assertTrue(calls)
        self.assertEqual(res.best.title, "Breaking Bad")

    def test_ai_corrector_not_called_when_match_is_good(self):
        calls = []
        kit = IMDBKit(tmdb_api_key=None, ai_corrector=lambda q: calls.append(q))
        kit.tmdb_api_key = None
        kit.search_title("interstellar")
        self.assertEqual(calls, [])


class DetailsTests(Base):
    def test_movie_full_details(self):
        m = self.kit.get_movie("tt0816692")
        self.assertEqual((m.title, m.year, m.kind), ("Interstellar", 2014, "Movie"))
        self.assertEqual(m.duration, 169)
        self.assertEqual(m.runtime_text, "2h 49m")
        self.assertEqual(m.rating, 8.7)
        self.assertEqual(m.rating_source, "IMDb")
        self.assertEqual(m.tmdb_rating, 8.4)
        self.assertIn("wormhole", m.plot)
        self.assertEqual(m.genres, ["Adventure", "Drama", "Science Fiction"])
        self.assertEqual([str(x) for x in m.directors], ["Christopher Nolan"])
        self.assertEqual(str(m.composers[0]), "Hans Zimmer")
        self.assertEqual(str(m.cinematographers[0]), "Hoyte van Hoytema")
        self.assertEqual(m.certificates, ["IN:UA", "US:PG-13"])
        self.assertEqual(m.trailer_url, "https://www.youtube.com/watch?v=TRAILER1")
        self.assertEqual(m.status, "Released")
        self.assertFalse(m.is_upcoming)
        self.assertEqual(m.title_akas, ["Interestelar"])
        self.assertEqual(m.cast_detail[0]["character"], "Cooper")

    def test_poster_and_backdrop(self):
        m = self.kit.get_movie("tt0816692")
        self.assertEqual(m.cover_url, "https://image.tmdb.org/t/p/w780/poster_default.jpg")
        self.assertNotIn("w1280", m.cover_url)             # w1280 is not a poster size
        self.assertEqual(m.backdrop_url, "https://image.tmdb.org/t/p/w1280/backdrop_default.jpg")
        self.assertTrue(m.posters[0].endswith("/poster_default.jpg"))
        self.assertTrue(m.posters[1].endswith("/poster_en.jpg"))      # English before French
        self.assertTrue(m.backdrops[1].endswith("/backdrop_clean.jpg"))  # no-text backdrop first
        self.assertTrue(m.logo_url.endswith("/logo.png"))
        self.assertIn("_V1_UX800_", m.imdb_poster_url)

    def test_compat_dict_access(self):
        m = self.kit.get_movie("0816692")
        self.assertEqual(m.get("title"), "Interstellar")
        self.assertEqual(m["runtimes"], 169)
        self.assertEqual([str(c) for c in m.get("cast")], ["Matthew McConaughey"])
        self.assertIn("plot", m)
        self.assertEqual(m.info_series.display_seasons, [])
        self.assertEqual(m.to_dict()["backdrop_url"], m.backdrop_url)

    def test_series_details(self):
        m = self.kit.get_movie("tt12004706")
        self.assertEqual((m.title, m.kind, m.media_type), ("Panchayat", "Web Series", "tv"))
        self.assertEqual(m.duration, 38)                     # from last_episode_to_air
        self.assertEqual(m.episodes, 32)
        self.assertEqual(m.number_of_seasons, 4)
        self.assertEqual(m.status, "Returning Series")
        self.assertEqual(m.title_akas, ["Panchayat (TV Series)"])   # tv uses "results"
        self.assertEqual([str(c) for c in m.creators], ["Chandan Kumar"])
        self.assertEqual(m.networks, ["Amazon Prime Video"])
        self.assertEqual(m.certificates, ["IN:UA 16+", "US:TV-14"])
        codes = [s["code"] for s in m.info_series.display_seasons]
        self.assertEqual(codes, ["S01", "S03", "S06"])       # specials removed
        self.assertTrue(m.info_series.display_seasons[-1]["upcoming"])
        self.assertEqual(m.last_episode.code, "S04E08")
        self.assertEqual(m.rating_source, "TMDB")

    def test_seasons_and_episodes(self):
        season = self.kit.get_season("tt12004706", 3)
        self.assertEqual((season.code, season.episode_count), ("S03", 2))
        self.assertEqual(season.episodes[0].code, "S03E01")
        self.assertTrue(season.episodes[0].still_url.endswith("/e1.jpg"))
        self.assertEqual(season.episodes[0].runtime_text, "36m")
        self.assertIsNone(season.episodes[1].rating)         # 0 votes -> no fake rating

        ep = self.kit.get_episode("tt12004706", 3, 2)
        self.assertEqual((ep.code, ep.title, ep.imdb_id), ("S03E02", "Chakka Jam", "tt5555555"))
        self.assertIsNone(self.kit.get_episode("tt12004706", 99, 1))
        self.assertIsNone(self.kit.get_season("tt0816692", 1))      # movie has no seasons

    def test_episode_imdb_id(self):
        m = self.kit.get_movie("tt5555555")
        self.assertEqual((m.kind, m.series_title, m.episode_info.code), ("Episode", "Panchayat", "S03E02"))

    def test_lookup_with_episode_and_season(self):
        m = self.kit.lookup("panchayat s03e02")
        self.assertEqual((m.title, m.episode_info.code, m.episode_info.title), ("Panchayat", "S03E02", "Chakka Jam"))
        m = self.kit.lookup("panchyat season 3")
        self.assertEqual((m.title, m.season_info.code, m.season_info.episode_count), ("Panchayat", "S03", 2))
        self.assertIsNone(self.kit.lookup("qqqxxzzv"))

    def test_without_tmdb_uses_imdb_page(self):
        m = self.kit_no_tmdb.get_movie("tt0816692")
        self.assertEqual((m.title, m.year, m.rating, m.duration), ("Interstellar", 2014, 8.7, 169))
        self.assertIn("wormhole", m.plot)
        self.assertEqual([str(d) for d in m.directors], ["Christopher Nolan"])
        self.assertEqual(m.certificates, ["PG-13"])
        self.assertTrue(m.cover_url and "_V1_UX800_" in m.cover_url)
        self.assertIsNone(m.backdrop_url)

    def test_unknown_id_does_not_crash(self):
        m = self.kit.get_movie("tt0000000")
        self.assertIsInstance(m, Movie)
        self.assertEqual(self.kit.get_movie(None).title, None)


class InfraTests(unittest.TestCase):
    def test_coalescer_waiters_get_the_result(self):
        co = RequestCoalescer()
        calls, results = [], []
        gate = threading.Event()

        def slow():
            calls.append(1)
            gate.wait(1)
            return {"ok": True}

        threads = [threading.Thread(target=lambda: results.append(co.execute("k", slow))) for _ in range(5)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"ok": True}] * 5)    # old implementation gave None to waiters

    def test_model_upcoming_logic(self):
        self.assertTrue(Title("tt1", "X", year=NEXT_YEAR).is_upcoming)
        self.assertFalse(Title("tt1", "X", year=1999).is_upcoming)
        self.assertTrue(Movie(status="Post Production").is_upcoming)
        self.assertTrue(Movie(release_date=f"{NEXT_YEAR}-01-01").is_upcoming)


if __name__ == "__main__":
    unittest.main()
