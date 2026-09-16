import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scrapers.base import (
    StoreScraper,
    clean_text,
    price_number,
    raw_product,
    same_site_url,
    scraper_error,
    unique_products,
)


SHOP = "iledebeaute"
BASE_URL = "https://iledebeaute.ru"
CATALOG_URL = BASE_URL + "/catalog/tip-has_discount-iz-prom/"


def normalized_url(url):
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    path = parts.path
    if path and not path.endswith("/"):
        path = path + "/"
    return urlunsplit(parts._replace(path=path, query=query, fragment=""))


def product_data(soup):
    for script in soup.select("script[type='application/ld+json']"):
        try:
            value = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = value if isinstance(value, list) else [value]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(x).endswith("Product") for x in types):
                return item
            stack.extend(item.values())
    return {}


class IleDeBeauteScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def product_links(self, html):
        soup = BeautifulSoup(html, "html.parser")
        result = []
        seen = set()
        for link in soup.select("a[href*='/product/']"):
            url = same_site_url(link.get("href"), BASE_URL, "iledebeaute.ru")
            if url:
                parts = urlsplit(normalized_url(url))
                url = urlunsplit(parts._replace(query="", fragment=""))
            name = clean_text(
                link.get("aria-label") or link.get("title") or link.get_text(" ", strip=True)
            )
            if not name:
                image = link.select_one("img[alt]")
                name = clean_text(image.get("alt")) if image else ""
            if name and url and url not in seen:
                seen.add(url)
                result.append({"name": name, "url": url})
        return result

    def parse_product(self, product):
        soup = BeautifulSoup(self.fetch(product["url"]), "html.parser")
        data = product_data(soup)
        offer = data.get("offers", {})
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        if not isinstance(offer, dict):
            offer = {}

        title = soup.select_one("h1")
        current = soup.select_one("[itemprop='price'], [class*='current-price']")
        old = soup.select_one("[class*='old-price'], [class*='oldPrice'], del, s")
        current_value = (
            current.get("content") or clean_text(current) if current else offer.get("price")
        )
        old_value = old.get("content") or clean_text(old) if old else None
        parent = current.parent if current else None
        for _ in range(4):
            if old_value or parent is None:
                break
            prices = re.findall(
                r"\d[\d ]*(?:[,.]\d{1,2})?\s*(?:¤|₽|руб\.?)",
                clean_text(parent),
                re.I,
            )
            for value in prices:
                if price_number(value) != price_number(current_value):
                    old_value = value
                    break
            parent = parent.parent
        current_number = price_number(current_value)
        old_number = price_number(old_value)
        if not current_number or not old_number or old_number <= current_number:
            return None
        name = clean_text(title) if title else clean_text(data.get("name") or product["name"])
        return raw_product(
            SHOP,
            product["url"],
            name,
            current_value,
            old_value,
            data,
        )

    def next_page(self, soup, current_url, visited):
        current_query = dict(parse_qsl(urlsplit(current_url).query))
        current = int(current_query.get("page", current_query.get("PAGEN_1", "1")) or 1)
        result = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = clean_text(link).lower()
            if "показать" not in text and "page=" not in href.lower() and "pagen_1" not in href.lower():
                continue
            url = same_site_url(href, current_url, "iledebeaute.ru")
            if not url:
                continue
            query = dict(parse_qsl(urlsplit(url).query))
            page = int(query.get("page", query.get("PAGEN_1", "0")) or 0)
            if page > current and normalized_url(url) not in visited:
                result.append((page, url))
        return min(result)[1] if result else None

    def scrape_products(self, limit=None):
        limit = self.get_limit(limit)
        products = []
        visited = set()
        page_url = CATALOG_URL
        while page_url and len(products) < limit:
            page_key = normalized_url(page_url)
            if page_key in visited:
                break
            visited.add(page_key)
            html = self.fetch(page_url)
            soup = BeautifulSoup(html, "html.parser")
            for product in self.product_links(html):
                if len(products) >= limit:
                    break
                try:
                    value = self.parse_product(product)
                except Exception as error:
                    scraper_error(self.shop)
                    print(f"Product page was skipped: {error}")
                    continue
                if value:
                    products.append(value)
            products = unique_products(products)
            page_url = self.next_page(soup, page_url, visited)
        return products[:limit]


def main():
    IleDeBeauteScraper().start()


if __name__ == "__main__":
    main()
