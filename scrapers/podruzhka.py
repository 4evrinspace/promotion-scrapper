import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scrapers.base import (
    StoreScraper,
    clean_text,
    json_products,
    mapped_product,
    products_from_json,
    unique_products,
)


SHOP = "podruzhka"
BASE_URL = "https://www.podrygka.ru"
CATALOG_URL = BASE_URL + "/catalog/?page=1"


class PodruzhkaScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def page_url(self, page):
        parts = urlsplit(CATALOG_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["page"] = str(page)
        return urlunsplit(parts._replace(query=urlencode(query)))

    def parse_products(self, value, page_url):
        if isinstance(value, (dict, list)):
            return products_from_json(value, SHOP, "podrygka.ru", page_url)
        try:
            return products_from_json(json.loads(value), SHOP, "podrygka.ru", page_url)
        except Exception:
            pass

        soup = BeautifulSoup(value, "html.parser")
        products = json_products(soup, SHOP, "podrygka.ru", page_url)

        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text.startswith("self.__next_f.push([1,"):
                continue
            try:
                payload = json.loads(text[text.find("[1,") + 3:-2])
            except Exception:
                continue
            if not isinstance(payload, str):
                continue
            for line in payload.splitlines():
                if ":" not in line:
                    continue
                try:
                    data = json.loads(line.split(":", 1)[1])
                except Exception:
                    continue
                products.extend(
                    products_from_json(data, SHOP, "podrygka.ru", page_url)
                )

        if products:
            return unique_products(products)

        selectors = "[data-product-id], .product-item, .catalog-item, .product-card"
        for card in soup.select(selectors):
            link = card.select_one("a[href]")
            name = card.select_one(
                "[itemprop='name'], [data-product-name], .product-name, .product-title"
            ) or link
            current = card.select_one(
                "[itemprop='price'], [data-price], .price-current, .current-price"
            )
            old = card.select_one(
                "[data-old-price], .price-old, .old-price, del.text-inherit, del, s"
            )
            data = {
                "productId": card.get("data-product-id"),
                "name": clean_text(name) if name else None,
                "url": link.get("href") if link else None,
                "price": clean_text(current) if current else None,
                "oldPrice": clean_text(old) if old else None,
            }
            product = mapped_product(SHOP, "podrygka.ru", page_url, data)
            if product:
                products.append(product)
        return unique_products(products)

    def scrape_products(self, limit=None):
        limit = self.get_limit(limit)
        products = []
        page = 1
        while len(products) < limit:
            url = self.page_url(page)
            page_products = self.parse_products(self.fetch(url), url)
            if not page_products:
                break
            old_count = len(products)
            products = unique_products(products + page_products)
            if len(products) == old_count:
                break
            page = page + 1
        return products[:limit]


def main():
    PodruzhkaScraper().start()


if __name__ == "__main__":
    main()
