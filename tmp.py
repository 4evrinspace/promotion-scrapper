import json
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server


BASE_URL = "https://lenta.com"
SITEMAP_URL = urljoin(BASE_URL, "/sitemap/sitemap_index.xml")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
RAW_PRODUCTS_TOPIC = os.getenv("RAW_PRODUCTS_TOPIC")
METRICS_PORT = int(os.getenv("METRICS_PORT"))
REQUEST_TIMEOUT_SECONDS = 30
SITEMAP_LIMIT = 2
URLS_PER_SITEMAP_LIMIT = 10
SCRAPE_INTERVAL_SECONDS = int(os.getenv("SCRAPE_INTERVAL_SECONDS"))

PAGES_PARSED = Counter("scraper_pages_parsed_total", "", ["shop"])
REQUEST_ERRORS = Counter("scraper_request_errors_total", "", ["shop"])
PRODUCTS_PUBLISHED = Counter("scraper_products_published_total", "", ["shop"])
LAST_SUCCESSFUL_RUN = Gauge("scraper_last_successful_run_timestamp", "", ["shop"])

SHOP = "lenta"
scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)
scraper.headers.update(
    {
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": f"{BASE_URL}/",
        "User-Agent": "Mozilla/5.0",
    }
)


def text_of(x, default=""):
    if x:
        return x.get_text(" ", strip=True)
    return default


def request_xml(url):
    r = scraper.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "xml")


def extract_gtin(soup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue

        if isinstance(data, dict):
            data = data.get("@graph", [data])
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for field in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
                digits = re.sub(r"\D", "", str(entry.get(field, "")))
                if len(digits) in {8, 12, 13, 14}:
                    return digits
    return None


def get_sitemap_urls(sitemap_url):
    sitemap = request_xml(sitemap_url)
    child_sitemaps = sitemap.find_all("sitemap")

    if not child_sitemaps:
        return [
            text_of(item.find("loc"))
            for item in sitemap.find_all("url")[:URLS_PER_SITEMAP_LIMIT]
            if item.find("loc")
        ]

    urls: list[str] = []
    for item in child_sitemaps[:SITEMAP_LIMIT]:
        child_url = text_of(item.find("loc"))
        if not child_url:
            continue

        try:
            child_sitemap = request_xml(child_url)
            urls.extend(
                text_of(url_tag.find("loc"))
                for url_tag in child_sitemap.find_all("url")[:URLS_PER_SITEMAP_LIMIT]
                if url_tag.find("loc")
            )
        except Exception as error:
            REQUEST_ERRORS.labels(shop=SHOP).inc()
            print(f"Sitemap error {child_url}: {error}")

    return urls


def parse_product(url):
    try:
        r = scraper.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        price_block = soup.select_one('[automation-id="product-price"]')
        old_price = text_of(
            price_block.select_one(".old-price-product span") if price_block else None
        )
        current_price = text_of(
            price_block.select_one(".main-price") if price_block else None
        )

        PAGES_PARSED.labels(shop=SHOP).inc()
        return {
            "shop": SHOP,
            "source_url": url,
            "source_product_id": url.rstrip("/").split("/")[-1],
            "gtin": extract_gtin(soup),
            "name": text_of(soup.select_one('h1[itemprop="name"]')),
            "current_price_text": current_price,
            "old_price_text": old_price,
            "discount_text": text_of(
                price_block.select_one(".discount-badge") if price_block else None
            ),
            "in_stock": bool(current_price),
            "collected_at": datetime.now(UTC).isoformat(),
        }
    except Exception as error:
        REQUEST_ERRORS.labels(shop=SHOP).inc()
        print(f"Page error {url}: {error}")
        return None


def publish_product(producer, product):
    producer.produce(
        RAW_PRODUCTS_TOPIC,
        key=f"{product['shop']}:{product['source_product_id']}",
        value=json.dumps(product, ensure_ascii=False),
    )
    producer.poll(0)
    PRODUCTS_PUBLISHED.labels(shop=SHOP).inc()


def scrape_once(producer):
    product_urls = list(dict.fromkeys(get_sitemap_urls(SITEMAP_URL)))
    print(f"Found product pages: {len(product_urls)}")

    for index, url in enumerate(product_urls, start=1):
        print(f"[{index}/{len(product_urls)}] {url}")
        if product := parse_product(url):
            publish_product(producer, product)

    producer.flush()
    LAST_SUCCESSFUL_RUN.labels(shop=SHOP).set_to_current_time()
    print("Scraping finished, data was sent to Kafka.")


def main():
    start_http_server(METRICS_PORT)
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})

    while True:
        try:
            scrape_once(producer)
        except Exception as error:
            REQUEST_ERRORS.labels(shop=SHOP).inc()
            print(f"Scraper error: {error}")

        print(f"Next run in {SCRAPE_INTERVAL_SECONDS} sec.")
        time.sleep(SCRAPE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
