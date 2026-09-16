from bs4 import BeautifulSoup

from scrapers.base import (
    StoreScraper,
    card_product,
    json_products,
    next_page_url,
    scraper_error,
    unique_products,
)


SHOP = "holodilnik"
BASE_URL = "https://www.holodilnik.ru"
ACTION_URLS = [
    BASE_URL + "/action/washing_machines_skidki/",
    BASE_URL + "/action/tv_skidki/",
    BASE_URL + "/action/refridgerators_skidki/",
]


class HolodilnikScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def parse_products(self, html, page_url):
        soup = BeautifulSoup(html, "html.parser")
        products = json_products(soup, SHOP, "holodilnik.ru", page_url)
        cards = soup.select(
            "div.a-prod, div.catalog-item, [data-product-id], .product-card, .product-item"
        )
        for card in cards:
            product = card_product(
                card,
                SHOP,
                "holodilnik.ru",
                page_url,
                "a.catalog-item__name, a.catalog-item__image-link, a.a-prod-link, a[href*='/product/']",
                ".catalog-item__name, .catalog-item__title, .ap-model, [itemprop='name'], .product-name, h2, h3",
                ".price-value__actual, .ap-price-new, [itemprop='price'], [data-current-price], .price-current",
                ".price-value__old, .ap-price-old, [data-old-price], .price-old, .old-price, del, s",
            )
            if product:
                products.append(product)
        return unique_products(products)

    def action_products(self, action_url, limit):
        products = []
        visited = set()
        page_url = action_url
        while page_url and len(products) < limit:
            if page_url in visited:
                break
            visited.add(page_url)
            html = self.fetch(page_url)
            page_products = self.parse_products(html, page_url)
            products = unique_products(products + page_products)
            if not page_products:
                break
            page_url = next_page_url(
                BeautifulSoup(html, "html.parser"),
                page_url,
                "holodilnik.ru",
                visited,
            )
        return products[:limit]

    def scrape_products(self, limit=None):
        limit = self.get_limit(limit)
        products = []
        for url in ACTION_URLS:
            if len(products) >= limit:
                break
            try:
                items = self.action_products(url, limit - len(products))
            except Exception as error:
                scraper_error(self.shop)
                print(f"Promotion page was skipped: {error}")
                continue
            products = unique_products(products + items)
        return products[:limit]


def main():
    HolodilnikScraper().start()


if __name__ == "__main__":
    main()
