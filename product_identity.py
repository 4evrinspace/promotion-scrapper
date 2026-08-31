
import hashlib
import re


SYNONYMS_KEY = "product_name_synonyms"
MARKETING_WORDS = ["акция", "выгода", "новинка", "хит", "подарок", "экономия"]
MEASUREMENT_PATTERN = re.compile(
    r"(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>кг|г|л|мл|шт|%)(?![а-яa-z])",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(
    r"[а-яa-z]+|\d+(?:\.\d+)?(?:g|ml|pcs|pct)?", re.IGNORECASE
)


class ProductIdentity:
    def __init__(self, id, name, method, score):
        self.canonical_id = id
        self.canonical_name = name
        self.match_method = method
        self.confidence = score


def normalize_text(s):
    s = s.lower().replace("ё", "е")
    s = MEASUREMENT_PATTERN.sub(_normalize_measurement, s)
    data = []
    for x in WORD_PATTERN.findall(s):
        if x not in MARKETING_WORDS:
            data.append(x)
    return " ".join(data)


def _normalize_measurement(match):
    value = float(match["value"].replace(",", "."))
    unit = match["unit"].lower()
    if unit == "кг":
        value, normalized_unit = value * 1000, "g"
    elif unit == "л":
        value, normalized_unit = value * 1000, "ml"
    else:
        normalized_unit = {"г": "g", "мл": "ml", "шт": "pcs", "%": "pct"}[unit]
    return f" {value:g}{normalized_unit} "


def get_gtin(data):
    x = re.sub(r"\D", "", str(data.get("gtin", "")))
    if len(x) in [8, 12, 13, 14]:
        return x
    return None


def resolve_identity(product, cache):
    gtin = get_gtin(product)
    if gtin:
        name = cache.hget(SYNONYMS_KEY, "gtin:" + gtin)
        if name:
            return ProductIdentity("gtin:" + gtin, name, "gtin", 1.0)

    shop_name = normalize_text(product.get("name", ""))
    name = cache.hget(SYNONYMS_KEY, shop_name)
    if name:
        return ProductIdentity(make_id(name), name, "synonym", 0.99)

    cache.hset(SYNONYMS_KEY, shop_name, shop_name)
    return ProductIdentity(make_id(shop_name), shop_name, "fallback", 0.45)


def make_id(name):
    x = hashlib.sha256(name.encode()).hexdigest()[:16]
    return "name:" + x
