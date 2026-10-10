IMDBKit

IMDb + TMDB metadata library for Python apps and Telegram bots.

Features

- Spell-tolerant search: typos (intersteler), Roman-Hindi spellings (pathan / pathaan,
  mirjapur / mirzapur), Devanagari input (पठान), file names (Pushpa.2021.1080p.WEB-DL)
- "Did you mean" (result.corrected_query)
- Optional AI spell check hook (ai_corrector=your_llm_function)
- Only movies, web series, mini series and TV movies (no games, podcasts, shorts, episodes, people)
- Past and upcoming (future) titles, with is_upcoming / status
- Year in query is used to pick the right one ("dune 1984", "dune part two 2024")
- Poster (portrait) + backdrop (landscape) + extra posters/backdrops + logo + trailer
- Plot, runtime (runtime_text = "2h 49m"), genres, languages, countries, certificates,
  cast (with characters/photos), directors, writers, producers, composer, cinematographer
- Real IMDb rating/votes (from the IMDb page) with TMDB rating as fallback
- Series: seasons (S01), episodes (S01E01), per-episode plot/runtime/air date/still
- Works without a TMDB key (IMDb only) but backdrops and seasons/episodes need a free TMDB key

Install

pip install git+https://github.com/DeveloperBoy-A/IMDBKit.git@main

Usage

from imdbkit import IMDBKit

imdb = IMDBKit(tmdb_api_key="YOUR_TMDB_KEY")   # or env TMDB_API_KEY (v3 key or v4 token)

res = imdb.search_movie("intersteler")
print(res.corrected_query)          # Interstellar
for t in res:
    print(t.label)                  # Interstellar (2014) • Movie
    print(t.imdb_id, t.kind, t.year, t.image_url, t.is_upcoming)

movie = imdb.get_movie("tt0816692")
movie.title, movie.year, movie.plot, movie.runtime_text, movie.rating
movie.cover_url      # poster
movie.backdrop_url   # backdrop
movie.posters, movie.backdrops, movie.trailer_url

Series

show = imdb.get_movie("tt12004706")
for s in show.info_series.display_seasons:     # {"code": "S01", "episodes": 8, ...}
    print(s["code"], s["episodes"])

season = imdb.get_season("tt12004706", 3)
ep = imdb.get_episode("tt12004706", 3, 2)
print(ep.code, ep.title, ep.plot, ep.runtime_text, ep.still_url)    # S03E02 ...

One call

m = imdb.lookup("panchayat s03e02")    # series + m.episode_info
m = imdb.lookup("panchayat season 3")  # series + m.season_info

Buttons / autocomplete

imdb.suggestions("avngers", limit=6)

AI spell check (optional)

def fix(query):            # call any LLM here; return corrected title(s)
    return "Avengers"
imdb = IMDBKit(tmdb_api_key=KEY, ai_corrector=fix)   # only called if local correction fails

Compatibility

Existing code keeps working: search_movie(), get_movie(), result.titles, title.movieID,
movie.get("title"), movie.cover_url, movie.info_series.display_seasons, etc.

Tests

python -m unittest discover -s tests -v      (offline, uses a fake IMDb/TMDB backend)

License

MIT License
