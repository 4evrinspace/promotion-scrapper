import json
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scrapers.base import StoreScraper, page_parsed, raw_product, scraper_error


BASE_URL = "https://lenta.com"
SITEMAP_URL = urljoin(BASE_URL, "/sitemap/sitemap_index.xml")
REQUEST_TIMEOUT_SECONDS = 30
SHOP = "lenta"


def text_of(x, default=""):
    if x:
        return x.get_text(" ", strip=True)
    return default


def json_ld_entries(soup):
    result = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            entry = stack.pop()
            if not isinstance(entry, dict):
                continue
            result.append(entry)
            for value in entry.values():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(x for x in value if isinstance(x, dict))
    return result


def simple_value(value):
    if isinstance(value, dict):
        if value.get("name"):
            return value["name"]
        if value.get("value") is not None:
            units = {
                "GRM": "g", "KGM": "kg", "MLT": "ml", "LTR": "l", "C62": "pcs",
            }
            unit = value.get("unitText") or units.get(value.get("unitCode"), "")
            return (str(value["value"]) + " " + unit).strip()
        return ""
    if isinstance(value, list):
        result = []
        for item in value:
            text = simple_value(item)
            if text:
                result.append(str(text))
        return ", ".join(result)
    return str(value or "")


def extract_product_data(soup):
    product = {}
    for entry in json_ld_entries(soup):
        item_type = entry.get("@type", "")
        if isinstance(item_type, list):
            is_product = any(str(x).endswith("Product") for x in item_type)
        else:
            is_product = str(item_type).endswith("Product")
        if is_product:
            product = entry
            break

    gtin = ""
    invalid_gtin = ""
    for field in ["gtin", "gtin8", "gtin12", "gtin13", "gtin14"]:
        value = product.get(field)
        values = value if isinstance(value, list) else [value]
        for item in values:
            raw_gtin = simple_value(item).strip()
            if raw_gtin and not invalid_gtin:
                invalid_gtin = raw_gtin
            digits = re.sub(r"[\s-]", "", raw_gtin)
            if re.fullmatch(r"[0-9]+", digits) and len(digits) in [8, 12, 13, 14]:
                gtin = raw_gtin
                break
        if gtin:
            break

    offer = product.get("offers", {})
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    if not isinstance(offer, dict):
        offer = {}

    return {
        "name": simple_value(product.get("name")),
        "gtin": gtin or invalid_gtin or None,
        "sku": simple_value(product.get("sku")),
        "model": simple_value(product.get("mpn") or product.get("model")),
        "variant": simple_value(product.get("color")),
        "brand": simple_value(product.get("brand")),
        "category": simple_value(product.get("category")),
        "package": simple_value(
            product.get("weight") or product.get("size") or product.get("volume")
        ),
        "price": simple_value(offer.get("price")),
    }


class LentaScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def request_xml(self, url):
        r = self.get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        return BeautifulSoup(r.text, "xml")

    def get_sitemap_urls(self, sitemap_url, limit=None):
        sitemap = self.request_xml(sitemap_url)
        child_sitemaps = sitemap.find_all("sitemap")

        if not child_sitemaps:
            items = sitemap.find_all("url")
            if limit is not None:
                items = items[:limit]
            return [
                text_of(item.find("loc"))
                for item in items
                if item.find("loc")
            ]

        urls = []
        for item in child_sitemaps:
            if limit is not None and len(urls) >= limit:
                break
            child_url = text_of(item.find("loc"))
            if not child_url:
                continue

            try:
                child_sitemap = self.request_xml(child_url)
                items = child_sitemap.find_all("url")
                if limit is not None:
                    items = items[:limit - len(urls)]
                urls.extend(
                    text_of(url_tag.find("loc"))
                    for url_tag in items
                    if url_tag.find("loc")
                )
            except Exception as error:
                scraper_error(self.shop)
                print(f"Sitemap error {child_url}: {error}")

        return urls

    def parse_product(self, url):
        try:
            r = self.get_session().get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            price_block = soup.select_one('[automation-id="product-price"]')
            old_price = text_of(
                price_block.select_one(
                    ".old-price > span:not(.discount-badge), .old-price-product span"
                )
                if price_block else None
            )
            current_price = text_of(
                price_block.select_one(".main-price") if price_block else None
            )
            structured = extract_product_data(soup)
            if not current_price:
                current_price = structured["price"]
            name = text_of(soup.select_one('h1[itemprop="name"]'))
            if not name:
                name = structured["name"]

            page_parsed(self.shop)
            return raw_product(
                self.shop,
                url,
                name,
                current_price,
                old_price,
                structured,
            )
        except Exception as error:
            scraper_error(self.shop)
            print(f"Page error {url}: {error}")
            return None

    def scrape_products(self, limit=None):
        if limit is None:
            limit = int(os.getenv("SCRAPER_PRODUCT_LIMIT"))
        product_urls = list(
            dict.fromkeys(self.get_sitemap_urls(SITEMAP_URL, limit))
        )[:limit]
        products = []

        for url in product_urls:
            product = self.parse_product(url)
            if product:
                products.append(product)

        return products


def main():
    LentaScraper().start()


if __name__ == "__main__":
    main()
