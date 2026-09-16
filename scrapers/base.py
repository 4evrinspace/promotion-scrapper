import json
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urljoin, urlsplit

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
REQUEST_TIMEOUT_SECONDS = 30


def blocked_page(html):
    text = str(html or "").lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S)
    title = re.sub(r"<[^>]+>", " ", title_match.group(1)) if title_match else ""
    title = re.sub(r"\s+", " ", title).strip()
    exact_titles = [
        "access denied",
        "forbidden",
        "too many requests",
        "service unavailable",
        "captcha",
        "robot check",
        "browser check",
        "доступ ограничен",
        "проверка браузера",
    ]
    title_starts = ["just a moment", "attention required"]
    status_title = re.fullmatch(
        r"(?:error\s*)?(?:403|429|503)(?:\s+[^<]*)?", title
    )
    if title in exact_titles or any(title.startswith(x) for x in title_starts):
        return True
    if status_title:
        return True

    bad_text = [
        "checking your browser before accessing",
        "verify you are human to continue",
        "подтвердите, что вы не робот",
        "проверяем, не робот ли вы",
        "доступ к сайту временно ограничен",
        "/cdn-cgi/challenge-platform/",
        'id="challenge-form"',
    ]
    return any(value in text for value in bad_text)


class StoreScraper(ABC):
    shop = ""
    base_url = ""

    def __init__(self):
        self.session = None
        self.last_request_finished_at = None

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

    def get_limit(self, limit):
        value = limit if limit is not None else int(os.getenv("SCRAPER_PRODUCT_LIMIT"))
        if value < 1:
            raise ValueError("Product limit must be greater than zero")
        return value

    def fetch(self, url):
        delay = float(os.getenv("REQUEST_DELAY_SECONDS"))
        if delay < 0:
            raise ValueError("Request delay must not be negative")
        if self.last_request_finished_at is not None:
            passed = time.monotonic() - self.last_request_finished_at
            if passed < delay:
                time.sleep(delay - passed)

        try:
            response = self.get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            encoding = str(getattr(response, "encoding", "") or "").lower()
            if encoding in ["iso-8859-1", "latin-1"]:
                response.encoding = response.apparent_encoding
            if blocked_page(response.text):
                raise RuntimeError("Blocked or challenge page received")
            page_parsed(self.shop)
            return response.text
        except Exception as error:
            raise RuntimeError(f"Unable to fetch page: {url}: {error}") from error
        finally:
            self.last_request_finished_at = time.monotonic()


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


def clean_text(value):
    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value_text(value).replace("\u00a0", " ")).strip()


def price_number(value):
    text = value_text(value).replace("\u00a0", " ")
    match = re.search(r"\d[\d\s]*(?:[,.]\d{1,2})?", text)
    if not match:
        return None
    number = re.sub(r"\s+", "", match.group(0)).replace(",", ".")
    try:
        return float(number)
    except Exception:
        return None


def looks_like_product(data, name, url, current, old):
    name = clean_text(name)
    if len(name) < 3 or not any(char.isalpha() for char in name):
        return False

    current_number = price_number(current)
    old_number = price_number(old)
    if current_number is None or old_number is None or old_number <= current_number:
        return False

    path = urlsplit(url).path.lower()
    product_path = re.search(r"/(?:product|products|promo-product|catalog)/", path)
    data_type = value_text(data.get("@type")).lower()
    item_id = first(data, "sku", "productId", "product_id", "code", "article")
    last_part = path.rstrip("/").split("/")[-1]
    named_id = item_id and any(char.isalpha() for char in last_part)
    parts = [part for part in path.split("/") if part]
    deep_path = len(parts) >= 3 and any(char.isalpha() for char in last_part)
    return bool(product_path or data_type == "product" or named_id or deep_path)


def same_site_url(value, page_url, host):
    if not value:
        return None
    url = urljoin(page_url, str(value).strip())
    actual_host = urlsplit(url).netloc.lower().split(":", 1)[0]
    if actual_host == host or actual_host.endswith("." + host):
        return url
    return None


