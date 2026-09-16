from bs4 import BeautifulSoup

from scrapers.base import (
    StoreScraper,
    card_product,
    json_products,
    next_page_url,
    scraper_error,
    unique_products,
)


SHOP = "mvideo"
BASE_URL = "https://www.mvideo.ru"
ACTION_URLS = [
    BASE_URL + "/promo/promocatalog",
    BASE_URL + "/promo/totalnaya-likvidatsiya",
    BASE_URL + "/promo/luchshie-predlojeniya",
    BASE_URL + "/promo/skidki-teplo",
    BASE_URL + "/promo/promocatalog?from=marketplace",
    BASE_URL + "/promo/skidki-na-tovary-dlya-krasoty",
]


class MVideoScraper(StoreScraper):
    shop = SHOP
    base_url = BASE_URL

    def parse_products(self, html, page_url):
        soup = BeautifulSoup(html, "html.parser")
        products = json_products(soup, SHOP, "mvideo.ru", page_url)
        cards = soup.select("mvid-product-card, div.product-card, [data-product-id]")
        for card in cards:
            product = card_product(
                card,
                SHOP,
                "mvideo.ru",
                page_url,
                "mvid-gallery a[href*='/products/'], mvid-product-title a[href*='/products/'], a[href*='/products/']",
                "mvid-product-title a, .product-mini-card__name a, .product-title, [itemprop='name'], h2, h3",
                "mvid-sale-price, .price__main-value, .price-current, [data-current-price], [itemprop='price']",
                "mvid-base-price, .price__old-value, .price-old, [data-old-price], del, s",
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
                BeautifulSoup(html, "html.parser"), page_url, "mvideo.ru", visited
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
    MVideoScraper().start()


if __name__ == "__main__":
    main()
