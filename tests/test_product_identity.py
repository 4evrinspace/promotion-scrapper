import json
import unittest
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from normalizer.product_identity import (
    CONFIRMED_ALIASES_KEY,
    GTIN_TO_PRODUCT_KEY,
    PENDING_MATCHES_KEY,
    SOURCE_TO_PRODUCT_KEY,
    extract_attributes,
    get_gtin,
    find_candidates,
    make_alias_key,
    make_source_key,
    resolve_identity,
    save_product,
)
from normalizer.worker import normalize_product, price
from scrapers.base import raw_product
from services.clickhouse_sink import make_row
from scripts.catalog_loader import (
    load_catalog,
    parse_item,
    replace_catalog,
    validate_catalog,
)


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    def hget(self, key, field):
        if field is None:
            raise TypeError("Redis field cannot be None")
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return self.hashes.get(key, {}).copy()

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)
            self.sets.pop(key, None)

    def scan_iter(self, match):
        keys = set(self.hashes) | set(self.sets)
        return iter([key for key in keys if fnmatch(key, match)])

    def pipeline(self, transaction=True):
        return self

    def execute(self):
        return []

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    def smembers(self, key):
        return self.sets.get(key, set())


class ProductIdentityTest(unittest.TestCase):
    def test_gtin(self):
        self.assertEqual(get_gtin({"gtin_raw": "4006381333931"}), "04006381333931")
        self.assertEqual(get_gtin({"gtin_raw": "04006381333931"}), "04006381333931")
        self.assertIsNone(get_gtin({"gtin_raw": "4006381333932"}))
        self.assertIsNone(get_gtin({"gtin_raw": "code4006381333931"}))
        self.assertIsNone(get_gtin({"gtin_raw": "00000000"}))
        self.assertIsNone(get_gtin({"gtin_raw": "٤٠٠٦٣٨١٣٣٣٩٣١"}))

    def test_generic_measurements(self):
        data = extract_attributes(
            {"name_raw": "Power bank 10000 mAh, cable 20 cm", "brand_raw": "Test"}
        )
        self.assertIn({"value": 10000, "unit": "mah"}, data["measurements"])
        self.assertIn({"value": 200, "unit": "mm"}, data["measurements"])
        self.assertNotIn("sugar_free", data)
        self.assertNotIn("fat_percent", data)

        words = extract_attributes({"name_raw": "Cream 500 грамм, 2 штуки"})
        self.assertIn({"value": 500, "unit": "g"}, words["measurements"])
        self.assertIn({"value": 2, "unit": "pcs"}, words["measurements"])

        phone = extract_attributes({"name_raw": "Phone 5G 256GB"})
        self.assertNotIn({"value": 5, "unit": "g"}, phone["measurements"])
        self.assertIn({"value": 256, "unit": "gb"}, phone["measurements"])
        self.assertEqual(
            extract_attributes({"name_raw": "Motor oil 0W-20"})["measurements"],
            [],
        )
        with self.assertRaises(ValueError):
            extract_attributes({"name_raw": "Product 0x500ml"})

    def test_brand_accents_are_normalized(self):
        left = extract_attributes({"name_raw": "L’Oréal Cream", "brand_raw": "L’Oréal"})
        right = extract_attributes({"name_raw": "LOREAL Cream", "brand_raw": "LOREAL"})
        self.assertEqual(left["brand"], right["brand"])
        self.assertEqual(left["name"], right["name"])

    def test_confirmed_alias(self):
        cache = FakeRedis()
        product = self.product("prod_shampoo", "Shampoo Test Repair 250 ml")
        save_product(cache, product)
        alias = "Test Repair shampoo 250ml"
        cache.hset(CONFIRMED_ALIASES_KEY, make_alias_key("shop", alias), "prod_shampoo")

        result = resolve_identity(
            {"shop": "shop", "source_product_id": "1", "name_raw": alias}, cache
        )
        self.assertEqual(result.canonical_id, "prod_shampoo")
        self.assertEqual(result.match_method, "confirmed_alias")

    def test_match_is_not_category_specific(self):
        cache = FakeRedis()
        product = self.product(
            "prod_serum",
            "Face serum TestLab Hydra 30 ml",
            brand="TestLab",
            category="face_serum",
            model="HYDRA30",
        )
        save_product(cache, product)

        result = resolve_identity(
            {
                "shop": "other",
                "source_product_id": "2",
                "name_raw": "TestLab Hydra face serum 30ml",
                "brand_raw": "TestLab",
                "category_raw": "face_serum",
                "model_raw": "HYDRA30",
            },
            cache,
        )
        self.assertEqual(result.canonical_id, "prod_serum")
        self.assertEqual(result.match_method, "attribute_match")

    def test_product_can_be_matched_by_name_only(self):
        cache = FakeRedis()
        product = self.product(
            "prod_headphones",
            "Demo X1 Wireless Headphones",
        )
        save_product(cache, product)

        result = resolve_identity(
            {
                "shop": "other",
                "source_product_id": "name-only",
                "name_raw": "Wireless Headphones Demo X1",
            },
            cache,
        )
        self.assertEqual(result.canonical_id, "prod_headphones")
        self.assertEqual(result.match_method, "attribute_match")

    def test_short_name_only_match_needs_review(self):
        cache = FakeRedis()
        save_product(cache, self.product("prod_demo", "Demo Product"))

        result = resolve_identity(
            {
                "shop": "other",
                "source_product_id": "short-name",
                "name_raw": "Product Demo",
            },
            cache,
        )

        self.assertEqual(result.status, "review")
        self.assertNotEqual(result.canonical_id, "prod_demo")
        self.assertEqual(
            result.evidence["candidates"][0]["canonical_product_id"],
            "prod_demo",
        )

    def test_different_size_is_not_matched(self):
        cache = FakeRedis()
        save_product(
            cache,
            self.product("prod_perfume", "Perfume Test 50 ml", "Test", "perfume"),
        )
        result = resolve_identity(
            {
                "shop": "other",
                "source_product_id": "3",
                "name_raw": "Perfume Test 100 ml",
                "brand_raw": "Test",
                "category_raw": "perfume",
            },
            cache,
        )
        self.assertNotEqual(result.canonical_id, "prod_perfume")

    def test_fallback_is_not_confirmed(self):
        cache = FakeRedis()
        result = resolve_identity(
            {"shop": "shop", "source_product_id": "4", "name_raw": "Unknown item"},
            cache,
        )
        self.assertEqual(result.match_method, "provisional")
        self.assertFalse(cache.hashes.get(CONFIRMED_ALIASES_KEY))
        self.assertIn("shop:4", cache.hashes[PENDING_MATCHES_KEY])

    def test_provisional_product_is_not_a_candidate(self):
        cache = FakeRedis()
        first = resolve_identity(
            {
                "shop": "one",
                "source_product_id": "first",
                "name_raw": "Demo Product 50 ml",
            },
            cache,
        )
        second = resolve_identity(
            {
                "shop": "two",
                "source_product_id": "second",
                "name_raw": "Product Demo 50ml",
            },
            cache,
        )
        self.assertEqual(first.status, "provisional")
        self.assertEqual(second.status, "provisional")
        self.assertNotEqual(first.canonical_id, second.canonical_id)

    def test_repeated_provisional_is_not_confirmed(self):
        cache = FakeRedis()
        source = {
            "shop": "shop",
            "source_product_id": "unknown-1",
            "name_raw": "Unknown Product 50 ml",
        }
        first = resolve_identity(source, cache)
        second = resolve_identity(source, cache)
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertEqual(second.status, "provisional")
        self.assertIsNone(cache.hget(SOURCE_TO_PRODUCT_KEY, "shop:unknown-1"))

    def test_numeric_variant_is_not_matched(self):
        cache = FakeRedis()
        save_product(cache, self.product("prod_x1", "Brand Wireless Headphones X1"))
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "x2",
                "name_raw": "Brand Wireless Headphones X2",
            },
            cache,
        )
        self.assertNotEqual(result.canonical_id, "prod_x1")

    def test_model_separator_is_not_significant(self):
        cache = FakeRedis()
        product = self.product(
            "prod_x1", "Brand Headphones X-1", brand="Brand", model="X-1"
        )
        save_product(cache, product)
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "x1",
                "name_raw": "Brand Headphones X1",
                "brand_raw": "Brand",
                "model_raw": "X1",
            },
            cache,
        )
        self.assertEqual(result.canonical_id, "prod_x1")

    def test_plus_in_model_is_significant(self):
        cache = FakeRedis()
        save_product(
            cache,
            self.product("prod_s10", "Phone S10", brand="Brand", model="S10"),
        )
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "s10-plus",
                "name_raw": "Phone S10+",
                "brand_raw": "Brand",
                "model_raw": "S10+",
            },
            cache,
        )
        self.assertNotEqual(result.canonical_id, "prod_s10")

    def test_missing_category_still_generates_candidate(self):
        cache = FakeRedis()
        save_product(
            cache,
            self.product(
                "prod_shampoo", "Test Repair Shampoo 250 ml",
                brand="Test", category="shampoo",
            ),
        )
        attributes = extract_attributes(
            {
                "name_raw": "Test Shampoo for damaged hair 250 ml",
                "brand_raw": "Test",
            }
        )
        candidates = find_candidates(cache, attributes)
        self.assertEqual(candidates[0]["canonical_product_id"], "prod_shampoo")

    def test_token_block_works_when_source_has_no_structured_fields(self):
        cache = FakeRedis()
        save_product(
            cache,
            self.product(
                "prod_shampoo", "Test Repair Shampoo 250 ml",
                brand="Test", category="shampoo",
            ),
        )
        attributes = extract_attributes(
            {"name_raw": "Test Repairing Shampoo 250 ml"}
        )

        candidates = find_candidates(cache, attributes)

        self.assertEqual(candidates[0]["canonical_product_id"], "prod_shampoo")

    def test_mobile_network_name_is_not_product_weight(self):
        lower = extract_attributes({"name_raw": "Phone 5g 256GB"})
        upper = extract_attributes({"name_raw": "Phone 5G 256GB"})

        self.assertEqual(lower["measurements"], [{"value": 256.0, "unit": "gb"}])
        self.assertEqual(lower["measurements"], upper["measurements"])

    def test_each_measurement_is_checked(self):
        cache = FakeRedis()
        product = self.product("prod_device", "Device 50 ml cable 20 cm")
        save_product(cache, product)
        cache.hset(SOURCE_TO_PRODUCT_KEY, "shop:item", "prod_device")
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "item",
                "name_raw": "Device 50 ml cable 30 cm",
            },
            cache,
        )
        self.assertEqual(result.status, "quarantine")
        self.assertIn("measurement", result.evidence["conflicts"])

    def test_reused_source_id_goes_to_quarantine(self):
        cache = FakeRedis()
        save_product(cache, self.product("prod_old", "Shampoo Alpha"))
        cache.hset(SOURCE_TO_PRODUCT_KEY, "shop:42", "prod_old")
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "42",
                "name_raw": "Face Cream Omega",
            },
            cache,
        )
        self.assertEqual(result.status, "quarantine")
        self.assertIn("source_identity", result.evidence["conflicts"])

    def test_gtin_added_later_does_not_create_orphan(self):
        cache = FakeRedis()
        source = {
            "shop": "shop",
            "source_product_id": "item",
            "name_raw": "Demo Product",
        }
        first = resolve_identity(source, cache)
        source["gtin_raw"] = "4006381333931"
        second = resolve_identity(source, cache)
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertEqual(len(cache.hashes["canonical_products"]), 1)

    def test_missing_gtin_reuses_pending_product(self):
        cache = FakeRedis()
        source = {
            "shop": "shop",
            "source_product_id": "item",
            "name_raw": "Demo Product",
            "gtin_raw": "4006381333931",
        }
        first = resolve_identity(source, cache)
        source["gtin_raw"] = None
        second = resolve_identity(source, cache)
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertEqual(len(cache.hashes["canonical_products"]), 1)

    def test_reused_pending_source_goes_to_quarantine(self):
        cache = FakeRedis()
        resolve_identity(
            {"shop": "shop", "source_product_id": "42", "name_raw": "Alpha Shampoo"},
            cache,
        )
        result = resolve_identity(
            {"shop": "shop", "source_product_id": "42", "name_raw": "Omega Television"},
            cache,
        )
        self.assertEqual(result.status, "quarantine")

    def test_confirmed_product_removes_old_pending_card(self):
        cache = FakeRedis()
        source = {
            "shop": "shop",
            "source_product_id": "item",
            "name_raw": "Demo Product",
        }
        old = resolve_identity(source, cache)
        confirmed = self.product("prod_confirmed", "Confirmed Product")
        confirmed["gtins"] = ["04006381333931"]
        save_product(cache, confirmed)
        cache.hset(GTIN_TO_PRODUCT_KEY, "04006381333931", "prod_confirmed")
        source["gtin_raw"] = "4006381333931"

        result = resolve_identity(source, cache)

        self.assertEqual(result.canonical_id, "prod_confirmed")
        self.assertIsNone(cache.hget("canonical_products", old.canonical_id))
        self.assertIsNone(cache.hget(PENDING_MATCHES_KEY, "shop:item"))

    def test_pending_date_is_not_reset(self):
        cache = FakeRedis()
        source = {
            "shop": "shop",
            "source_product_id": "item",
            "name_raw": "Demo Product",
        }
        resolve_identity(source, cache)
        first = json.loads(cache.hget(PENDING_MATCHES_KEY, "shop:item"))["created_at"]
        resolve_identity(source, cache)
        second = json.loads(cache.hget(PENDING_MATCHES_KEY, "shop:item"))["created_at"]
        self.assertEqual(first, second)

    def test_equal_candidates_need_review(self):
        cache = FakeRedis()
        save_product(cache, self.product("prod_one", "Demo Product X1"))
        save_product(cache, self.product("prod_two", "Demo Product X1"))
        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "item",
                "name_raw": "Demo Product X1",
            },
            cache,
        )
        self.assertEqual(result.status, "review")

    def test_global_alias_key(self):
        self.assertTrue(make_alias_key("*", "Demo Product").startswith("*:"))

    def test_price_formats(self):
        self.assertEqual(price("199,99 ₽"), 19999)
        self.assertEqual(price("1 299 руб. 50 коп."), 129950)
        self.assertEqual(price("1 299 ₽ 50 коп."), 129950)
        self.assertEqual(price("199 руб. 5 коп."), 19905)
        self.assertEqual(price("129 99 ₽"), 12999)
        self.assertEqual(price("1 299 ₽"), 129900)
        self.assertEqual(price("199,99 руб -20%"), 19999)
        self.assertEqual(price("199 ₽ / 1 шт."), 19900)
        self.assertEqual(price("199 ₽ за 100 г"), 19900)
        self.assertIsNone(price("no price"))

    def test_normalized_message_has_only_required_data(self):
        cache = FakeRedis()
        product = raw_product(
            "shop", "https://shop.test/item/1", "Demo Product",
            "99,99 ₽", "149,99 ₽", {"sku": "1"},
        )
        product["collected_at"] = datetime.now(UTC).isoformat()
        result = normalize_product(product, cache)
        self.assertEqual(
            set(result),
            {
                "canonical_product_id", "name", "shop", "date",
                "original_price_kopecks", "promotion_price_kopecks",
                "match_status",
            },
        )
        self.assertEqual(result["match_status"], "provisional")

    def test_product_without_promotion_does_not_fill_redis(self):
        cache = FakeRedis()
        product = raw_product(
            "shop", "https://shop.test/item/1", "Demo Product",
            "149,99 ₽", "149,99 ₽", {"sku": "1"},
        )
        self.assertIsNone(normalize_product(product, cache))
        self.assertEqual(cache.hashes, {})

    def test_clickhouse_row(self):
        data = {
            "canonical_product_id": "prod_1",
            "name": "Demo Product",
            "shop": "shop",
            "date": datetime.now(UTC).isoformat(),
            "original_price_kopecks": 15000,
            "promotion_price_kopecks": 9999,
            "match_status": "confirmed",
        }
        row = make_row(data)
        self.assertEqual(str(row[4]), "150")
        self.assertEqual(str(row[5]), "99.99")

        data["original_price_kopecks"] = 1_000_000_000_000
        with self.assertRaises(ValueError):
            make_row(data)

    def test_catalog_is_loaded_as_source_of_truth(self):
        cache = FakeRedis()
        cache.hset(SOURCE_TO_PRODUCT_KEY, "shop:known", "prod_snaq_fabriq_milk_75g")
        cache.hset(SOURCE_TO_PRODUCT_KEY, "shop:old", "prod_removed")
        loaded = load_catalog(Path("catalog/catalog.jsonl"), cache)
        self.assertEqual(loaded, 1)
        self.assertIn(
            "prod_snaq_fabriq_milk_75g",
            cache.hashes["canonical_products"],
        )
        self.assertEqual(len(cache.hashes[CONFIRMED_ALIASES_KEY]), 1)
        self.assertEqual(
            cache.hget(SOURCE_TO_PRODUCT_KEY, "shop:known"),
            "prod_snaq_fabriq_milk_75g",
        )
        self.assertIsNone(cache.hget(SOURCE_TO_PRODUCT_KEY, "shop:old"))

    def test_catalog_reload_keeps_valid_runtime_gtins(self):
        cache = FakeRedis()
        runtime_products = [
            ("prod_runtime_one", "04006381333931", "provisional"),
            ("prod_runtime_two", "05901234123457", "review"),
            ("prod_runtime_three", "00000012345670", "quarantine"),
        ]
        for product_id, gtin, status in runtime_products:
            product = self.product(product_id, "Runtime " + product_id)
            product["gtins"] = [gtin]
            product["status"] = status
            save_product(cache, product)
            cache.hset(GTIN_TO_PRODUCT_KEY, gtin, product_id)
            cache.hset(
                PENDING_MATCHES_KEY,
                "shop:" + product_id,
                json.dumps(
                    {
                        "product_id": product_id,
                        "status": status,
                        "candidates": [],
                        "conflicts": [],
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )

        load_catalog(Path("catalog/catalog.jsonl"), cache)

        for product_id, gtin, status in runtime_products:
            self.assertEqual(cache.hget(GTIN_TO_PRODUCT_KEY, gtin), product_id)
            product = json.loads(cache.hget("canonical_products", product_id))
            self.assertEqual(product["status"], status)

    def test_catalog_gtin_has_priority_over_runtime_mapping(self):
        cache = FakeRedis()
        gtin = "04006381333931"
        runtime = self.product("prod_runtime", "Runtime Product")
        runtime["gtins"] = [gtin]
        runtime["status"] = "provisional"
        save_product(cache, runtime)
        cache.hset(GTIN_TO_PRODUCT_KEY, gtin, "prod_runtime")
        cache.hset(
            PENDING_MATCHES_KEY,
            "shop:item",
            json.dumps(
                {
                    "product_id": "prod_runtime",
                    "status": "provisional",
                    "candidates": [],
                }
            ),
        )
        entry = parse_item(
            {
                "canonical_product_id": "prod_catalog",
                "canonical_name": "Catalog Product",
                "gtins": [gtin],
            }
        )

        replace_catalog(cache, [entry])

        self.assertEqual(cache.hget(GTIN_TO_PRODUCT_KEY, gtin), "prod_catalog")
        self.assertIsNone(cache.hget("canonical_products", "prod_runtime"))
        self.assertIsNone(cache.hget(PENDING_MATCHES_KEY, "shop:item"))

    def test_catalog_reload_removes_orphan_gtin_mappings(self):
        cache = FakeRedis()
        cache.hset(GTIN_TO_PRODUCT_KEY, "04006381333931", "prod_missing")

        no_pending = self.product("prod_no_pending", "No Pending Product")
        no_pending["gtins"] = ["05901234123457"]
        no_pending["status"] = "provisional"
        save_product(cache, no_pending)
        cache.hset(
            GTIN_TO_PRODUCT_KEY, "05901234123457", "prod_no_pending"
        )

        wrong_gtin = self.product("prod_wrong_gtin", "Wrong GTIN Product")
        wrong_gtin["gtins"] = []
        wrong_gtin["status"] = "review"
        save_product(cache, wrong_gtin)
        cache.hset(GTIN_TO_PRODUCT_KEY, "00000012345670", "prod_wrong_gtin")
        cache.hset(
            PENDING_MATCHES_KEY,
            "shop:wrong",
            json.dumps(
                {
                    "product_id": "prod_wrong_gtin",
                    "status": "review",
                    "candidates": [],
                }
            ),
        )

        load_catalog(Path("catalog/catalog.jsonl"), cache)

        self.assertIsNone(cache.hget(GTIN_TO_PRODUCT_KEY, "04006381333931"))
        self.assertIsNone(cache.hget(GTIN_TO_PRODUCT_KEY, "05901234123457"))
        self.assertIsNone(cache.hget(GTIN_TO_PRODUCT_KEY, "00000012345670"))

    def test_catalog_alias_conflict_is_found_before_writes(self):
        first = parse_item(
            {
                "canonical_product_id": "prod_one",
                "canonical_name": "Product One",
                "aliases": ["Shared Name"],
            },
        )
        second = parse_item(
            {
                "canonical_product_id": "prod_two",
                "canonical_name": "Product Two",
                "aliases": ["Shared Name"],
            },
        )
        with self.assertRaises(ValueError):
            validate_catalog([first, second])

    def test_catalog_rejects_unknown_data(self):
        with self.assertRaises(ValueError):
            parse_item(
                {
                    "canonical_product_id": "prod_one",
                    "canonical_name": "Product",
                    "unused": "value",
                }
            )
        with self.assertRaises(ValueError):
            parse_item(
                {"canonical_product_id": "prod_punctuation", "canonical_name": "!!!"}
            )
        with self.assertRaises(ValueError):
            parse_item(
                {
                    "canonical_product_id": "prod_" + "x" * 96,
                    "canonical_name": "Product",
                }
            )
        with self.assertRaises(ValueError):
            parse_item(
                {
                    "canonical_product_id": "prod_one",
                    "canonical_name": "Product",
                    "gtins": [4006381333931],
                }
            )

    def test_name_source_key_is_normalized(self):
        first = {
            "shop": "shop",
            "source_product_id": "Demo Product",
            "source_id_is_stable": False,
            "name_raw": "Demo Product",
        }
        second = {
            "shop": "shop",
            "source_product_id": "demo  product",
            "source_id_is_stable": False,
            "name_raw": "demo  product",
        }
        self.assertEqual(make_source_key(first), make_source_key(second))

    def test_conflicting_ids_go_to_quarantine(self):
        cache = FakeRedis()
        save_product(cache, self.product("prod_a", "Product A"))
        save_product(cache, self.product("prod_b", "Product B"))
        cache.hset(GTIN_TO_PRODUCT_KEY, "04006381333931", "prod_a")
        cache.hset(SOURCE_TO_PRODUCT_KEY, "shop:5", "prod_b")

        result = resolve_identity(
            {
                "shop": "shop",
                "source_product_id": "5",
                "name_raw": "Product",
                "gtin_raw": "4006381333931",
            },
            cache,
        )
        self.assertEqual(result.status, "quarantine")
        self.assertIsNotNone(cache.hget(PENDING_MATCHES_KEY, "shop:5"))

    def product(self, product_id, name, brand="", category="", model=""):
        attributes = extract_attributes(
            {
                "name_raw": name,
                "brand_raw": brand,
                "category_raw": category,
                "model_raw": model,
            }
        )
        return {
            "canonical_product_id": product_id,
            "canonical_name": name,
            "attributes": attributes,
            "gtins": [],
            "status": "confirmed",
        }


if __name__ == "__main__":
    unittest.main()
