import json
import math
import os
import re
import sys
from pathlib import Path


from normalizer.product_identity import (
    CANONICAL_PRODUCTS_KEY,
    CONFIRMED_ALIASES_KEY,
    GTIN_TO_PRODUCT_KEY,
    INDEX_PREFIX,
    PENDING_MATCHES_KEY,
    SOURCE_TO_PRODUCT_KEY,
    extract_attributes,
    get_gtin,
    make_alias_key,
    make_id,
    product_index_keys,
)


REDIS_URL = os.getenv("REDIS_URL")


def parse_item(item):
    allowed_fields = {
        "canonical_product_id", "canonical_name", "gtins", "brand", "category",
        "model", "variant", "package", "attributes", "aliases",
    }
    unknown_fields = set(item) - allowed_fields
    if unknown_fields:
        raise ValueError("unknown fields: " + ", ".join(sorted(unknown_fields)))

    name = item.get("canonical_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("canonical_name is required")
    if len(name) > 1000:
        raise ValueError("canonical_name is too long")

    gtin_values = item.get("gtins", [])
    if not isinstance(gtin_values, list):
        raise ValueError("gtins must be a list")
    if len(gtin_values) > 20:
        raise ValueError("too many GTIN values")
    gtins = []
    for value in gtin_values:
        if not isinstance(value, str):
            raise ValueError("GTIN must be a string")
        gtin = get_gtin({"gtin_raw": value})
        if not gtin:
            raise ValueError("invalid GTIN: " + str(value))
        if gtin not in gtins:
            gtins.append(gtin)
    gtins.sort()

    product_id = item.get("canonical_product_id")
    if not product_id and len(gtins) == 1:
        product_id = make_id("gtin:" + gtins[0])
    if not product_id and len(gtins) > 1:
        raise ValueError("canonical_product_id is required for multiple GTIN values")
    if not isinstance(product_id, str) or not re.fullmatch(
        r"prod_[a-zA-Z0-9_-]{1,95}", product_id
    ):
        raise ValueError("canonical_product_id or a valid GTIN is required")

    raw = {
        "name_raw": name,
        "brand_raw": item.get("brand", ""),
        "category_raw": item.get("category", ""),
        "model_raw": item.get("model", ""),
        "variant_raw": item.get("variant", ""),
        "package_raw": item.get("package", ""),
    }
    if any(not isinstance(value, str) for value in raw.values()):
        raise ValueError("product text fields must be strings")
    for field, value in raw.items():
        limit = 1000 if field == "name_raw" else 500
        if len(value) > limit:
            raise ValueError(field + " is too long")
    attributes = extract_attributes(raw)
    if not attributes["name"]:
        raise ValueError("canonical_name has no searchable characters")
    extra_attributes = item.get("attributes", {})
    if not isinstance(extra_attributes, dict):
        raise ValueError("attributes must be an object")
    allowed = {"measurements", "pack_count"}
    unknown = set(extra_attributes) - allowed
    if unknown:
        raise ValueError("unknown attributes: " + ", ".join(sorted(unknown)))
    if "measurements" in extra_attributes:
        measurements = extra_attributes["measurements"]
        if not isinstance(measurements, list):
            raise ValueError("measurements must be a list")
        if len(measurements) > 20:
            raise ValueError("too many measurements")
        for measurement in measurements:
            if not isinstance(measurement, dict) or set(measurement) != {"value", "unit"}:
                raise ValueError("measurement must contain value and unit")
            value = measurement["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("measurement value must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError("measurement value must be positive and finite")
            if measurement["unit"] not in ["g", "ml", "pcs", "mm", "gb", "w", "v", "mah", "pct"]:
                raise ValueError("measurement unit is not supported")
    if "pack_count" in extra_attributes:
        count = extra_attributes["pack_count"]
        if count is not None and (
            isinstance(count, bool) or not isinstance(count, int) or count < 1
        ):
            raise ValueError("pack_count must be a positive integer or null")
    attributes.update(extra_attributes)

    aliases = {}
    alias_values = item.get("aliases", [])
    if not isinstance(alias_values, list):
        raise ValueError("aliases must be a list")
    if len(alias_values) > 100:
        raise ValueError("too many aliases")
    values = alias_values + [name]
    for alias in values:
        shop = "*"
        alias_name = alias
        if isinstance(alias, dict):
            if set(alias) != {"shop", "name"}:
                raise ValueError("alias object must contain shop and name")
            shop = alias.get("shop", "*")
            alias_name = alias.get("name", "")
        if not isinstance(shop, str) or not shop.strip():
            raise ValueError("alias shop must be a non-empty string")
        if not isinstance(alias_name, str) or not alias_name.strip():
            raise ValueError("alias name must be a non-empty string")
        if shop != "*" and not re.fullmatch(r"[a-z0-9_-]+", shop):
            raise ValueError("alias shop must be an identifier")
        if len(shop) > 50 or len(alias_name) > 1000:
            raise ValueError("alias value is too long")
        alias_key = make_alias_key(shop, alias_name)
        if alias_key.endswith(":"):
            raise ValueError("alias name has no searchable characters")
        aliases[alias_key] = product_id

    product = {
        "canonical_product_id": product_id,
        "canonical_name": name.strip(),
        "attributes": attributes,
        "gtins": gtins,
        "status": "confirmed",
    }
    return {"product": product, "aliases": aliases}


def read_catalog(file_path):
    entries = []
    with file_path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("catalog line must contain an object")
                entries.append(parse_item(item))
            except Exception as error:
                raise ValueError(f"Invalid catalog line {line_number}: {error}") from error
    return entries


def validate_catalog(entries):
    product_ids = set()
    aliases = {}
    gtins = {}
    for entry in entries:
        product = entry["product"]
        product_id = product["canonical_product_id"]
        if product_id in product_ids:
            raise ValueError("Duplicate canonical_product_id: " + product_id)
        product_ids.add(product_id)

        for alias_key, alias_product_id in entry["aliases"].items():
            old_id = aliases.get(alias_key)
            if old_id and old_id != alias_product_id:
                raise ValueError("Alias points to different products: " + alias_key)
            aliases[alias_key] = alias_product_id

        for gtin in product["gtins"]:
            old_id = gtins.get(gtin)
            if old_id and old_id != product_id:
                raise ValueError("GTIN points to different products: " + gtin)
            gtins[gtin] = product_id


def replace_catalog(cache, entries):
    product_ids = {entry["product"]["canonical_product_id"] for entry in entries}
    catalog_gtins = {}
    for entry in entries:
        product = entry["product"]
        for gtin in product["gtins"]:
            catalog_gtins[gtin] = product["canonical_product_id"]

    old_gtins = cache.hgetall(GTIN_TO_PRODUCT_KEY)
    old_products = cache.hgetall(CANONICAL_PRODUCTS_KEY)
    pending = cache.hgetall(PENDING_MATCHES_KEY)
    pipe = cache.pipeline(transaction=True)

    index_keys = list(cache.scan_iter(match=INDEX_PREFIX + ":*"))
    if index_keys:
        pipe.delete(*index_keys)
    pipe.delete(CONFIRMED_ALIASES_KEY, GTIN_TO_PRODUCT_KEY)

    for source_key, product_id in cache.hgetall(SOURCE_TO_PRODUCT_KEY).items():
        if product_id not in product_ids:
            pipe.hdel(SOURCE_TO_PRODUCT_KEY, source_key)

    valid_old_products = {}
    for product_id, value in old_products.items():
        try:
            product = json.loads(value)
        except Exception:
            pipe.hdel(CANONICAL_PRODUCTS_KEY, product_id)
            continue
        if not isinstance(product, dict):
            pipe.hdel(CANONICAL_PRODUCTS_KEY, product_id)
            continue
        valid_old_products[product_id] = product
        if product.get("status") == "confirmed" and product_id not in product_ids:
            pipe.hdel(CANONICAL_PRODUCTS_KEY, product_id)

    merge_ids = {}
    for entry in entries:
        product = entry["product"]
        product_id = product["canonical_product_id"]
        for gtin in product["gtins"]:
            old_id = old_gtins.get(gtin)
            old_product = valid_old_products.get(old_id)
            if old_product and old_id != product_id and old_product.get("status") != "confirmed":
                merge_ids[old_id] = product_id
                pipe.hdel(CANONICAL_PRODUCTS_KEY, old_id)

    pending_ids = set()
    runtime_ids = set()
    for source_key, value in pending.items():
        try:
            item = json.loads(value)
        except Exception:
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        if not isinstance(item, dict) or not isinstance(item.get("candidates", []), list):
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        pending_id = item.get("product_id")
        pending_product = valid_old_products.get(pending_id)
        if pending_id in product_ids:
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        if pending_id not in product_ids and not pending_product:
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        if pending_product and pending_product.get("status") == "confirmed" and pending_id not in product_ids:
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        if pending_id in merge_ids:
            pipe.hdel(PENDING_MATCHES_KEY, source_key)
            continue
        pending_ids.add(pending_id)

        product_status = pending_product.get("status")
        valid_runtime_product = (
            pending_product.get("canonical_product_id") == pending_id
            and isinstance(pending_product.get("canonical_name"), str)
            and bool(pending_product.get("canonical_name").strip())
            and isinstance(pending_product.get("attributes"), dict)
            and isinstance(pending_product.get("gtins"), list)
            and product_status in ["provisional", "review", "quarantine"]
        )
        if valid_runtime_product and item.get("status") == product_status:
            runtime_ids.add(pending_id)

        candidates = item.get("candidates", [])
        filtered = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("canonical_product_id") in product_ids:
                filtered.append(candidate)
        if filtered != candidates:
            item["candidates"] = filtered
            if item.get("status") == "review" and not filtered:
                item["status"] = "provisional"
                pending_product["status"] = "provisional"
                pipe.hset(
                    CANONICAL_PRODUCTS_KEY,
                    pending_id,
                    json.dumps(pending_product, ensure_ascii=False),
                )
            pipe.hset(
                PENDING_MATCHES_KEY,
                source_key,
                json.dumps(item, ensure_ascii=False),
            )

    for product_id, product in valid_old_products.items():
        if (
            product.get("status") in ["provisional", "review", "quarantine", "merged"]
            and product_id not in pending_ids
            and product_id not in merge_ids
        ):
            pipe.hdel(CANONICAL_PRODUCTS_KEY, product_id)

    for gtin, product_id in old_gtins.items():
        if gtin in catalog_gtins or product_id not in runtime_ids:
            continue
        product = valid_old_products.get(product_id)
        if not product or gtin not in product.get("gtins", []):
            continue
        if get_gtin({"gtin_raw": gtin}) != gtin:
            continue
        pipe.hset(GTIN_TO_PRODUCT_KEY, gtin, product_id)

    for entry in entries:
        product = entry["product"]
        product_id = product["canonical_product_id"]
        pipe.hset(
            CANONICAL_PRODUCTS_KEY,
            product_id,
            json.dumps(product, ensure_ascii=False),
        )
        for key in product_index_keys(product["attributes"]):
            pipe.sadd(key, product_id)
        for alias_key in entry["aliases"]:
            pipe.hset(CONFIRMED_ALIASES_KEY, alias_key, product_id)
        for gtin in product["gtins"]:
            pipe.hset(GTIN_TO_PRODUCT_KEY, gtin, product_id)

    pipe.execute()


def load_catalog(file_path, cache):
    entries = read_catalog(file_path)
    if not entries:
        raise ValueError("Catalog is empty")

    validate_catalog(entries)
    replace_catalog(cache, entries)
    print(f"Catalog load finished. Loaded: {len(entries)}")
    return len(entries)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 -m scripts.catalog_loader catalog.jsonl")
        sys.exit(1)

    import redis

    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        load_catalog(Path(sys.argv[1]), cache)
    except Exception as error:
        print(f"Catalog load failed: {error}")
        sys.exit(1)
