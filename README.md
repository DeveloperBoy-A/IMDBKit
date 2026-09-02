IMDBKit

Lightweight IMDb + TMDB metadata library for Python applications and Telegram bots.

Features

- IMDb title search
- Spell-check / fuzzy title matching
- Movie metadata
- TV series metadata
- IMDb ID support
- IMDb rating and votes
- Poster URL
- Cast and crew
- Genres
- Languages
- Countries
- Release date and year
- Runtime
- Box office information
- Series season information
- TMDB metadata support
- Legacy-compatible movie objects
- Dictionary-style metadata access

Installation

Install directly from GitHub:

pip install git+https://github.com/DeveloperBoy-A/IMDBKit.git@main

Basic Usage

from imdbkit import IMDBKit

imdb = IMDBKit()

results = imdb.search_movie("interstellar")

for movie in results.titles:
    print(movie.title)
    print(movie.imdb_id)
    print(movie.year)

movie = imdb.get_movie("tt0816692")

print(movie.title)
print(movie.year)
print(movie.rating)
print(movie.plot)
print(movie.cover_url)

TMDB Support

If a TMDB API key is available, pass it when creating the client:

from imdbkit import IMDBKit

imdb = IMDBKit(
    tmdb_api_key="YOUR_TMDB_API_KEY"
)

Alternatively, the library can read:

TMDB_API_KEY

from the environment.

Compatibility

The library is designed to work with existing code using:

from imdbkit import IMDBKit

imdb = IMDBKit()

Search results provide:

result.titles

Title objects provide:

title.imdb_id
title.movieID
title.title
title.year
title.kind
title.image_url

Movie objects provide commonly used metadata such as:

movie.title
movie.year
movie.imdb_id
movie.rating
movie.votes
movie.plot
movie.cover_url
movie.url
movie.genres
movie.languages
movie.countries
movie.directors
movie.writers
movie.producers
movie.stars
movie.info_series.display_seasons

Dictionary-style access is also supported:

movie.get("title")
movie.get("year")
movie.get("genres")
movie.get("cast")

License

MIT License
