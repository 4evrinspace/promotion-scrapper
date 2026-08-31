import json
import os
from decimal import Decimal

import clickhouse_connect
from confluent_kafka import Consumer
from prometheus_client import Counter, start_http_server


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT"))
METRICS_PORT = int(os.getenv("METRICS_PORT"))
ROWS_WRITTEN = Counter("clickhouse_sink_rows_written_total", "")

SQL = """
CREATE TABLE IF NOT EXISTS products (
    name String,
    shop LowCardinality(String),
    date DateTime64(3, 'UTC'),
    original_price Nullable(Decimal(12, 2)),
    promotion_price Nullable(Decimal(12, 2))
) ENGINE = MergeTree
ORDER BY (date, shop, name)
"""

FIELDS = ["name", "shop", "date", "original_price", "promotion_price"]


def rubles(value):
    if value is None:
        return None
    return Decimal(value) / Decimal(100)


def main():
    start_http_server(METRICS_PORT)
    db = clickhouse_connect.get_client(host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT)
    db.command(SQL)

    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "clickhouse-sink",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([NORMALIZED_PRODUCTS_TOPIC])

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                print(msg.error())
                continue

            data = json.loads(msg.value())
            old_price = data.get("old_price_kopecks")
            current_price = data.get("current_price_kopecks")
            if old_price is None:
                old_price = current_price

            promotion_price = None
            if data.get("has_promotion"):
                promotion_price = rubles(current_price)

            row = [
                data.get("normalized_name"),
                data.get("shop"),
                data.get("collected_at"),
                rubles(old_price),
                promotion_price,
            ]
            db.insert("products", [row], column_names=FIELDS)
            ROWS_WRITTEN.inc()
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
