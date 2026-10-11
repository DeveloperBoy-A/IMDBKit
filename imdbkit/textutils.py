"""
Text intelligence helpers for IMDBKit.

* Devanagari -> Roman transliteration (with correct inherent-vowel handling)
* Normalisation + phonetic keys (Roman-Hindi spelling tolerant)
* Query / file-name parser  ("Panchayat.S03E02.1080p.WEB-DL" -> title, season, episode)
* Spell-variant generators used by the search pipeline
"""

import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

# Years further than this in the future are treated as part of the title
# (e.g. "Blade Runner 2049") instead of a release year.
MAX_FUTURE_YEARS = 6


def current_year() -> int:
    return date.today().year


# ============================================================
# Devanagari -> Roman
# ============================================================

_DEV_RE = re.compile(r"[\u0900-\u097F]")

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "\ue000": "gy",  # placeholder for ज्ञ
}

_VOWELS_SHORT = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}
_VOWELS_LONG = dict(_VOWELS_SHORT, **{"आ": "aa", "ई": "ee", "ऊ": "oo"})

_MATRAS_SHORT = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
_MATRAS_LONG = dict(_MATRAS_SHORT, **{"ा": "aa", "ी": "ee", "ू": "oo"})

_DIGITS = {c: str(i) for i, c in enumerate("०१२३४५६७८९")}
_NUKTA = "\u093c"
_VIRAMA = "\u094d"


def has_devanagari(text: Any) -> bool:
    return bool(text) and bool(_DEV_RE.search(str(text)))


def devanagari_to_roman(text: Any, long_vowels: bool = False) -> str:
    """
    Phonetic Devanagari -> Roman conversion.

    Handles matras, virama (conjuncts) and the inherent 'a' of consonants
    (dropped at the end of a word), so  पठान -> pathan, दंगल -> dangal,
    स्त्री -> stri (or stree with long_vowels=True).
    """
    if not text:
        return ""
    text = str(text)
    if not _DEV_RE.search(text):
        return text

    text = unicodedata.normalize("NFC", text).replace("ज्ञ", "\ue000")
    vowels = _VOWELS_LONG if long_vowels else _VOWELS_SHORT
    matras = _MATRAS_LONG if long_vowels else _MATRAS_SHORT

    out: List[str] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        if ch in _CONSONANTS:
            out.append(_CONSONANTS[ch])
            j = i + 1
            while j < n and text[j] == _NUKTA:
                j += 1
            nxt = text[j] if j < n else ""

            if nxt in matras:
                out.append(matras[nxt])
                i = j + 1
            elif nxt == _VIRAMA:
                i = j + 1
            else:
                # inherent 'a' unless the consonant ends the word
                if nxt and "\u0900" <= nxt <= "\u097f" and nxt not in ("।", "॥"):
                    out.append("a")
                i = j
            continue

        if ch in vowels:
            out.append(vowels[ch])
        elif ch in ("ं", "ँ"):
            out.append("n")
        elif ch == "ः":
            out.append("h")
        elif ch in _DIGITS:
            out.append(_DIGITS[ch])
        elif ch in ("।", "॥"):
            out.append(" ")
        elif ch in (_NUKTA, "ऽ", _VIRAMA) or ch in matras:
            pass
        else:
            out.append(ch)
        i += 1

    return "".join(out)


# ============================================================
# Normalisation / phonetics
# ============================================================

def ascii_fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_text(value: Any) -> str:
    """Lowercase ASCII-ish, punctuation-free text used for comparisons."""
    if value is None:
        return ""
    text = devanagari_to_roman(str(value))
    text = ascii_fold(unicodedata.normalize("NFKC", text)).lower()
    text = text.replace("&", " and ").replace("@", " at ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")


def strip_article(norm: str) -> str:
    stripped = _ARTICLE_RE.sub("", norm)
    return stripped or norm


_PHONETIC_RULES = [
    ("ph", "f"), ("kh", "k"), ("gh", "g"), ("bh", "b"), ("dh", "d"),
    ("th", "t"), ("sh", "s"), ("ch", "c"), ("ck", "k"), ("q", "k"),
    ("w", "v"), ("z", "j"), ("x", "ks"),
    ("aa", "a"), ("ee", "i"), ("oo", "u"), ("ou", "u"),
    ("ay", "e"), ("ai", "e"), ("ey", "e"), ("y", "i"),
]


def phonetic_key(value: Any) -> str:
    """
    Compact phonetic key that is tolerant of Roman-Hindi spelling variance:
    Pathaan/Pathan, Jawan/Jawaan, Mirzapur/Mirjapur, Sholay/Shole ...
    Only ever used for *comparison*, never shown to users.
    """
    norm = normalize_text(value)
    if not norm:
        return ""
    keys = []
    for word in norm.split():
        for src, dst in _PHONETIC_RULES:
            word = word.replace(src, dst)
        word = re.sub(r"(.)\1+", r"\1", word)
        keys.append(word)
    return "".join(keys)


# ============================================================
# Query / file-name parser
# ============================================================

