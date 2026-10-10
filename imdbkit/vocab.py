"""
Local title vocabulary used for spell correction.

IMDb's suggestion endpoint is prefix based, so a badly misspelled query
("avngers", "intersteler", "pathan") returns nothing.  We keep a vocabulary of
popular titles (seed list below) and of every title IMDBKit has already seen,
find the closest phonetic matches for a bad query and re-query IMDb with them.

The seed list is only a *hint* for correction.  Every title shown to the user,
including its year, poster, kind etc., always comes from IMDb / TMDB.
"""

import threading
from typing import Dict, Iterable, List

from rapidfuzz import fuzz, process

from .textutils import normalize_text, phonetic_key, strip_article

SEED_TITLES = (
    # --- Bollywood ---
    "Pathaan", "Jawan", "Animal", "Dunki", "Tiger 3", "Fighter", "Stree", "Stree 2",
    "Bhool Bhulaiyaa", "Bhool Bhulaiyaa 2", "Bhool Bhulaiyaa 3", "Singham Again",
    "Singham", "Simmba", "Sooryavanshi", "Dangal", "3 Idiots", "PK", "Sholay",
    "Dilwale Dulhania Le Jayenge", "Kabhi Khushi Kabhie Gham", "Kuch Kuch Hota Hai",
    "Kal Ho Naa Ho", "Lagaan", "Gadar: Ek Prem Katha", "Gadar 2", "Drishyam",
    "Drishyam 2", "Kabir Singh", "Sanju", "Chhichhore", "Super 30", "War", "Dhoom",
    "Dhoom 2", "Dhoom 3", "Krrish", "Krrish 3", "Koi... Mil Gaya", "Don", "Don 2",
    "Welcome", "Hera Pheri", "Phir Hera Pheri", "Golmaal", "Housefull", "Bajrangi Bhaijaan",
    "Sultan", "Tiger Zinda Hai", "Ek Tha Tiger", "Dabangg", "Wanted", "Bhaag Milkha Bhaag",
    "Barfi!", "Queen", "Tamasha", "Rockstar", "Yeh Jawaani Hai Deewani",
    "Zindagi Na Milegi Dobara", "Dil Chahta Hai", "Devdas", "Swades", "Rang De Basanti",
    "Taare Zameen Par", "Gully Boy", "Andhadhun", "Article 15", "Uri: The Surgical Strike",
    "Shershaah", "Kesari", "Raazi", "Badhaai Ho", "Bhediya", "Munjya", "Laapataa Ladies",
    "12th Fail", "Chhaava", "Saiyaara", "Brahmastra", "Rocky Aur Rani Kii Prem Kahaani",
    "Gangubai Kathiawadi", "Bajirao Mastani", "Padmaavat", "Kabir Singh", "Sooryavanshi",
    "Jolly LLB", "Jolly LLB 2", "Toilet: Ek Prem Katha", "Pad Man", "Airlift", "Baby",
    "Special 26", "A Wednesday", "Hindi Medium", "Angrezi Medium", "Dil Dhadakne Do",
    "Chak De! India", "Munna Bhai M.B.B.S.", "Lage Raho Munna Bhai", "Om Shanti Om",
    "Chennai Express", "Happy New Year", "Dilwale", "Raees", "Zero", "Kick", "Bodyguard",
    "Ready", "Race", "Tanu Weds Manu", "Badrinath Ki Dulhania", "Sonu Ke Titu Ki Sweety",
    "Pyaar Ka Punchnama", "Kapoor & Sons", "Piku", "Pink", "Neerja", "Mission Mangal",
    "Bell Bottom", "Ludo", "Thappad", "Mimi", "Dasvi", "Sam Bahadur", "Crew", "Kill",
    "Bade Miyan Chote Miyan", "Maidaan", "Sky Force", "Jigra", "Bhool Chuk Maaf",
    "Housefull 5", "Sitaare Zameen Par", "War 2", "Metro... In Dino",
    # --- South ---
    "Kantara", "Kantara: Chapter 1", "Vikram", "Leo", "Jailer", "Master", "Kaithi",
    "Ponniyin Selvan: I", "Ponniyin Selvan: II", "Salaar: Part 1 - Ceasefire", "Sita Ramam",
    "Hanu-Man", "Maharaja", "Lucky Baskhar", "Manjummel Boys", "Premalu", "Pushpa: The Rise",
    "Pushpa 2: The Rule", "K.G.F: Chapter 1", "K.G.F: Chapter 2", "RRR",
    "Baahubali: The Beginning", "Baahubali 2: The Conclusion", "Kalki 2898 AD",
    "Game Changer", "Devara: Part 1", "Coolie", "Thug Life", "Vettaiyan", "Ala Vaikunthapurramuloo",
    "Arjun Reddy", "Eega", "Magadheera", "Sarkaru Vaari Paata", "Vikram Vedha", "Soorarai Pottru",
    "Jai Bhim", "Asuran", "Karnan", "Bheeshma Parvam", "Aavesham",
    "Pushpa", "Bahubali", "Adipurush", "Radhe Shyam", "Saaho", "Ghajini", "Enthiran", "2.0",
    "Sivaji: The Boss", "Kabali", "Kaala", "Darbar", "Annaatthe", "Beast", "Varisu", "Bigil",
    "Mersal", "Sarkar", "Theri", "Kaththi", "Thunivu", "Valimai", "Viswasam", "Veeram",
    # --- Hollywood ---
    "Avengers: Endgame", "Avengers: Infinity War", "The Avengers", "Avengers: Age of Ultron",
    "Iron Man", "Thor: Ragnarok", "Thor: Love and Thunder", "Black Panther",
    "Black Panther: Wakanda Forever", "Spider-Man: No Way Home", "Spider-Man: Homecoming",
    "Spider-Man: Far from Home", "Spider-Man: Into the Spider-Verse",
    "Spider-Man: Across the Spider-Verse", "Doctor Strange", "Doctor Strange in the Multiverse of Madness",
    "Guardians of the Galaxy", "Deadpool", "Deadpool & Wolverine", "Joker", "The Dark Knight",
    "The Dark Knight Rises", "Batman Begins", "The Batman", "Inception", "Interstellar",
    "Oppenheimer", "Tenet", "Dunkirk", "The Prestige", "Memento", "Titanic", "Avatar",
    "Avatar: The Way of Water", "Avatar: Fire and Ash", "Jurassic Park", "Jurassic World",
    "The Lion King", "Frozen", "Frozen II", "Toy Story", "Finding Nemo", "Inside Out",
    "Inside Out 2", "Moana", "Coco", "Up", "Shrek", "Fast Five", "Furious 7",
    "Fast & Furious 6", "F9", "Fast X", "John Wick", "John Wick: Chapter 4",
    "Mission: Impossible - Dead Reckoning Part One", "Mission: Impossible - Fallout",
    "Top Gun: Maverick", "The Matrix", "Gladiator", "Gladiator II", "Dune", "Dune: Part Two",
    "The Godfather", "Pulp Fiction", "Fight Club", "Forrest Gump", "The Shawshank Redemption",
    "Schindler's List", "The Lord of the Rings: The Fellowship of the Ring",
    "The Lord of the Rings: The Two Towers", "The Lord of the Rings: The Return of the King",
    "Harry Potter and the Philosopher's Stone", "Harry Potter and the Deathly Hallows: Part 2",
    "Star Wars", "The Terminator", "Terminator 2: Judgment Day", "Jaws", "Barbie", "Wonka",
    "Wicked", "Venom", "Aquaman", "Shazam!", "Superman", "Man of Steel", "Wonder Woman",
    "Captain America: The Winter Soldier", "Captain America: Civil War", "Ant-Man",
    "Captain Marvel", "Eternals", "Shang-Chi and the Legend of the Ten Rings", "Sinners",
    "Parasite", "Oldboy", "Everything Everywhere All at Once", "Knives Out", "Glass Onion",
    "The Wolf of Wall Street", "Django Unchained", "Inglourious Basterds", "Kill Bill: Vol. 1",
    "The Revenant", "Shutter Island", "The Departed", "Se7en", "Gone Girl", "Whiplash",
    "La La Land", "The Social Network", "Mad Max: Fury Road", "Blade Runner 2049",
    "Gravity", "The Martian", "Arrival", "Edge of Tomorrow", "Transformers", "Kung Fu Panda",
    "Despicable Me", "Minions", "How to Train Your Dragon", "Ice Age", "Madagascar",
    "The Conjuring", "Insidious", "Annabelle", "It", "A Quiet Place", "Get Out", "Hereditary",
    "Midsommar", "The Exorcist", "Saw", "Final Destination", "Mr. & Mrs. Smith", "Ocean's Eleven",
    "The Hangover", "Home Alone", "Mean Girls", "The Notebook", "Pirates of the Caribbean",
    "The Pursuit of Happyness", "Catch Me If You Can", "Saving Private Ryan", "Braveheart",
    "The Green Mile", "Goodfellas", "Scarface", "Casino Royale", "Skyfall", "No Time to Die",
    "Rocky", "Rambo", "Die Hard", "Predator", "Alien", "Aliens", "Hobbs & Shaw",
    # --- Series (international) ---
    "Money Heist", "La Casa de Papel", "Breaking Bad", "Better Call Saul", "Game of Thrones",
    "House of the Dragon", "The Last of Us", "Stranger Things", "The Boys", "Peaky Blinders",
    "Narcos", "Dark", "Mindhunter", "Ozark", "Black Mirror", "The Witcher", "Wednesday",
    "Squid Game", "Friends", "The Office", "Sherlock", "Chernobyl", "True Detective",
    "Succession", "The Crown", "Lucifer", "The Umbrella Academy", "Loki", "WandaVision",
    "The Mandalorian", "Andor", "Arcane", "Attack on Titan", "Death Note", "One Piece",
    "Naruto", "Dragon Ball Z", "Demon Slayer: Kimetsu no Yaiba", "Jujutsu Kaisen",
    "The Big Bang Theory", "How I Met Your Mother", "Modern Family", "Vikings",
    "The Walking Dead", "Prison Break", "Dexter", "Westworld", "Fargo", "Severance",
    "Shogun", "The Bear", "Beef", "Baby Reindeer", "Adolescence", "The White Lotus",
    "Bridgerton", "Sex Education", "Elite", "Emily in Paris", "Virgin River",
    "Ginny & Georgia", "Lupin", "Rick and Morty", "The Simpsons", "Family Guy",
    "South Park", "Grey's Anatomy", "Suits", "House M.D.", "Brooklyn Nine-Nine",
    "Euphoria", "The Sopranos", "The Wire", "Band of Brothers", "Fallout",
    "Reacher", "Yellowstone", "The Penguin", "Daredevil: Born Again", "Ahsoka",
    "The Rings of Power", "Heartstopper", "The Gentlemen", "Slow Horses", "Mr. Robot",
    "Mirzapur", "Dark Matter", "Alice in Borderland", "Kingdom", "Sweet Home", "All of Us Are Dead",
    "Cobra Kai", "Outer Banks", "Money Heist: Korea - Joint Economic Area", "Vincenzo",
    "Crash Landing on You", "Hospital Playlist", "The Glory", "Moving",
    # --- Series (Indian) ---
    "Panchayat", "The Family Man", "Sacred Games", "Scam 1992: The Harshad Mehta Story",
    "Paatal Lok", "Asur", "Delhi Crime", "Kota Factory", "TVF Aspirants", "Gullak",
    "Farzi", "Criminal Justice", "Special Ops", "Made in Heaven",
    "Heeramandi: The Diamond Bazaar", "Rocket Boys", "Aarya", "Tandav", "Jamtara",
    "Mismatched", "Bandish Bandits", "Breathe", "Inside Edge", "Kohrra", "Poacher",
    "Dahaad", "Jubilee", "Bambai Meri Jaan", "The Railway Men", "Black Warrant", "Taskaree",
    "Choona", "Khauf", "Ray", "Human", "Aashram", "Bhaukaal", "Rangbaaz", "Illegal",
    "Hostel Daze", "Permanent Roommates", "Pitchers", "Yeh Meri Family", "Mumbai Diaries",
    "Undekhi", "Apharan", "Class", "Duranga", "Guilty Minds", "Lootere", "Maamla Legal Hai",
    "Taarak Mehta Ka Ooltah Chashmah", "The Great Indian Kapil Show", "Shark Tank India",
    "Bigg Boss", "Kaun Banega Crorepati", "Naagin", "Anupamaa", "Balika Vadhu",
    "Kyunki Saas Bhi Kabhi Bahu Thi", "CID", "Crime Patrol", "Mahabharat", "Ramayan",
)