def mapped_product(shop, host, page_url, data):
    if not isinstance(data, dict):
        return None

    nested = {}
    for key in ["pricing", "priceData", "prices", "offers"]:
        value = data.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            nested.update(value)

    name = first(data, "name", "product_name", "productName", "title", "displayName")
    url = first(data, "url", "product_url", "productUrl", "link", "detailUrl", "canonicalUrl")
    current = first(
        data, "current_price", "price_current", "currentPrice", "salePrice",
        "promoPrice", "finalPrice", "priceFinal", "price",
    )
    old = first(
        data, "old_price", "price_old", "oldPrice", "regularPrice",
        "basePrice", "listPrice", "highPrice", "crossedPrice", "old",
    )
    if current is None:
        current = first(
            nested, "current", "sale", "final", "currentPrice", "salePrice",
            "totalPrice", "price",
        )
    if old is None:
        old = first(
            nested, "old", "regular", "base", "oldPrice", "regularPrice",
            "basePrice", "listPrice", "highPrice",
        )
    if not name:
        name = first(nested, "name", "productName", "title")
    if not url:
        url = first(nested, "url", "productUrl")

    product_url = same_site_url(url, page_url, host)
    if not name or not product_url or current is None or old is None:
        return None
    if not looks_like_product(data, name, product_url, current, old):
        return None

    details = dict(nested)
    details.update(data)
    attributes = data.get("attributes")
    if isinstance(attributes, dict):
        brand = attributes.get("brand")
        if isinstance(brand, dict):
            details.setdefault("brand", brand.get("value"))
        sections = attributes.get("section")
        if isinstance(sections, list) and sections:
            section = sections[-1]
            if isinstance(section, dict):
                details.setdefault("category", section.get("value"))
    return raw_product(shop, product_url, clean_text(name), current, old, details)


def products_from_json(value, shop, host, page_url):
    products = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            product = mapped_product(shop, host, page_url, item)
            if product:
                products.append(product)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return unique_products(products)


def json_products(soup, shop, host, page_url):
    products = []
    selectors = (
        "script#__NEXT_DATA__, script#__NUXT_DATA__, "
        "script[type='application/ld+json'], script[type='application/json']"
    )
    for script in soup.select(selectors):
        text = script.string or script.get_text()
        if not text or len(text) > 10_000_000:
            continue
        try:
            value = json.loads(text)
        except Exception:
            continue
        products.extend(products_from_json(value, shop, host, page_url))
    return unique_products(products)


def unique_products(products):
    result = []
    seen = set()
    for product in products:
        url = product.get("source_url")
        if url and url not in seen:
            seen.add(url)
            result.append(product)
    return result


def card_product(
    card, shop, host, page_url, link_selector, name_selector,
    current_selector, old_selector,
):
    link = card.select_one(link_selector)
    name = card.select_one(name_selector)
    current = card.select_one(current_selector)
    old = card.select_one(old_selector)
    data = {
        "productId": card.get("data-product-id"),
        "name": clean_text(name) if name else None,
        "url": link.get("href") if link else None,
        "price": current.get("content") or clean_text(current) if current else None,
        "oldPrice": old.get("content") or clean_text(old) if old else None,
    }
    return mapped_product(shop, host, page_url, data)


def next_page_url(soup, current_url, host, visited, parameter="page"):
    query = dict(parse_qsl(urlsplit(current_url).query, keep_blank_values=True))
    current = int(query.get(parameter, "1") or 1)
    candidates = []
    selectors = "a[rel='next'][href], a[aria-label*='След'][href], a[href], button[data-url]"
    for link in soup.select(selectors):
        href = link.get("href") or link.get("data-url")
        url = same_site_url(href, current_url, host)
        if not url or url in visited:
            continue
        item_query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        page_text = item_query.get(parameter, "0") or "0"
        page = int(page_text) if str(page_text).isdigit() else 0
        text = clean_text(link).lower()
        is_next = (
            link.get("rel") == ["next"] or "след" in text or "далее" in text
            or "показать еще" in text or "показать ещё" in text
        )
        if page == current + 1 or is_next:
            candidates.append((page or current + 1, url))
    return min(candidates)[1] if candidates else None


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
