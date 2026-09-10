import csv
import io
import os

from fastapi import FastAPI
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


@app.get("/products.csv")
def get_csv():
    import clickhouse_connect

    fields = ", ".join(FIELDS)
    sql = "SELECT " + fields + " FROM promotions FINAL ORDER BY date DESC LIMIT 100"

    db = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=int(CLICKHOUSE_PORT),
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
    )
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
