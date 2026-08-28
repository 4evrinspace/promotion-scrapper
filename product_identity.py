
import hashlib
import json
import re
from difflib import SequenceMatcher


SYNONYMS_KEY = "product_name_synonyms"
PRODUCT_KEY_PREFIX = "catalog:product:"
TOKEN_KEY_PREFIX = "catalog:token:"
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
        product_id = cache.get("catalog:gtin:" + gtin)
        if product_id:
            return _load_identity(product_id, cache, "gtin", 1.0)

    normalized_name = normalize_text(product.get("name", ""))
    product_id = cache.hget(SYNONYMS_KEY, normalized_name)
    if product_id:
        return _load_identity(product_id, cache, "exact_alias", 0.99)

    candidate = _find_similar_product(normalized_name, cache)
    if candidate:
        return candidate

    identity = _build_fallback_identity(normalized_name)
    _save_fallback_synonym(normalized_name, identity, cache)
    return identity


def _load_identity(product_id, cache, method, confidence):
    product = json.loads(cache.get(f"{PRODUCT_KEY_PREFIX}{product_id}"))
    if product.get("is_fallback"):
        return ProductIdentity(
            product_id,
            product["canonical_name"],
            "fallback_cache",
            confidence=0.45,
        )
    return ProductIdentity(product_id, product["canonical_name"], method, confidence)


def _find_similar_product(name, cache):
    tokens = sorted(set(name.split()), key=len, reverse=True)[:4]
    candidate_ids = set()
    for token in tokens:
        candidate_ids.update(cache.smembers(f"{TOKEN_KEY_PREFIX}{token}"))
        if len(candidate_ids) >= 100:
            break

    best_id = ""
    best_score = 0.0
    for product_id in list(candidate_ids)[:100]:
        product = json.loads(cache.get(f"{PRODUCT_KEY_PREFIX}{product_id}"))
        if _measurements(name) != _measurements(product["search_name"]):
            continue
        a = " ".join(sorted(name.split()))
        b = " ".join(sorted(product["search_name"].split()))
        score = SequenceMatcher(None, a, b).ratio()
        if score > best_score:
            best_id, best_score = product_id, score

    if best_score < 0.93:
        return None
    return _load_identity(best_id, cache, "fuzzy_alias", best_score)


def _measurements(name):
    return tuple(
        token
        for token in name.split()
        if re.fullmatch(r"\d+(?:\.\d+)?(?:g|ml|pcs|pct)", token)
    )


def _build_fallback_identity(name):
    fingerprint = " ".join(sorted(set(name.split())))
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    return ProductIdentity(
        canonical_id=f"fallback:{digest}",
        canonical_name=name,
        match_method="fallback",
        confidence=0.45,
    )


def _save_fallback_synonym(shop_name, identity, cache):
    card = {
        "canonical_name": identity.canonical_name,
        "search_name": identity.canonical_name,
        "is_fallback": True,
    }
    pipeline = cache.pipeline()
    pipeline.set(
        f"{PRODUCT_KEY_PREFIX}{identity.canonical_id}",
        json.dumps(card, ensure_ascii=False),
        nx=True,
    )
    pipeline.hset(SYNONYMS_KEY, shop_name, identity.canonical_id)
    pipeline.execute()
