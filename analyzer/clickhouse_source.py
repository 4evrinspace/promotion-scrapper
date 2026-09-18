"""Загрузка данных для аналитики напрямую из ClickHouse (без промежуточного CSV)."""

import os
import pandas as pd  # pyright: ignore[reportMissingModuleSource]

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD")

FIELDS = [
    "canonical_product_id", "name", "shop", "date",
    "original_price", "promotion_price", "match_status",
]


def load_products_from_clickhouse(days: int = 3650) -> pd.DataFrame:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=int(CLICKHOUSE_PORT),
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
    )
    try:
        sql = (
            "SELECT " + ", ".join(FIELDS) + " FROM promotions FINAL "
            "WHERE date >= now() - INTERVAL %(days)s DAY"
        )
        result = client.query(sql, parameters={"days": days})
        df = pd.DataFrame(result.result_rows, columns=FIELDS)
    finally:
        client.close()

    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["original_price"] > 0) & (df["promotion_price"] > 0)]
    return df.sort_values(by=["date"]).reset_index(drop=True)