_SEP_RE = re.compile(r"[._]+")
_BRACKET_YEAR_RE = re.compile(r"[\(\[]\s*((?:18|19|20)\d{2})\s*[\)\]]")
_YEAR_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])((?:18|19|20)\d{2})(?![A-Za-z0-9])")

_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"

_JUNK_RE = re.compile(
    _B + r"(?:"
    + "|".join(
        [
            r"\d{3,4}p", r"\d{3,4}x\d{3,4}", r"4k", r"8k", r"uhd", r"hdr10\+?", r"hdr",
            r"10[ -]?bit", r"8[ -]?bit", r"[xh]26[45]", r"hevc", r"av1", r"xvid", r"divx",
            r"web[ -]?dl", r"web[ -]?rip", r"blu[ -]?ray", r"bd[ -]?rip", r"br[ -]?rip",
            r"dvd[ -]?rip", r"dvd[ -]?scr", r"hd[ -]?rip", r"hdtv", r"hd[ -]?cam",
            r"cam[ -]?rip", r"tele[ -]?sync", r"aac", r"ac3", r"dd\+?5", r"ddp5", r"atmos",
            r"dts", r"proper", r"repack", r"dual[ -]?audio", r"multi[ -]?audio",
            r"[em]subs?", r"\d+(?:\.\d+)?[ ]?[gm]b",
        ]
    )
    + r")" + _E,
    re.I,
)

_SE_COMBINED = [
    re.compile(_B + r"S(\d{1,3})[ \-]?E(\d{1,4})(?:[ \-]?E\d{1,4})*" + _E, re.I),
    re.compile(_B + r"(\d{1,2})x(\d{2,3})" + _E, re.I),
    re.compile(
        _B + r"Season[ \-]*(\d{1,3})[ \-,]*(?:Episode|Ep)[ \-]*(\d{1,4})" + _E, re.I
    ),
]
_SEASON_RE = re.compile(
    _B + r"(?:Season|Sezon|Saison)[ \-]*(\d{1,3})" + _E + r"|" + _B + r"S(\d{1,3})" + _E,
    re.I,
)
_EPISODE_RE = re.compile(
    _B + r"(?:Episode|Eps?)[ \-]*(\d{1,4})" + _E + r"|" + _B + r"E(\d{1,4})" + _E,
    re.I,
)

_SERIES_NOISE = {"series", "season", "seasons", "episode", "episodes", "tv", "complete"}
_MOVIE_NOISE = {"movie", "film"}
_OTHER_NOISE = {
    "full", "web", "download", "watch", "online", "free", "hd", "trailer", "new",
    "latest", "hindi", "dubbed", "dub", "english", "tamil", "telugu", "malayalam",
    "kannada", "punjabi", "bengali", "marathi", "dual", "audio", "subbed", "sub",
    "subtitles", "esub", "esubs", "all",
}
_TRAIL_NOISE = _SERIES_NOISE | _MOVIE_NOISE | _OTHER_NOISE


def _tidy_keep(text: str) -> str:
    """Like _tidy but keeps '-', '.', ':' and '&' (IMDb needs 'T-170', 'Mr. Bachchan')."""
    text = re.sub(r"[^\w'\-.:&]+", " ", text, flags=re.UNICODE).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" -.:")
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"[^\w']+", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse(raw: str, long_vowels: bool) -> Dict[str, Any]:
    text = unicodedata.normalize("NFKC", devanagari_to_roman(raw, long_vowels))
    text = _SEP_RE.sub(" ", text)

    season: Optional[int] = None
    episode: Optional[int] = None
    spans = []

    for rx in _SE_COMBINED:
        m = rx.search(text)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            spans.append(m.span())
            break

    if season is None:
        m = _SEASON_RE.search(text)
        if m:
            season = int(m.group(1) or m.group(2))
            spans.append(m.span())

    if episode is None:
        m = _EPISODE_RE.search(text)
        if m:
            episode = int(m.group(1) or m.group(2))
            spans.append(m.span())

    cut = len(text)
    for start, end in spans:
        if start == 0 or not text[:start].strip():
            text = text[:start] + " " * (end - start) + text[end:]
        else:
            cut = min(cut, start)

    junk = _JUNK_RE.search(text)
    if junk and text[: junk.start()].strip():
        cut = min(cut, junk.start())

    head = text[:cut]

    # ---- year ----
    year: Optional[int] = None
    title_with_year: Optional[str] = None
    limit = current_year() + MAX_FUTURE_YEARS

    brackets = list(_BRACKET_YEAR_RE.finditer(head))
    if brackets and head[: brackets[-1].start()].strip():
        m = brackets[-1]
        year = int(m.group(1))
        head = head[: m.start()]
    else:
        candidates = [
            m for m in _YEAR_TOKEN_RE.finditer(head)
            if head[: m.start()].strip() and int(m.group(1)) <= limit
        ]
        if candidates:
            m = candidates[-1]
            year = int(m.group(1))
            before = head[: m.start()]
            title_with_year = _tidy(before + " " + m.group(1))
            head = before

    loose = _tidy(head)

    # ---- noise words at the end ("... full movie", "... hindi dubbed") ----
    tokens = loose.split()
    kind_hint: Optional[str] = None
    while len(tokens) > 1 and tokens[-1].lower() in _TRAIL_NOISE:
        word = tokens.pop().lower()
        if word in _SERIES_NOISE:
            kind_hint = "series"
        elif word in _MOVIE_NOISE and kind_hint is None:
            kind_hint = "movie"

    if season is not None or episode is not None:
        kind_hint = "series"

    punct_tokens = _tidy_keep(head).split()
    while len(punct_tokens) > 1 and re.sub(r"[^a-z0-9]", "", punct_tokens[-1].lower()) in _TRAIL_NOISE:
        punct_tokens.pop()

    return {
        "raw": raw,
        "title_punct": " ".join(punct_tokens),
        "title": " ".join(tokens),
        "title_loose": loose,
        "title_with_year": title_with_year,
        "year": year,
        "season": season,
        "episode": episode,
        "is_series": kind_hint == "series",
        "kind_hint": kind_hint,
        "alt_titles": [],
    }


