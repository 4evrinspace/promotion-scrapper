import re

from bs4 import BeautifulSoup

from scrapers.base import (
    StoreScraper,
    card_product,
    clean_text,
    json_products,
    next_page_url,
    scraper_error,
    unique_products,
)


SHOP = "magnit"
BASE_URL = "https://magnit.ru"
SHOP_CODE = "995010"
CATEGORY_URLS = [
    BASE_URL + "/promo-catalog?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/151-skidki-na-kategorii?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/27-ovoschi-i-fruktyi?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/22-moloko-syir-yajtsa?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/25-myaso-ptitsa-kolbasyi?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/23-bakaleya-sousyi?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/21-napitki?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/101-chaj-kofe-kakao?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/33-byitovaya-himiya?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/32-kosmetika-i-parfyumeriya?shopCode=" + SHOP_CODE,
    BASE_URL + "/promo-catalog/30-konditerskie-izdeliya?shopCode=" + SHOP_CODE,
]


class MagnitScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def parse_products(self, html, page_url):
        soup = BeautifulSoup(html, "html.parser")
        products = json_products(soup, SHOP, "magnit.ru", page_url)
        cards = []
        for link in soup.select("a[href*='/promo-product/']"):
            for parent in link.parents:
                text = clean_text(parent)
                if len(text) > 1600:
                    break
                if re.search(r"\d[\d\s]*[,.]?\d*\s*(?:₽|руб\.?|р\.?)", text, re.I):
                    cards.append(parent)
                    break

        for card in cards:
            product = card_product(
                card,
                SHOP,
                "magnit.ru",
                page_url,
                "a[href*='/promo-product/']",
                "[itemprop='name'], [data-product-name], h2, h3, .unit-catalog-product-preview-title",
                "[itemprop='price'], [data-current-price], .price-current, .unit-catalog-product-preview-prices__regular",
                "[data-old-price], .price-old, .old-price, .unit-catalog-product-preview-prices__sale, del, s",
            )
            if product:
                products.append(product)
        return unique_products(products)

    def category_products(self, category_url, limit):
        products = []
        visited = set()
        page_url = category_url
        while page_url and len(products) < limit:
            if page_url in visited:
                break
            visited.add(page_url)
            html = self.fetch(page_url)
            page_products = self.parse_products(html, page_url)
            old_count = len(products)
            products = unique_products(products + page_products)
            if not page_products or len(products) == old_count:
                break
            page_url = next_page_url(
                BeautifulSoup(html, "html.parser"), page_url, "magnit.ru", visited
            )
        return products[:limit]

    def scrape_products(self, limit=None):
        limit = self.get_limit(limit)
        products = []
        for url in CATEGORY_URLS:
            if len(products) >= limit:
                break
            try:
                items = self.category_products(url, limit - len(products))
            except Exception as error:
                scraper_error(self.shop)
                print(f"Category page was skipped: {error}")
                continue
            products = unique_products(products + items)
        return products[:limit]


def main():
    MagnitScraper().start()


if __name__ == "__main__":
    main()
