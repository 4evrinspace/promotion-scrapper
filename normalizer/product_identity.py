import hashlib
import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher


GTIN_TO_PRODUCT_KEY = "gtin_to_product"
SOURCE_TO_PRODUCT_KEY = "source_to_product"
CONFIRMED_ALIASES_KEY = "confirmed_aliases"
PENDING_MATCHES_KEY = "pending_matches"
CANONICAL_PRODUCTS_KEY = "canonical_products"
INDEX_PREFIX = "product_index"

AUTO_MATCH_SCORE = 0.88
REVIEW_SCORE = 0.68
MARKETING_WORDS = ["акция", "выгода", "новинка", "хит", "экономия"]
MEASUREMENT_PATTERN = re.compile(
    r"(?P<value>\d+(?:[,.]\d+)?)\s*(?P<unit>"
    r"fl\s*oz|oz|cl|кг|kg|мг|mg|грамм(?:а|ов)?|гр|г|g|"
    r"литр(?:а|ов)?|л|l|миллилитр(?:а|ов)?|мл|ml|"
    r"штук(?:а|и)?|шт|pcs|"
    r"мм|mm|см|cm|метр|м|meter|inch|дюйм|"
    r"мб|mb|гб|gb|тб|tb|вт|w|вольт|v|мач|mah|%)(?![а-яa-z])",
    re.IGNORECASE,
)
PACK_PATTERN = re.compile(
    r"(?P<count>\d+)\s*[xх×*]\s*(?P<value>\d+(?:[,.]\d+)?)\s*"
    r"(?P<unit>fl\s*oz|oz|cl|кг|kg|мг|mg|грамм(?:а|ов)?|гр|г|g|"
    r"литр(?:а|ов)?|л|l|миллилитр(?:а|ов)?|мл|ml|"
    r"штук(?:а|и)?|шт|pcs)(?![а-яa-z])",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(
    r"[а-яa-z]+\d+(?:[,.]\d+)?[а-яa-z]*(?:\+{1,2})?|"
    r"[а-яa-z]+\+{1,2}|"
    r"\d+(?:[,.]\d+)?[а-яa-z]*|[а-яa-z]+",
    re.IGNORECASE,
)


class ProductIdentity:
    def __init__(self, product_id, name, method, score, status, evidence=None):
        self.canonical_id = product_id
        self.canonical_name = name
        self.match_method = method
        self.confidence = score
        self.status = status
        self.evidence = evidence or {}


def normalize_text(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(_fold_latin_char(char) for char in text)
    text = text.lower().replace("ё", "е")
    text = re.sub(r"(?<=[а-яa-z])['’`](?=[а-яa-z])", "", text)
    text = re.sub(r"(?<=[а-яa-z])-(?=\d)", "", text)
    text = re.sub(r"(?<=\d)-(?=[а-яa-z])", "", text)
    text = MEASUREMENT_PATTERN.sub(_normalize_measurement, text)
    words = []
    for word in WORD_PATTERN.findall(text):
        if word not in MARKETING_WORDS:
            words.append(word)
    return " ".join(words)


def _fold_latin_char(char):
    if "LATIN" not in unicodedata.name(char, ""):
        return char
    value = unicodedata.normalize("NFKD", char)
    return "".join(part for part in value if not unicodedata.combining(part))


def _measurement(value, unit):
    value = float(str(value).replace(",", "."))
    if not math.isfinite(value) or value < 0:
        raise ValueError("Measurement must be non-negative and finite")
    unit = unit.lower()
    if unit in ["кг", "kg"]:
        value, unit = value * 1000, "g"
    elif unit in ["мг", "mg"]:
        value, unit = value / 1000, "g"
    elif unit in ["л", "l", "литр", "литра", "литров"]:
        value, unit = value * 1000, "ml"
    elif unit in ["г", "гр", "g", "грамм", "грамма", "граммов"]:
        unit = "g"
    elif unit in ["мл", "ml", "миллилитр", "миллилитра", "миллилитров"]:
        unit = "ml"
    elif unit in ["шт", "pcs", "штук", "штука", "штуки"]:
        unit = "pcs"
    elif unit == "cl":
        value, unit = value * 10, "ml"
    elif re.fullmatch(r"fl\s*oz", unit):
        value, unit = value * 29.5735, "ml"
    elif unit == "oz":
        value, unit = value * 28.3495, "g"
    elif unit in ["метр", "м", "meter"]:
        value, unit = value * 1000, "mm"
    elif unit in ["см", "cm"]:
        value, unit = value * 10, "mm"
    elif unit in ["дюйм", "inch"]:
        value, unit = value * 25.4, "mm"
    elif unit in ["мм", "mm"]:
        unit = "mm"
    elif unit in ["тб", "tb"]:
        value, unit = value * 1000, "gb"
    elif unit in ["мб", "mb"]:
        value, unit = value / 1000, "gb"
    elif unit in ["гб", "gb"]:
        unit = "gb"
    elif unit in ["вт", "w"]:
        unit = "w"
    elif unit in ["вольт", "v"]:
        unit = "v"
    elif unit in ["мач", "mah"]:
        unit = "mah"
    else:
        unit = "pct"
    if not math.isfinite(value):
        raise ValueError("Converted measurement must be finite")
    return value, unit


def _normalize_measurement(match):
    value, unit = _measurement(match["value"], match["unit"])
    return f" {value:g}{unit} "


def get_gtin(product):
    raw = str(product.get("gtin_raw", "") or "").strip()
    if not re.fullmatch(r"[0-9\s-]+", raw):
        return None
    value = re.sub(r"[\s-]", "", raw)
    if len(value) not in [8, 12, 13, 14]:
        return None
    if set(value) == {"0"}:
        return None

    total = 0
    for i, char in enumerate(reversed(value[:-1])):
        weight = 3 if i % 2 == 0 else 1
        total = total + int(char) * weight
    if (10 - total % 10) % 10 != int(value[-1]):
        return None
    return value.zfill(14)


def normalize_code(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(_fold_latin_char(char) for char in text)
    text = text.lower().replace("ё", "е")
    return re.sub(r"[^0-9а-яa-z+/]", "", text)


def extract_attributes(product):
    name = product.get("name_raw", "")
    model = product.get("model_raw", "")
    measurement_text = str(name) + " " + str(product.get("package_raw", ""))
    pack = PACK_PATTERN.search(measurement_text)
    pack_count = int(pack["count"]) if pack else None
    if pack_count == 0:
        raise ValueError("Pack count must be positive")

    measurements = []
    for item in MEASUREMENT_PATTERN.finditer(measurement_text):
        value, unit = _measurement(item["value"], item["unit"])
        compact_value = re.sub(r"\s+", "", item.group(0)).lower()
        if value == 0 or re.fullmatch(r"[2-6]g", compact_value):
            continue
        measurement = {"value": value, "unit": unit}
        if measurement not in measurements:
            measurements.append(measurement)
        if len(measurements) > 20:
            raise ValueError("Too many product measurements")

    return {
        "name": normalize_text(name),
        "brand": normalize_text(product.get("brand_raw", "")).replace(" ", ""),
        "category": normalize_text(product.get("category_raw", "")),
        "model": normalize_code(model),
        "variant": normalize_text(product.get("variant_raw", "")),
        "measurements": measurements,
        "pack_count": pack_count,
    }


def make_id(value):
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:16]
    return "prod_" + digest


def make_source_key(product):
    shop = str(product.get("shop", "unknown")).strip().lower()
    source_id = str(product.get("source_product_id", "")).strip()
    stable = product.get("source_id_is_stable", bool(source_id))
    if not stable or not source_id:
        source_id = normalize_text(product.get("name_raw", ""))
    return shop + ":" + source_id


def make_alias_key(shop, name):
    shop_key = "*" if shop == "*" else str(shop).strip().lower()
    return shop_key + ":" + normalize_text(name)


def _measurement_key(item):
    return f"{float(item['value']):g}{item['unit']}"


def _block_values(attributes):
    name = attributes.get("name", "")
    brand = attributes.get("brand", "")
    model = attributes.get("model", "")
    values = []

    if name:
        tokens = sorted(set(name.split()))
        values.append("name:" + ":".join(tokens))
    if brand and model:
        values.append("brand_model:" + brand + ":" + model)
    elif model:
        values.append("model:" + model)
    if brand:
        for item in attributes.get("measurements", []):
            values.append("brand_size:" + brand + ":" + _measurement_key(item))
    if name:
        tokens = sorted(name.split(), key=lambda x: (-len(x), x))
        long_tokens = [token for token in tokens if len(token) >= 5][:3]
        for token in long_tokens:
            values.append("token:" + token)
    return values


def _index_key(value):
    digest = hashlib.sha256(value.encode()).hexdigest()[:20]
    return INDEX_PREFIX + ":" + digest


def product_index_keys(attributes):
    return [_index_key(value) for value in _block_values(attributes)]


def save_product(cache, product):
    product_id = product["canonical_product_id"]
    old_value = cache.hget(CANONICAL_PRODUCTS_KEY, product_id)
    if old_value:
        try:
            old_product = json.loads(old_value)
        except Exception:
            old_product = None
        if isinstance(old_product, dict) and old_product.get("status") == "confirmed":
            for key in product_index_keys(old_product.get("attributes", {})):
                cache.srem(key, product_id)
    cache.hset(CANONICAL_PRODUCTS_KEY, product_id, json.dumps(product, ensure_ascii=False))
    if product.get("status") != "confirmed":
        return
    for key in product_index_keys(product.get("attributes", {})):
        cache.sadd(key, product_id)


def load_product(cache, product_id):
    if not product_id:
        return None
    value = cache.hget(CANONICAL_PRODUCTS_KEY, product_id)
    if not value:
        return None
    try:
        product = json.loads(value)
    except Exception:
        cache.hdel(CANONICAL_PRODUCTS_KEY, product_id)
        return None
    if not isinstance(product, dict):
        cache.hdel(CANONICAL_PRODUCTS_KEY, product_id)
        return None
    if product.get("canonical_product_id") != product_id:
        cache.hdel(CANONICAL_PRODUCTS_KEY, product_id)
        return None
    if (
        not isinstance(product.get("canonical_name"), str)
        or not isinstance(product.get("attributes"), dict)
        or not isinstance(product.get("gtins"), list)
        or product.get("status") not in ["confirmed", "provisional", "review", "quarantine"]
    ):
        cache.hdel(CANONICAL_PRODUCTS_KEY, product_id)
        return None
    return product


def text_similarity(left, right):
    left = normalize_text(left)
    right = normalize_text(right)
    if not left or not right:
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if left_tokens == right_tokens:
        return 1.0
    token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence_score = SequenceMatcher(None, left, right).ratio()
    return (token_score + sequence_score) / 2


def score_candidate(attributes, candidate, gtin=None):
    other = candidate.get("attributes", {})
    conflicts = []
    candidate_gtins = candidate.get("gtins", [])
    if gtin and candidate_gtins and gtin not in candidate_gtins:
        conflicts.append("gtin")

    for field in ["brand", "model", "variant", "pack_count"]:
        left = attributes.get(field)
        right = other.get(field)
        if left not in [None, ""] and right not in [None, ""] and left != right:
            conflicts.append(field)

    left_sizes = _measurements_by_unit(attributes.get("measurements", []))
    right_sizes = _measurements_by_unit(other.get("measurements", []))
    for unit in left_sizes.keys() & right_sizes.keys():
        if left_sizes[unit] != right_sizes[unit]:
            conflicts.append("measurement")
            break

    left_numbers = _number_tokens(attributes.get("name", ""))
    right_numbers = _number_tokens(other.get("name", ""))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        conflicts.append("numeric_token")
    if conflicts:
        return 0.0, {"conflicts": conflicts, "fields": []}

    fields = [
        ("brand", 0.25),
        ("category", 0.10),
        ("model", 0.25),
        ("variant", 0.15),
        ("pack_count", 0.05),
    ]
    score = 0.0
    weight = 0.0
    compared = []
    for field, field_weight in fields:
        left = attributes.get(field)
        right = other.get(field)
        if left in [None, ""] or right in [None, ""]:
            continue
        weight = weight + field_weight
        if left == right:
            score = score + field_weight
            compared.append(field)

    left_size_values = {_measurement_key(x) for x in attributes.get("measurements", [])}
    right_size_values = {_measurement_key(x) for x in other.get("measurements", [])}
    if left_size_values and right_size_values:
        size_score = len(left_size_values & right_size_values) / len(
            left_size_values | right_size_values
        )
        score = score + size_score * 0.20
        weight = weight + 0.20
        if size_score > 0:
            compared.append("measurements")

    title_score = text_similarity(attributes.get("name", ""), candidate["canonical_name"])
    score = score + title_score * 0.35
    weight = weight + 0.35
    name_tokens = set(attributes.get("name", "").split())
    other_tokens = set(normalize_text(candidate["canonical_name"]).split())
    return score / weight, {
        "conflicts": [],
        "fields": compared,
        "title_similarity": round(title_score, 4),
        "same_name_tokens": bool(name_tokens and name_tokens == other_tokens),
    }


def _measurements_by_unit(measurements):
    result = {}
    for item in measurements:
        result.setdefault(item["unit"], set()).add(float(item["value"]))
    return result


def _number_tokens(name):
    return {word for word in str(name).split() if any(char.isdigit() for char in word)}


def find_candidates(cache, attributes, gtin=None):
    product_ids = set()
    for value in _block_values(attributes):
        product_ids.update(cache.smembers(_index_key(value)))

    result = []
    for product_id in product_ids:
        product = load_product(cache, product_id)
        if not product or product.get("status") != "confirmed":
            continue
        score, evidence = score_candidate(attributes, product, gtin)
        result.append(
            {
                "canonical_product_id": product_id,
                "score": round(score, 4),
                "evidence": evidence,
            }
        )
    result.sort(key=lambda item: item["score"], reverse=True)
    return result[:3]


def _new_product(source, attributes, status, gtin=None):
    source_key = make_source_key(source)
    seed = "gtin:" + gtin if gtin else "source:" + source_key
    return {
        "canonical_product_id": make_id(seed),
        "canonical_name": " ".join(str(source.get("name_raw", "")).split()),
        "attributes": attributes,
        "gtins": [gtin] if gtin else [],
        "status": status,
    }


def _identity(product, method, score, status, evidence=None):
    return ProductIdentity(
        product["canonical_product_id"],
        product["canonical_name"],
        method,
        score,
        status,
        evidence,
    )


def _save_pending(
    cache, source_key, product_id, status, candidates=None, conflicts=None, source=None
):
    short_candidates = []
    for candidate in candidates or []:
        short_candidates.append(
            {
                "canonical_product_id": candidate["canonical_product_id"],
                "score": candidate.get("score", 0),
            }
        )
    created_at = datetime.now(UTC).isoformat()
    old_value = cache.hget(PENDING_MATCHES_KEY, source_key)
    if old_value:
        try:
            created_at = json.loads(old_value).get("created_at", created_at)
        except Exception:
            pass
    data = {
        "product_id": product_id,
        "status": status,
        "candidates": short_candidates,
        "conflicts": conflicts or [],
        "created_at": created_at,
    }
    if source:
        clean_source = {
            key: value for key, value in source.items() if value not in [None, "", {}]
        }
        if clean_source:
            data["source"] = clean_source
    cache.hset(PENDING_MATCHES_KEY, source_key, json.dumps(data, ensure_ascii=False))


def _attach_gtin(cache, product, gtin):
    gtins = product.get("gtins", [])
    if gtin not in gtins:
        gtins.append(gtin)
        product["gtins"] = gtins
        save_product(cache, product)
    cache.hset(GTIN_TO_PRODUCT_KEY, gtin, product["canonical_product_id"])


def _resolved(cache, source, product, method, score, evidence=None):
    status = "auto_matched" if method == "attribute_match" else "confirmed"
    source_key = make_source_key(source)
    if source.get("source_id_is_stable", True) and method != "attribute_match":
        cache.hset(SOURCE_TO_PRODUCT_KEY, source_key, product["canonical_product_id"])
    cache.hdel(PENDING_MATCHES_KEY, source_key)
    return _identity(product, method, score, status, evidence)


def _remove_old_pending_product(cache, source_key, pending_product, product_id):
    if not pending_product or pending_product["canonical_product_id"] == product_id:
        return
    pending_id = pending_product["canonical_product_id"]
    for key, value in cache.hgetall(PENDING_MATCHES_KEY).items():
        if key == source_key:
            continue
        try:
            if json.loads(value).get("product_id") == pending_id:
                return
        except Exception:
            continue
    cache.hdel(CANONICAL_PRODUCTS_KEY, pending_id)
    for value in pending_product.get("gtins", []):
        if cache.hget(GTIN_TO_PRODUCT_KEY, value) == pending_id:
            cache.hdel(GTIN_TO_PRODUCT_KEY, value)


def _quarantine(cache, source, attributes, ids, conflicts=None):
    source_key = make_source_key(source)
    product_id = next((value for value in ids.values() if value), None)
    product = load_product(cache, product_id)
    if not product:
        product = _new_product(source, attributes, "quarantine")
        save_product(cache, product)
    elif product.get("status") != "confirmed":
        product["status"] = "quarantine"
        save_product(cache, product)
    mapped_ids = {key: value for key, value in ids.items() if value}
    fields = conflicts or ["identity_mapping"]
    evidence = {"mapped_product_ids": mapped_ids, "conflicts": fields}
    pending_source = {
        "shop": source.get("shop", ""),
        "source_product_id": source.get("source_product_id", ""),
        "source_url": source.get("source_url", ""),
        "name_raw": source.get("name_raw", ""),
        "attributes": attributes,
    }
    _save_pending(
        cache,
        source_key,
        product["canonical_product_id"],
        "quarantine",
        conflicts=fields,
        source=pending_source,
    )
    return _identity(product, "quarantine", 0.0, "quarantine", evidence)


def resolve_identity(source, cache):
    attributes = extract_attributes(source)
    if not attributes["name"]:
        raise ValueError("Product name has no searchable characters")
    gtin = get_gtin(source)
    source_key = make_source_key(source)
    stable_id = source.get("source_id_is_stable", True)
    pending_product = None
    pending_value = cache.hget(PENDING_MATCHES_KEY, source_key)
    if pending_value:
        try:
            pending_data = json.loads(pending_value)
            if isinstance(pending_data, dict):
                pending_product = load_product(cache, pending_data.get("product_id"))
        except Exception:
            pending_product = None
        if not pending_product or pending_product.get("status") not in [
            "provisional", "review", "quarantine",
        ]:
            cache.hdel(PENDING_MATCHES_KEY, source_key)
            pending_product = None
    gtin_id = cache.hget(GTIN_TO_PRODUCT_KEY, gtin) if gtin else None
    source_id = cache.hget(SOURCE_TO_PRODUCT_KEY, source_key) if stable_id else None
    source_product = load_product(cache, source_id)
    if source_id and (
        not source_product or source_product.get("status") != "confirmed"
    ):
        cache.hdel(SOURCE_TO_PRODUCT_KEY, source_key)
        source_id = None
    local_alias_key = make_alias_key(
        source.get("shop", ""), source.get("name_raw", "")
    )
    global_alias_key = make_alias_key("*", source.get("name_raw", ""))
    alias_key = local_alias_key
    alias_id = cache.hget(CONFIRMED_ALIASES_KEY, local_alias_key)
    if not alias_id:
        alias_key = global_alias_key
        alias_id = cache.hget(CONFIRMED_ALIASES_KEY, global_alias_key)
    alias_product = load_product(cache, alias_id)
    if alias_id and (
        not alias_product or alias_product.get("status") != "confirmed"
    ):
        cache.hdel(CONFIRMED_ALIASES_KEY, alias_key)
        alias_id = None
    if not alias_id and alias_key == local_alias_key:
        alias_key = global_alias_key
        alias_id = cache.hget(CONFIRMED_ALIASES_KEY, global_alias_key)
        alias_product = load_product(cache, alias_id)
        if alias_id and (
            not alias_product or alias_product.get("status") != "confirmed"
        ):
            cache.hdel(CONFIRMED_ALIASES_KEY, alias_key)
            alias_id = None

    ids = {"gtin": gtin_id, "source": source_id, "alias": alias_id}
    if len(set(value for value in ids.values() if value)) > 1:
        return _quarantine(cache, source, attributes, ids, ["identity_mapping"])

    exact_id = gtin_id or source_id or alias_id
    exact_product = load_product(cache, exact_id)
    if exact_product:
        _, evidence = score_candidate(attributes, exact_product, gtin)
        if evidence["conflicts"]:
            return _quarantine(cache, source, attributes, ids, evidence["conflicts"])
        if exact_product.get("status") == "quarantine":
            return _quarantine(
                cache, source, attributes, ids, ["pending_quarantine"]
            )
        if source_id and not gtin_id and not alias_id:
            title_score = evidence.get("title_similarity", 0)
            strong_fields = set(evidence.get("fields", [])) & {
                "model", "variant", "measurements", "pack_count",
            }
            if title_score < 0.35 and not strong_fields:
                return _quarantine(
                    cache, source, attributes, ids, ["source_identity"]
                )
        if gtin:
            _attach_gtin(cache, exact_product, gtin)
        if gtin_id and exact_product.get("status") != "confirmed":
            _save_pending(
                cache,
                source_key,
                exact_product["canonical_product_id"],
                "provisional",
                source={"source_url": source.get("source_url", "")},
            )
            return _identity(exact_product, "gtin", 1.0, "provisional", evidence)
        method = "gtin" if gtin_id else "source_id" if source_id else "confirmed_alias"
        score = 1.0 if method != "confirmed_alias" else 0.99
        _remove_old_pending_product(
            cache, source_key, pending_product, exact_product["canonical_product_id"]
        )
        return _resolved(cache, source, exact_product, method, score, evidence)

    if pending_product:
        _, pending_evidence = score_candidate(attributes, pending_product, gtin)
        if pending_evidence["conflicts"]:
            return _quarantine(
                cache,
                source,
                attributes,
                {"pending": pending_product["canonical_product_id"]},
                pending_evidence["conflicts"],
            )
        if pending_product.get("status") == "quarantine":
            return _quarantine(
                cache,
                source,
                attributes,
                {"pending": pending_product["canonical_product_id"]},
                ["pending_quarantine"],
            )
        title_score = pending_evidence.get("title_similarity", 0)
        strong_fields = set(pending_evidence.get("fields", [])) & {
            "model", "variant", "measurements", "pack_count",
        }
        if stable_id and title_score < 0.35 and not strong_fields:
            return _quarantine(
                cache,
                source,
                attributes,
                {"pending": pending_product["canonical_product_id"]},
                ["source_identity"],
            )

    if gtin:
        if pending_product and pending_product.get("status") in ["provisional", "review"]:
            pending_product["canonical_name"] = " ".join(
                str(source.get("name_raw", "")).split()
            )
            pending_product["attributes"] = attributes
            gtins = pending_product.get("gtins", [])
            if gtin not in gtins:
                gtins.append(gtin)
            pending_product["gtins"] = gtins
            pending_product["status"] = "provisional"
            save_product(cache, pending_product)
            cache.hset(
                GTIN_TO_PRODUCT_KEY, gtin, pending_product["canonical_product_id"]
            )
            _save_pending(
                cache,
                source_key,
                pending_product["canonical_product_id"],
                "provisional",
                source={"source_url": source.get("source_url", "")},
            )
            return _identity(pending_product, "new_gtin", 0.95, "provisional")

        product = _new_product(source, attributes, "provisional", gtin)
        save_product(cache, product)
        cache.hset(GTIN_TO_PRODUCT_KEY, gtin, product["canonical_product_id"])
        _save_pending(
            cache,
            source_key,
            product["canonical_product_id"],
            "provisional",
            source={"source_url": source.get("source_url", "")},
        )
        return _identity(product, "new_gtin", 0.95, "provisional")

    candidate_gtin = gtin
    if not candidate_gtin and pending_product:
        pending_gtins = pending_product.get("gtins", [])
        if len(pending_gtins) == 1:
            candidate_gtin = pending_gtins[0]
    candidates = find_candidates(cache, attributes, candidate_gtin)
    best = candidates[0] if candidates else None
    enough = False
    if best:
        fields = best["evidence"]["fields"]
        has_attributes = len(fields) >= 2 and any(
            field in fields for field in ["brand", "model", "measurements"]
        )
        same_name = best["evidence"].get("same_name_tokens", False)
        enough = has_attributes or (same_name and len(attributes["name"].split()) >= 3)

    second = candidates[1] if len(candidates) > 1 else None
    ambiguous = second and best["score"] - second["score"] < 0.05

    if best and best["score"] >= AUTO_MATCH_SCORE and enough and not ambiguous:
        product = load_product(cache, best["canonical_product_id"])
        _remove_old_pending_product(
            cache, source_key, pending_product, product["canonical_product_id"]
        )
        return _resolved(
            cache, source, product, "attribute_match", best["score"], best["evidence"]
        )

    status = "review" if best and best["score"] >= REVIEW_SCORE else "provisional"
    if pending_product:
        product = pending_product
        product["canonical_name"] = " ".join(str(source.get("name_raw", "")).split())
        product["attributes"] = attributes
        product["status"] = status
    else:
        product = _new_product(source, attributes, status)
    save_product(cache, product)
    _save_pending(
        cache,
        source_key,
        product["canonical_product_id"],
        status,
        candidates,
        source={"source_url": source.get("source_url", "")},
    )
    score = best["score"] if best else 0.0
    return _identity(product, status, score, status, {"candidates": candidates})
