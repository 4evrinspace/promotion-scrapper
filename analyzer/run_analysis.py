"""Точка входа анализатора.

Пример:
    python -m analyzer.run_analysis \
        --input data/normalized_products_500.csv \
        --out report/
"""

import argparse

from analyzer.data_prep import load_products
from analyzer.reports import build_report, export_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Анализ промо-акций")
    parser.add_argument("--input", required=True, help="CSV из нормализатора")
    parser.add_argument("--out", default="report", help="папка для отчёта")
    parser.add_argument("--top-favorites", type=int, default=20)
    args = parser.parse_args()

    products = load_products(args.input)
    report = build_report(products, top_n_favorites=args.top_favorites)
    paths = export_report(report, args.out)

    print(
        f"Строк данных: {len(products)}, товаров: "
        f"{products['canonical_product_id'].nunique()}, "
        f"магазинов: {products['shop'].nunique()}"
    )
    for name, table in report.items():
        print(f"  {name}: {len(table)} строк")
    print(f"Отчёт сохранён: {paths}")


if __name__ == "__main__":
    main()