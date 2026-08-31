import csv
import io
import os

import clickhouse_connect
from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import make_asgi_app


CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT"))

app = FastAPI(title="Product Promotions API")
app.mount("/metrics", make_asgi_app())
FIELDS = [
    "name", "shop", "date", "original_price", "promotion_price",
]


@app.get("/products.csv")
def get_csv():
    fields = ", ".join(FIELDS)
    sql = "SELECT " + fields + " FROM products ORDER BY date DESC LIMIT 100"

    db = clickhouse_connect.get_client(host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT)
    rows = db.query(sql).result_rows

    result = io.StringIO()
    writer = csv.writer(result)
    writer.writerow(FIELDS)
    writer.writerows(rows)

    return Response(
        content=result.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=products.csv"},
    )
