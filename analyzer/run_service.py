"""Периодический прогон аналитики поверх ClickHouse внутри compose."""

import os
import time

from analyzer.clickhouse_source import load_products_from_clickhouse
from analyzer.reports import build_report, export_report


def run_once(out_dir: str, top_n_favorites: int) -> None:
    products = load_products_from_clickhouse()
    if products.empty:
        print("В ClickHouse пока нет данных — пропускаю прогон.")
        return
    report = build_report(products, top_n_favorites=top_n_favorites)
    paths = export_report(report, out_dir)
    print(f"Отчёт обновлён: {paths}")


def main() -> None:
    out_dir = os.getenv("ANALYZER_OUTPUT_DIR", "report")
    interval = int(os.getenv("ANALYZER_INTERVAL_SECONDS", "3600"))
    top_n = int(os.getenv("ANALYZER_TOP_FAVORITES", "20"))

    while True:
        try:
            run_once(out_dir, top_n)
        except Exception as error:
            print(f"Ошибка прогона аналитики: {error}")
        time.sleep(interval)


if __name__ == "__main__":
    main()