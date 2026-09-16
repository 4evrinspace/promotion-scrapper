import os
import sys
import unittest
from inspect import signature
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from scrapers.base import (
    PRODUCT_FIELDS,
    StoreScraper,
    products_from_json,
    raw_product,
    validate_product,
)
from scrapers.citilink import CitilinkScraper, SITEMAP_URL
from scrapers.holodilnik import HolodilnikScraper
from scrapers.iledebeaute import IleDeBeauteScraper
from scrapers.lenta import LentaScraper, extract_product_data
from scrapers.magnit import MagnitScraper
from scrapers.mvideo import MVideoScraper
from scrapers.podruzhka import PodruzhkaScraper
from scrapers.pyaterochka import PyaterochkaScraper
from scrapers.rivegosh import RiveGoshScraper


SCRAPERS = [
    CitilinkScraper,
    HolodilnikScraper,
    IleDeBeauteScraper,
    LentaScraper,
    MagnitScraper,
    MVideoScraper,
    PodruzhkaScraper,
    PyaterochkaScraper,
    RiveGoshScraper,
]


class ScrapersTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"REQUEST_DELAY_SECONDS": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_interface(self):
        for scraper in SCRAPERS:
            self.assertTrue(issubclass(scraper, StoreScraper))
            self.assertIn("scrape_products", scraper.__dict__)
            self.assertEqual(str(signature(scraper.scrape_products)), "(self, limit=None)")
        self.assertEqual(LentaScraper.base_url, "https://lenta.com")

    def test_common_session(self):
        browsers = []

        class Session:
            def __init__(self):
                self.headers = {}

        def create_scraper(browser):
            browsers.append(browser)
            return Session()

        module = SimpleNamespace(create_scraper=create_scraper)
        with patch.dict(sys.modules, {"cloudscraper": module}):
            for scraper_class in SCRAPERS:
                scraper = scraper_class()
                session = scraper.get_session()
                self.assertEqual(session.headers["User-Agent"], "GPTBot")
                self.assertEqual(session.headers["Accept-Language"], "ru-RU,ru;q=0.9")
                self.assertEqual(session.headers["Referer"], scraper.base_url + "/")
        self.assertTrue(all(x["browser"] == "chrome" for x in browsers))

    def test_fetch_rejects_challenge_page(self):
        class Response:
            text = "<html><title>Just a moment...</title></html>"
            encoding = "utf-8"

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        scraper = RiveGoshScraper()
        scraper.session = Session()
        with patch("scrapers.base.page_parsed") as parsed:
            with self.assertRaisesRegex(RuntimeError, "challenge page"):
                scraper.fetch("https://rivegauche.ru/tags/sale")
        parsed.assert_not_called()

        Response.text = "<html><title>Forbidden Euphoria perfume</title></html>"
        with patch("scrapers.base.page_parsed") as parsed:
            page = scraper.fetch("https://rivegauche.ru/product/perfume")
        self.assertIn("Forbidden Euphoria", page)
        parsed.assert_called_once_with(scraper.shop)

    def test_delay_between_requests(self):
        class Response:
            text = "<html><title>Product</title></html>"
            encoding = "utf-8"

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        scraper = RiveGoshScraper()
        scraper.session = Session()
        scraper.last_request_finished_at = 10
        with patch.dict(os.environ, {"REQUEST_DELAY_SECONDS": "2"}), patch(
            "scrapers.base.time.monotonic", side_effect=[11, 13]
        ), patch("scrapers.base.time.sleep") as sleep:
            scraper.fetch("https://rivegauche.ru/tags/sale")

        sleep.assert_called_once_with(1)

    def test_skipped_section_error_is_counted(self):
        product = {"source_url": "https://shop.test/product/1"}
        cases = [
            (HolodilnikScraper, "action_products", "scrapers.holodilnik.scraper_error"),
            (MagnitScraper, "category_products", "scrapers.magnit.scraper_error"),
            (MVideoScraper, "action_products", "scrapers.mvideo.scraper_error"),
            (PyaterochkaScraper, "category_products", "scrapers.pyaterochka.scraper_error"),
        ]
        for scraper_class, method, metric_name in cases:
            scraper = scraper_class()
            with self.subTest(shop=scraper.shop):
                with patch.object(
                    scraper,
                    method,
                    side_effect=[RuntimeError("HTTP error"), [product]],
                ), patch(metric_name) as error_metric:
                    products = scraper.scrape_products(1)
                self.assertEqual(products, [product])
                error_metric.assert_called_once_with(scraper.shop)

    def test_skipped_product_error_is_counted(self):
        scraper = IleDeBeauteScraper()
        links = [
            {"name": "First", "url": "https://iledebeaute.ru/product/1/"},
            {"name": "Second", "url": "https://iledebeaute.ru/product/2/"},
        ]
        product = {"source_url": links[1]["url"]}
        with patch.object(scraper, "fetch", return_value=""), patch.object(
            scraper, "product_links", return_value=links
        ), patch.object(
            scraper, "parse_product", side_effect=[RuntimeError("HTTP error"), product]
        ), patch(
            "scrapers.iledebeaute.scraper_error"
        ) as error_metric:
            products = scraper.scrape_products(1)
        self.assertEqual(products, [product])
        error_metric.assert_called_once_with(scraper.shop)

    def test_external_scraper_json_format(self):
        scrapers = [
            (CitilinkScraper(), "https://www.citilink.ru/product/demo-123/"),
            (HolodilnikScraper(), "https://www.holodilnik.ru/product/demo-123/"),
            (MagnitScraper(), "https://magnit.ru/promo-product/demo-123/"),
            (MVideoScraper(), "https://www.mvideo.ru/products/demo-123"),
            (PodruzhkaScraper(), "https://www.podrygka.ru/product/demo-123/"),
            (PyaterochkaScraper(), "https://5ka.ru/product/demo-123/"),
            (RiveGoshScraper(), "https://rivegauche.ru/product/demo-123"),
        ]
        for scraper, url in scrapers:
            page = """
            <script type="application/json">
            {"name":"Demo Product 50 ml","url":"%s","sku":"123",
             "brand":{"name":"Demo"},"currentPrice":"99,99 руб",
             "oldPrice":"149,99 руб"}
            </script>
            """ % url
            products = scraper.parse_products(page, scraper.base_url)
            self.assertEqual(len(products), 1, scraper.shop)
            self.assertEqual(products[0]["shop"], scraper.shop)
            self.assertEqual(products[0]["source_product_id"], "123")
            self.assertEqual(products[0]["brand_raw"], "Demo")
            validate_product(products[0], scraper.shop)

    def test_service_json_is_not_a_product(self):
        value = {
            "id": 308,
            "name": 309,
            "url": 310,
            "price": 312,
            "oldPrice": 313,
        }
        products = products_from_json(
            value, "magnit", "magnit.ru", "https://magnit.ru/promo-catalog"
        )
        self.assertEqual(products, [])

    def test_product_without_discount_is_skipped(self):
        value = {
            "@type": "Product",
            "name": "Demo Product",
            "url": "https://shop.test/product/demo",
            "price": "150",
            "oldPrice": "100",
        }
        products = products_from_json(
            value, "shop", "shop.test", "https://shop.test/catalog/"
        )
        self.assertEqual(products, [])

    def test_magnit_card_format(self):
        page = """
        <article class="unit-catalog-product-preview">
          <a href="/promo-product/123-demo" title="Demo Product">
            <span class="unit-catalog-product-preview-prices__regular">99,99 ₽</span>
            <span class="unit-catalog-product-preview-prices__sale">149,99 ₽</span>
            <div class="unit-catalog-product-preview-title">Demo Product</div>
          </a>
        </article>
        """
        products = MagnitScraper().parse_products(
            page, "https://magnit.ru/promo-catalog"
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name_raw"], "Demo Product")
        self.assertEqual(products[0]["current_price_text"], "99,99 ₽")
        self.assertEqual(products[0]["old_price_text"], "149,99 ₽")

    def test_citilink_action_sitemap(self):
        page = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.citilink.ru/actions/demo-sale/</loc></url>
          <url><loc>https://www.citilink.ru/actions/demo-sale/</loc></url>
          <url><loc>https://www.citilink.ru/actions/</loc></url>
          <url><loc>https://www.citilink.ru/product/demo-123/</loc></url>
          <url><loc>https://example.com/actions/wrong-site/</loc></url>
        </urlset>
        """
        links = CitilinkScraper().action_links(page)
        self.assertEqual(
            links,
            ["https://www.citilink.ru/actions/demo-sale/"],
        )

    def test_citilink_current_card_format(self):
        page = """
        <div class="app-catalog-random-class">
          <a href="/product/wrong-overlay/"></a>
          <a data-meta-name="Snippet__title"
             href="/product/noutbuk-acer-demo-1234567/">
            Notebook Acer Demo
          </a>
          <span data-meta-name="Snippet__old-price">66 990</span>
          <span data-meta-name="Snippet__price">
            <span>59 990</span><span>₽</span>
          </span>
          <button data-meta-price="59990">Buy</button>
          <span>From 1 999 ₽ per month</span>
        </div>
        """
        products = CitilinkScraper().parse_products(
            page, "https://www.citilink.ru/actions/demo-sale/"
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name_raw"], "Notebook Acer Demo")
        self.assertEqual(products[0]["current_price_text"], "59990")
        self.assertEqual(products[0]["old_price_text"], "66 990")
        self.assertEqual(
            products[0]["source_url"],
            "https://www.citilink.ru/product/noutbuk-acer-demo-1234567/",
        )
        validate_product(products[0], "citilink")

    def test_citilink_scrape_starts_from_sitemap(self):
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.citilink.ru/actions/demo-sale/</loc></url>
        </urlset>
        """
        action = """
        <article>
          <a data-meta-name="Snippet__title" href="/product/demo-123/">
            Demo Product
          </a>
          <span data-meta-name="Snippet__old-price">150</span>
          <span data-meta-name="Snippet__price">
            <span data-meta-price="100">100 ₽</span>
          </span>
        </article>
        """
        scraper = CitilinkScraper()
        pages = {
            SITEMAP_URL: sitemap,
            "https://www.citilink.ru/actions/demo-sale/": action,
        }

        with patch.object(scraper, "fetch", side_effect=lambda url: pages[url]) as fetch:
            products = scraper.scrape_products(1)

        self.assertEqual(len(products), 1)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            [SITEMAP_URL, "https://www.citilink.ru/actions/demo-sale/"],
        )

    def test_citilink_does_not_hide_page_error(self):
        sitemap = """
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.citilink.ru/actions/demo-sale/</loc></url>
          <url><loc>https://www.citilink.ru/actions/second-sale/</loc></url>
        </urlset>
        """
        scraper = CitilinkScraper()
        with patch.object(
            scraper,
            "fetch",
            side_effect=[sitemap, RuntimeError("HTTP 429")],
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "429"):
                scraper.scrape_products(1)

        self.assertEqual(fetch.call_count, 2)

    def test_podruzhka_nested_product_format(self):
        value = {
            "article": "259301",
            "name": "Demo Toner 200 ml",
            "url": "/catalog/face/259301-demo-toner/",
            "pricing": {"totalPrice": 1043, "basePrice": 1490},
            "attributes": {
                "brand": {"value": "Demo Brand"},
                "section": [{"value": "Face care"}],
            },
        }
        products = PodruzhkaScraper().parse_products(
            value, "https://www.podrygka.ru/catalog/"
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["source_product_id"], "259301")
        self.assertEqual(products[0]["brand_raw"], "Demo Brand")
        self.assertEqual(products[0]["category_raw"], "Face care")

    def test_iledebeaute_product_format(self):
        page = """
        <h1>Demo Cream 50 ml</h1>
        <span itemprop="price" content="99.99"></span>
        <del>149.99</del>
        <script type="application/ld+json">
          {"@type":"Product","name":"Demo Cream 50 ml","sku":"123",
           "brand":{"name":"Demo"},"offers":{"price":"99.99"}}
        </script>
        """

        class Response:
            text = page

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        scraper = IleDeBeauteScraper()
        scraper.session = Session()
        product = scraper.parse_product(
            {"name": "Demo Cream", "url": "https://iledebeaute.ru/product/demo-123"}
        )
        self.assertEqual(product["source_product_id"], "123")
        self.assertEqual(product["brand_raw"], "Demo")
        validate_product(product, scraper.shop)

    def test_iledebeaute_product_links_are_unique(self):
        page = """
        <a href="/product/demo-123">Demo Product</a>
        <a href="/product/demo-123?source=tile">Demo Product</a>
        """
        products = IleDeBeauteScraper().product_links(page)
        self.assertEqual(
            products,
            [{
                "name": "Demo Product",
                "url": "https://iledebeaute.ru/product/demo-123/",
            }],
        )

    def test_product_format(self):
        product = raw_product(
            "shop",
            "https://shop.test/product/123/",
            "Product 50 ml",
            100,
            150,
            {"sku": "123", "brand": {"name": "Brand"}},
        )
        self.assertEqual(set(product), set(PRODUCT_FIELDS))
        self.assertEqual(product["brand_raw"], "Brand")
        validate_product(product, "shop")

        product["unused"] = "data"
        with self.assertRaises(ValueError):
            validate_product(product, "shop")

    def test_name_is_used_when_source_id_is_missing(self):
        product = raw_product("shop", "", "Product without id", 100)
        self.assertEqual(product["source_product_id"], "product without id")
        self.assertFalse(product["source_id_is_stable"])
        with self.assertRaises(ValueError):
            validate_product(product, "shop")

        generic_url = raw_product(
            "shop", "https://shop.test/product/", "Product without id", 100
        )
        self.assertFalse(generic_url["source_id_is_stable"])
        validate_product(generic_url, "shop")

    def test_lenta_json_data(self):
        page = """
        <script type="application/ld+json">
          {"@type":"Product","name":"Product","sku":"123",
           "gtin13":"4006381333931","brand":{"name":"Brand"}}
        </script>
        """
        data = extract_product_data(BeautifulSoup(page, "html.parser"))
        self.assertEqual(data["sku"], "123")
        self.assertEqual(data["gtin"], "4006381333931")

        invalid_page = """
        <script type="application/ld+json">
          {"@type":"Product","name":"Product","gtin13":"code4006381333931"}
        </script>
        """
        data = extract_product_data(BeautifulSoup(invalid_page, "html.parser"))
        self.assertEqual(data["gtin"], "code4006381333931")

    def test_lenta_visible_prices(self):
        page = """
        <h1 itemprop="name">Demo Product</h1>
        <div automation-id="product-price">
          <div class="main-price"><span>129</span><span>99</span> ₽</div>
          <div class="old-price">
            <span>199 99 ₽</span><span class="discount-badge">-35%</span>
          </div>
        </div>
        <script type="application/ld+json">
          {"@type":"Product","name":"Other Name","sku":"123",
           "offers":{"price":"500"}}
        </script>
        """

        class Response:
            text = page

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        scraper = LentaScraper()
        scraper.session = Session()
        product = scraper.parse_product("https://lenta.com/product/demo-123/")
        self.assertEqual(product["current_price_text"], "129 99 ₽")
        self.assertEqual(product["old_price_text"], "199 99 ₽")
        self.assertEqual(product["name_raw"], "Demo Product")

    def test_lenta_product_without_promotion_is_skipped(self):
        page = """
        <h1 itemprop="name">Demo Product</h1>
        <div automation-id="product-price">
          <div class="main-price"><span>129</span><span>99</span> ₽</div>
        </div>
        <script type="application/ld+json">
          {"@type":"Product","name":"Demo Product","sku":"123",
           "offers":{"price":"129.99"}}
        </script>
        """

        class Response:
            text = page

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        scraper = LentaScraper()
        scraper.session = Session()
        product = scraper.parse_product("https://lenta.com/product/demo-123/")
        self.assertIsNone(product)


if __name__ == "__main__":
    unittest.main()
