import argparse
import json

from scrapers.base import validate_product
from scrapers.lenta import LentaScraper


SCRAPERS = {
    "lenta": LentaScraper,
}


def short_product(product):
    return {
        "name": product["name_raw"],
        "source_product_id": product["source_product_id"],
        "gtin": product["gtin_raw"],
        "current_price": product["current_price_text"],
        "old_price": product["old_price_text"],
        "url": product["source_url"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("shop", choices=SCRAPERS)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    if args.limit < 1:
        print("Limit must be greater than zero")
        return 1

    scraper = SCRAPERS[args.shop]()

    try:
        products = scraper.scrape_products(args.limit)
    except Exception as error:
        print(f"Scraper check failed: {error}")
        return 1

    if not products:
        print("No products were found")
        return 1

    for product in products:
        try:
            validate_product(product, scraper.shop)
        except Exception as error:
            print(f"Invalid product: {error}")
            return 1
        print(json.dumps(short_product(product), ensure_ascii=False, indent=2))

    print(f"Checked products: {len(products)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