def parse_media_query(value: Any) -> Dict[str, Any]:
    """
    Parse a user query or a release file name.

    >>> parse_media_query("Panchayat.S03E02.1080p.WEB-DL.x264")["season"]
    3
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return {
            "raw": "", "title": "", "title_punct": "", "title_loose": "", "title_with_year": None,
            "year": None, "season": None, "episode": None, "is_series": False,
            "kind_hint": None, "alt_titles": [],
        }

    parsed = _parse(raw, long_vowels=False)

    if has_devanagari(raw):
        alt = _parse(raw, long_vowels=True)["title"]
        if alt and alt != parsed["title"]:
            parsed["alt_titles"].append(alt)

    return parsed


# ============================================================
# Spell variants
# ============================================================

_RESPELL_RULES = [
    (r"(?<=[^aeiou\s])an\b", "aan"),
    (r"w", "v"),
    (r"v", "w"),
    (r"ee", "i"),
    (r"oo", "u"),
    (r"aa", "a"),
    (r"ph", "f"),
    (r"z", "j"),
    (r"j", "z"),
    (r"([a-z])\1", r"\1"),
]


def respell_variants(norm: str) -> List[str]:
    """Single-rule Roman-Hindi respellings of an already normalised query."""
    seen = {norm}
    out: List[str] = []
    for pattern, repl in _RESPELL_RULES:
        candidate = re.sub(pattern, repl, norm)
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def prefix_variants(norm: str) -> List[str]:
    """Prefix-style queries (IMDb suggestions are prefix based)."""
    if not norm:
        return []
    words = norm.split()
    out: List[str] = []

    if len(norm) >= 6:
        cut = max(4, int(len(norm) * 0.6))
        out.append(norm[:cut].strip())

    if len(words) > 1 and len(words[0]) >= 3:
        out.append(words[0])

    longest = max(words, key=len)
    if len(longest) >= 4:
        out.append(longest)
        if len(longest) >= 6:
            out.append(longest[: max(4, int(len(longest) * 0.65))])

    seen = set()
    result = []
    for item in out:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def word_variants(norm: str) -> List[str]:
    """
    Extra retrieval strings for a query that found nothing useful: every word on its
    own, first two words, last two words and 'mr bach'-style word + prefix.
    (IMDb suggestions are prefix based, so a typo late in the title kills the lookup.)
    """
    words = [w for w in (norm or "").split() if w]
    out: List[str] = []
    if len(words) > 1:
        out.append(" ".join(words[:2]))
        out.append(" ".join(words[-2:]))
        if len(words[1]) >= 4:
            out.append(words[0] + " " + words[1][:3])
    for w in words:
        if len(w) >= 3 and not w.isdigit():
            out.append(w)
            if len(w) >= 6:
                out.append(w[: max(3, len(w) // 2)])
    if words and words[0] not in out:
        out.append(words[0])           # very short titles ("T-170"): still try the first word
    if len(words) > 1 and "".join(words) not in out and len("".join(words)) <= 20:
        out.append("".join(words))
    seen, result = set(), []
    for item in out:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def build_primary_queries(parsed: Dict[str, Any], limit: int = 6) -> List[str]:
    """First-wave search strings for a parsed query."""
    queries: List[str] = []

    def add(value: Optional[str]):
        if not value:
            return
        q = normalize_text(value) or str(value).strip().lower()
        if q and q not in queries:
            queries.append(q)

    punct = (parsed.get("title_punct") or "").strip().lower()
    if punct and re.search(r"[-.:&]", punct):
        queries.append(punct)          # "t-170", "mr. bachchan", "spider-man"

    add(parsed.get("title"))
    add(parsed.get("title_loose"))
    add(parsed.get("title_with_year"))
    for alt in parsed.get("alt_titles", []):
        add(alt)

    main = normalize_text(parsed.get("title")) or normalize_text(parsed.get("title_loose"))
    if main:
        if " " in main and len(main.replace(" ", "")) <= 20:
            add(main.replace(" ", ""))
        for variant in respell_variants(main)[:3]:
            add(variant)

    return queries[:limit]
