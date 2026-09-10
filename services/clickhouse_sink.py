import json
import os
from datetime import datetime
from decimal import Decimal

from prometheus_client import Counter, start_http_server


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD")
METRICS_PORT = os.getenv("METRICS_PORT")
ROWS_WRITTEN = Counter("clickhouse_sink_rows_written_total", "")
ROWS_REJECTED = Counter("clickhouse_sink_rows_rejected_total", "")

SQL = """
CREATE TABLE IF NOT EXISTS promotions (
    canonical_product_id String,
    name String,
    shop LowCardinality(String),
    date DateTime64(3, 'UTC'),
    original_price Decimal(12, 2),
    promotion_price Decimal(12, 2),
    match_status LowCardinality(String)
) ENGINE = ReplacingMergeTree
ORDER BY (canonical_product_id, shop, date, original_price, promotion_price)
"""

FIELDS = [
    "canonical_product_id", "name", "shop", "date",
    "original_price", "promotion_price", "match_status",
]
EVENT_FIELDS = {
    "canonical_product_id", "name", "shop", "date",
    "original_price_kopecks", "promotion_price_kopecks", "match_status",
}


def rubles(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Price must be a positive integer")
    if value > 999_999_999_999:
        raise ValueError("Price is too large")
    return Decimal(value) / Decimal(100)


def make_row(data):
    if not isinstance(data, dict) or set(data) != EVENT_FIELDS:
        raise ValueError("Normalized product has invalid fields")
    for field in ["canonical_product_id", "name", "shop", "date", "match_status"]:
        if not isinstance(data[field], str) or not data[field]:
            raise ValueError(field + " must be a non-empty string")
    limits = {
        "canonical_product_id": 100,
        "name": 1000,
        "shop": 50,
        "date": 100,
        "match_status": 20,
    }
    for field, limit in limits.items():
        if len(data[field]) > limit:
            raise ValueError(field + " is too long")
    if data["match_status"] not in ["confirmed", "auto_matched", "provisional", "review"]:
        raise ValueError("match_status is invalid")
    date = datetime.fromisoformat(data["date"])
    if date.tzinfo is None:
        raise ValueError("date must contain a timezone")
    original = rubles(data["original_price_kopecks"])
    promotion = rubles(data["promotion_price_kopecks"])
    if original <= promotion:
        raise ValueError("Promotion price must be lower than original price")
    return [
        data["canonical_product_id"], data["name"], data["shop"], date,
        original, promotion, data["match_status"],
    ]


def main():
    import clickhouse_connect
    from confluent_kafka import Consumer

    db = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=int(CLICKHOUSE_PORT),
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
    )
    db.command(SQL)
    columns = [row[0] for row in db.query("DESCRIBE TABLE promotions").result_rows]
    if columns != FIELDS:
        raise RuntimeError("ClickHouse promotions table has an incompatible schema")
    start_http_server(int(METRICS_PORT))
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "clickhouse-sink",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
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

            try:
                data = json.loads(msg.value())
                row = make_row(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                print(f"Invalid normalized product: {error}")
                ROWS_REJECTED.inc()
                consumer.commit(message=msg, asynchronous=False)
                continue
            db.insert("promotions", [row], column_names=FIELDS)
            ROWS_WRITTEN.inc()
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()
        db.close()


if __name__ == "__main__":
    main()