class TitleVocabulary:
    """Thread-safe phonetic title vocabulary with fuzzy lookup."""

    def __init__(self, seed: Iterable[str] = (), max_size: int = 30000):
        self._lock = threading.Lock()
        self._max_size = max_size
        self._by_key: Dict[str, str] = {}
        self._keys: List[str] = []
        self.add_many(seed)

    @staticmethod
    def _key(title: str) -> str:
        return phonetic_key(strip_article(normalize_text(title)))

    def __len__(self) -> int:
        return len(self._keys)

    def add_many(self, titles: Iterable[str]) -> None:
        with self._lock:
            for title in titles:
                if not title:
                    continue
                key = self._key(title)
                if len(key) < 2 or key in self._by_key:
                    continue
                if len(self._keys) >= self._max_size:
                    return
                self._by_key[key] = title
                self._keys.append(key)

    def correct(self, query: str, limit: int = 3, cutoff: float = 80.0) -> List[str]:
        """Closest known titles for a (possibly misspelled) query."""
        qkey = self._key(query)
        if len(qkey) < 3:
            return []

        with self._lock:
            keys = list(self._keys)

        matches = process.extract(
            qkey, keys, scorer=fuzz.ratio, limit=limit, score_cutoff=cutoff
        )

        out: List[str] = []
        for match in matches:
            key = match[0]
            title = self._by_key.get(key)
            if title and title not in out:
                out.append(title)
        return out
