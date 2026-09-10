import json
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import urlsplit

metrics = None
PRODUCT_FIELDS = [
    "shop",
    "source_url",
    "source_product_id",
    "source_id_is_stable",
    "name_raw",
    "gtin_raw",
    "model_raw",
    "variant_raw",
    "brand_raw",
    "category_raw",
    "package_raw",
    "current_price_text",
    "old_price_text",
    "collected_at",
]


class StoreScraper(ABC):
    shop = ""
    base_url = ""

    def __init__(self):
        self.session = None

    def get_session(self):
        if self.session is None:
            import cloudscraper

            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            self.session.headers.update(
                {
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": self.base_url + "/",
                    "User-Agent": "GPTBot",
                }
            )
        return self.session

    @abstractmethod
    def scrape_products(self, limit=None):
        pass

    def start(self):
        run_scraper(self)


def get_metrics():
    global metrics
    if metrics is None:
        from prometheus_client import Counter, Gauge

        metrics = {
            "pages": Counter("scraper_pages_parsed_total", "", ["shop"]),
            "errors": Counter("scraper_errors_total", "", ["shop"]),
            "products": Counter("scraper_products_published_total", "", ["shop"]),
            "last_run": Gauge("scraper_last_successful_run_timestamp", "", ["shop"]),
        }
    return metrics


def page_parsed(shop):
    get_metrics()["pages"].labels(shop=shop).inc()


def scraper_error(shop):
    get_metrics()["errors"].labels(shop=shop).inc()


def value_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value") or value.get("amount") or value.get("text") or value.get("name")
    if isinstance(value, list):
        result = []
        for item in value:
            text = value_text(item)
            if text:
                result.append(text)
        return ", ".join(result)
    return str(value).strip()


def first(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return value
    return None


def source_id(url, data, name=""):
    value = first(data, "sku", "productId", "product_id", "id", "code", "article")
    if value not in (None, ""):
        return value_text(value), True

    path = urlsplit(url).path.rstrip("/")
    value = path.split("/")[-1]
    if value and value.lower() not in ["product", "products", "item", "catalog"]:
        return value, True
    return " ".join(value_text(name).lower().split()), False


def raw_product(shop, url, name, current, old=None, data=None):
    data = data if isinstance(data, dict) else {}
    gtin = first(data, "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean", "barcode")
    brand = first(data, "brand", "brandName", "manufacturer")
    product_id, stable_id = source_id(url, data, name)

    return {
        "shop": shop,
        "source_url": url,
        "source_product_id": product_id,
        "source_id_is_stable": stable_id,
        "name_raw": value_text(name),
        "gtin_raw": value_text(gtin) or None,
        "model_raw": value_text(first(data, "mpn", "model", "modelName", "vendorCode")),
        "variant_raw": value_text(first(data, "variant", "color", "shade", "flavor")),
        "brand_raw": value_text(brand),
        "category_raw": value_text(first(data, "category", "categoryName", "productType")),
        "package_raw": value_text(first(data, "weight", "size", "volume", "package")),
        "current_price_text": value_text(current),
        "old_price_text": value_text(old),
        "collected_at": datetime.now(UTC).isoformat(),
    }


def validate_product(product, shop):
    fields = set(product)
    expected = set(PRODUCT_FIELDS)
    if fields != expected:
        missing = sorted(expected - fields)
        extra = sorted(fields - expected)
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if extra:
            parts.append("extra: " + ", ".join(extra))
        raise ValueError("Invalid product fields (" + "; ".join(parts) + ")")
    if not isinstance(product["shop"], str):
        raise ValueError("Product shop must be a string")
    if product["shop"] != shop:
        raise ValueError("Product shop does not match scraper shop")
    if not re.fullmatch(r"[a-z0-9_-]+", product["shop"]):
        raise ValueError("Product shop must be a non-empty identifier")
    if not isinstance(product["source_product_id"], str):
        raise ValueError("source_product_id must be a string")
    if not isinstance(product["name_raw"], str):
        raise ValueError("name_raw must be a string")
    if not product["source_product_id"] or not product["name_raw"]:
        raise ValueError("Product identity fields are empty")
    if not isinstance(product["source_id_is_stable"], bool):
        raise ValueError("source_id_is_stable must be boolean")
    text_fields = [
        "source_url", "model_raw", "variant_raw", "brand_raw", "category_raw",
        "package_raw", "current_price_text", "old_price_text",
    ]
    if any(not isinstance(product[field], str) for field in text_fields):
        raise ValueError("Product text fields must be strings")
    url = urlsplit(product["source_url"])
    if url.scheme not in ["http", "https"] or not url.netloc:
        raise ValueError("source_url must be an HTTP URL")
    if product["gtin_raw"] is not None and not isinstance(product["gtin_raw"], str):
        raise ValueError("gtin_raw must be a string or null")
    limits = {
        "shop": 50,
        "source_url": 2048,
        "source_product_id": 500,
        "name_raw": 1000,
        "gtin_raw": 50,
        "model_raw": 500,
        "variant_raw": 500,
        "brand_raw": 500,
        "category_raw": 500,
        "package_raw": 500,
        "current_price_text": 100,
        "old_price_text": 100,
        "collected_at": 100,
    }
    for field, limit in limits.items():
        value = product[field]
        if value is not None and len(value) > limit:
            raise ValueError(field + " is too long")
    try:
        collected_at = datetime.fromisoformat(product["collected_at"])
    except (TypeError, ValueError):
        raise ValueError("collected_at must be an ISO datetime") from None
    if collected_at.tzinfo is None:
        raise ValueError("collected_at must contain a timezone")


def run_scraper(scraper):
    from confluent_kafka import Producer
    from prometheus_client import start_http_server

    shop = scraper.shop
    if not shop or not scraper.base_url:
        raise ValueError("Scraper shop and base_url must not be empty")
    metrics_port = int(os.getenv("METRICS_PORT"))
    interval = int(os.getenv("SCRAPE_INTERVAL_SECONDS"))
    kafka = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    topic = os.getenv("RAW_PRODUCTS_TOPIC")

    start_http_server(metrics_port)
    producer = Producer({"bootstrap.servers": kafka})

    while True:
        try:
            delivery_errors = []

            def delivered(error, _message):
                if error:
                    delivery_errors.append(str(error))

            products = scraper.scrape_products()
            if not products:
                raise RuntimeError("No products were found")

            print(f"Found products: {len(products)}")
            valid_products = []
            for product in products:
                try:
                    validate_product(product, shop)
                    valid_products.append(product)
                except Exception as error:
                    scraper_error(shop)
                    print(f"Invalid product was skipped: {error}")

            if not valid_products:
                raise RuntimeError("No valid products were found")

            for i, product in enumerate(valid_products, start=1):
                print(f"[{i}/{len(valid_products)}] {product['source_url']}")
                key = product["shop"] + ":" + product["source_product_id"]
                producer.produce(
                    topic,
                    key=key,
                    value=json.dumps(product, ensure_ascii=False),
                    on_delivery=delivered,
                )
                producer.poll(0)

            not_sent = producer.flush(10)
            if not_sent or delivery_errors:
                raise RuntimeError("Products were not sent to Kafka")

            get_metrics()["products"].labels(shop=shop).inc(len(valid_products))
            get_metrics()["last_run"].labels(shop=shop).set_to_current_time()
            print("Scraping finished, data was sent to Kafka.")
        except Exception as error:
            scraper_error(shop)
            print(f"Scraper error: {error}")

        print(f"Next run in {interval} sec.")
        time.sleep(interval)
