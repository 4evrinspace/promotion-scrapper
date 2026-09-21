import csv
import io
import os

from fastapi import FastAPI, Query
from fastapi.responses import Response
from prometheus_client import make_asgi_app


CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD")

app = FastAPI(title="Product Promotions API")
app.mount("/metrics", make_asgi_app())
FIELDS = [
    "canonical_product_id", "name", "shop", "date",
    "original_price", "promotion_price", "match_status",
]


def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def clickhouse_client():
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=int(CLICKHOUSE_PORT),
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
    )


@app.get("/products.csv")
def get_csv():
    fields = ", ".join(FIELDS)
    sql = "SELECT " + fields + " FROM promotions FINAL ORDER BY date DESC LIMIT 100"

    db = clickhouse_client()
    try:
        rows = db.query(sql).result_rows
    finally:
        db.close()

    result = io.StringIO()
    writer = csv.writer(result)
    writer.writerow(FIELDS)
    for row in rows:
        writer.writerow([csv_value(value) for value in row])

    return Response(
        content=result.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=products.csv"},
    )


@app.get("/analytics/summary")
def analytics_summary(days: int = Query(default=30, ge=1, le=3650)):
    """Return analytical aggregates directly from the ClickHouse source of truth."""
    db = clickhouse_client()
    try:
        result = db.query(
            """
            SELECT
                count() AS observations,
                uniqExact(canonical_product_id) AS products,
                uniqExact(shop) AS shops,
                round(avg((1 - promotion_price / original_price) * 100), 2) AS avg_discount_pct,
                round(quantile(0.5)((1 - promotion_price / original_price) * 100), 2) AS median_discount_pct
            FROM promotions FINAL
            WHERE date >= now() - INTERVAL %(days)s DAY
            """,
            parameters={"days": days},
        )
        row = result.first_item
        return dict(zip(result.column_names, row))
    finally:
        db.close()


@app.get("/analytics/by-shop")
def analytics_by_shop(days: int = Query(default=30, ge=1, le=3650)):
    """Return discount and coverage aggregates grouped by shop."""
    db = clickhouse_client()
    try:
        result = db.query(
            """
            SELECT
                shop,
                count() AS observations,
                uniqExact(canonical_product_id) AS products,
                round(avg((1 - promotion_price / original_price) * 100), 2) AS avg_discount_pct,
                round(quantile(0.5)((1 - promotion_price / original_price) * 100), 2) AS median_discount_pct
            FROM promotions FINAL
            WHERE date >= now() - INTERVAL %(days)s DAY
            GROUP BY shop
            ORDER BY observations DESC
            """,
            parameters={"days": days},
        )
        return [dict(zip(result.column_names, row)) for row in result.result_rows]
    finally:
        db.close()
