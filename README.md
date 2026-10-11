
<div align="center">

<img src="assets/banner.svg" alt="TOKYO_UPDATES" width="100%"/>

# 𝐓ᴏᴋʏᴏ_𝐔ᴘᴅᴀᴛᴇꜱ · IMDBKit

**IMDb + TMDB metadata engine for Python apps & Telegram bots**

![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Version](https://img.shields.io/badge/version-1.2.0-ff2e63?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-8e44ad?style=for-the-badge)
![Tests](https://img.shields.io/badge/tests-48%20passing-2ecc71?style=for-the-badge)

[✨ Features](#-key-features) · [⚡ Install](#-install) · [🚀 Usage](#-quick-usage) · [🎯 Strict Match](#-strict-matching) · [💬 Support](#-support)

</div>

---

## ✨ Key Features

| | Feature | What it does |
|---|---|---|
| 🔎 | **Spell-tolerant search** | Typos (`intersteler`), Roman-Hindi (`pathan` = Pathaan), Devanagari (`पठान`), file names (`Pushpa.2021.1080p.WEB-DL`) |
| 🎯 | **Strict matching** | Every result is tagged `exact / phonetic / close / partial / weak`. Auto-correct only when it is the real title |
| 📅 | **Year smart** | Title decides, year only helps. Wrong year is detected and reported, never hides the right title |
| 🎬 | **Movies & Series only** | No games, podcasts, shorts, episodes or people. Past and upcoming titles |
| 🖼️ | **Posters & Backdrops** | Poster, backdrop, extra images, logo and trailer |
| 📺 | **Seasons & Episodes** | `S01`, `S01E01`, per-episode plot, runtime, air date, still |
| ⭐ | **Real IMDb rating** | IMDb rating and votes, TMDB as fallback |
| 📡 | **OTT coverage** | Netflix, JioHotstar, MX Player, Korean dramas via TMDB + TVMaze + Wikipedia |
| 💡 | **Suggestions** | Ready-made button labels: `Interstellar (2014) • Movie` |
| ⚡ | **Fast & safe** | Thread-safe, cached, request-coalescing |

---

## ⚡ Install

```bash
pip install git+https://github.com/DeveloperBoy-A/IMDBKit.git@main
```

## 🚀 Quick Usage

```python
from imdbkit import IMDBKit

imdb = IMDBKit(tmdb_api_key="YOUR_TMDB_KEY")   # or env TMDB_API_KEY

res = imdb.search_title("mr bachachhan 2024")
for t in res:
    print(t.label, t.match_type, t.year_match)  # Mr. Bachchan (2024) • Movie  close  exact

movie = imdb.get_movie("tt0816692")
print(movie.title, movie.rating, movie.runtime_text, movie.cover_url)

ep = imdb.get_episode("tt12004706", 3, 2)       # S03E02
```

## 🎯 Strict Matching

```python
res = imdb.search_title("the mongoose 2026")

res.auto_correct   # safe title to use without asking, else None
res.exact_titles   # titles that really are what the user typed
res.year_found     # False -> typed year matches nothing
res.year_note      # ready-made hint text
```

| Query | Result |
|---|---|
| `pathan` | ✅ auto-correct → **Pathaan** |
| `mr bachachhan` | 💡 suggestion → Mr. Bachchan |
| `the mongoose 2026` | 💡 suggestions only, never "The Mouse" |
| `full house with rohit s01 e07` | 💡 suggestions only, never plain "Full House" |
| `t-170 2026` | 💡 closest titles instead of "not found" |

> Auto-correct only when `res.auto_correct` is not `None`, otherwise show `res.titles` as suggestion buttons.

## 🧪 Tests

```bash
python -m unittest discover -s tests -v   # offline, fake IMDb/TMDB backend
```

## 💬 Support

Made with ❤️ by **𝐓ᴏᴋʏᴏ_𝐔ᴘᴅᴀᴛᴇꜱ**
📢 Updates & support: [t.me/YOUR_CHANNEL](https://t.me/YOUR_CHANNEL)

<div align="center">

**MIT License · © 𝐓ᴏᴋʏᴏ_𝐔ᴘᴅᴀᴛᴇꜱ**

</div>
