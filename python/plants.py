"""Profili delle piante: quanta acqua, quanta luce, quanto caldo sopportano.

Il modello di riconoscimento da' il nome scientifico (per esempio
"Fragaria x ananassa"). Il profilo si cerca cosi':
  1. nome esatto o alias (PROFILES, ALIASES)
  2. genere, cioe' la prima parola del nome (GENUS)
  3. altrimenti il profilo generico DEFAULT

I valori sono indicativi e vanno adattati alle colture reali. La luce minima
e' nella scala del fotoresistore (0 buio, 100 piena luce): va tarata guardando
il valore "Luce" sulla pagina web.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlantProfile:
    name: str
    light: str          # "sole", "mezzombra", "ombra"
    light_min_pct: int  # luce minima da garantire alla pianta (% letta dal fotoresistore)
    max_temp_c: float   # sopra questa temperatura la pianta va tenuta in ombra
    water_ml_day: int   # acqua al giorno per una pianta in vaso, con aria ne' secca ne' umida
    soil_min_pct: int   # sotto questa umidita' del terreno va innaffiata (se c'e' il sensore)


DEFAULT = PlantProfile("sconosciuta", "mezzombra", 30, 30.0, 300, 35)

PROFILES = {
    # ortaggi e frutti
    "pomodoro": PlantProfile("pomodoro", "sole", 45, 35.0, 800, 45),
    "peperone": PlantProfile("peperone", "sole", 45, 34.0, 600, 45),
    "basilico": PlantProfile("basilico", "sole", 45, 32.0, 350, 50),
    "fragola": PlantProfile("fragola", "mezzombra", 30, 28.0, 300, 50),
    "lattuga": PlantProfile("lattuga", "mezzombra", 30, 26.0, 250, 55),
    "spinacio": PlantProfile("spinacio", "ombra", 15, 24.0, 200, 55),
    "zucca": PlantProfile("zucca o zucchina", "sole", 45, 34.0, 900, 50),
    "carota": PlantProfile("carota", "sole", 45, 30.0, 350, 45),
    "melograno": PlantProfile("melograno", "sole", 45, 38.0, 500, 35),
    "legume": PlantProfile("leguminosa (trifoglio, lupino)", "sole", 45, 30.0, 300, 40),
    "cereale": PlantProfile("cereale o graminacea", "sole", 45, 34.0, 300, 35),
    # fiori e aromatiche
    "lavanda": PlantProfile("lavanda", "sole", 45, 38.0, 120, 25),
    "geranio": PlantProfile("geranio", "sole", 45, 34.0, 300, 40),
    "fiore": PlantProfile("fiore da pieno sole", "sole", 45, 32.0, 300, 40),
    # piante grasse, felci, piante da interno
    "succulenta": PlantProfile("pianta grassa", "sole", 45, 40.0, 40, 15),
    "felce": PlantProfile("felce", "ombra", 15, 26.0, 300, 60),
    "tropicale": PlantProfile("pianta tropicale da interno", "ombra", 15, 30.0, 150, 45),
    "orchidea": PlantProfile("orchidea", "mezzombra", 30, 30.0, 60, 30),
}

ALIASES = {
    "tomato": "pomodoro", "pepper": "peperone", "basil": "basilico",
    "strawberry": "fragola", "lettuce": "lattuga", "spinach": "spinacio",
}

# Genere botanico (minuscolo) -> profilo. Copre i generi del modello Pl@ntNet-300K
# che ha senso tenere in vaso sotto un pannello; gli altri usano DEFAULT.
GENUS = {
    "solanum": "pomodoro", "capsicum": "peperone", "ocimum": "basilico", "spinacia": "spinacio",
    "fragaria": "fragola", "duchesnea": "fragola",
    "lactuca": "lattuga",
    "cucurbita": "zucca", "lagenaria": "zucca", "luffa": "zucca",
    "daucus": "carota",
    "punica": "melograno",
    "trifolium": "legume", "lupinus": "legume", "melilotus": "legume", "crotalaria": "legume", "galega": "legume",
    "secale": "cereale", "phalaris": "cereale", "cenchrus": "cereale",
    "lavandula": "lavanda", "perovskia": "lavanda",
    "pelargonium": "geranio",
    "calendula": "fiore", "tagetes": "fiore", "papaver": "fiore", "alcea": "fiore", "anemone": "fiore",
    "freesia": "fiore", "kniphofia": "fiore", "dorotheanthus": "fiore", "hypericum": "fiore",
    "sedum": "succulenta", "phedimus": "succulenta", "lithops": "succulenta", "cereus": "succulenta",
    "pilosocereus": "succulenta", "selenicereus": "succulenta", "coryphantha": "succulenta",
    "stenocactus": "succulenta", "tephrocactus": "succulenta", "neobuxbaumia": "succulenta",
    "pereskia": "succulenta", "othonna": "succulenta", "cyphostemma": "succulenta",
    "dryopteris": "felce", "nephrolepis": "felce", "osmunda": "felce", "rumohra": "felce",
    "oreopteris": "felce", "anisocampium": "felce",
    "anthurium": "tropicale", "alocasia": "tropicale", "peperomia": "tropicale", "fittonia": "tropicale",
    "zamioculcas": "tropicale", "schefflera": "tropicale", "tradescantia": "tropicale",
    "aphelandra": "tropicale", "dischidia": "tropicale", "gynura": "tropicale", "nepenthes": "tropicale",
    "dendrobium": "orchidea", "vanilla": "orchidea", "ophrys": "orchidea", "epipactis": "orchidea",
    "ansellia": "orchidea", "erycina": "orchidea", "neotinea": "orchidea",
}


def profile_for(label: str | None) -> PlantProfile:
    key = (label or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in PROFILES:
        return PROFILES[key]
    genus = key.split(" ")[0] if key else ""
    return PROFILES.get(GENUS.get(genus, ""), DEFAULT)
