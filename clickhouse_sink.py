import json
import os

import clickhouse_connect
from confluent_kafka import Consumer
from prometheus_client import Counter, start_http_server


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT"))
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE")
METRICS_PORT = int(os.getenv("METRICS_PORT"))

ROWS_INSERTED = Counter("clickhouse_sink_rows_inserted_total", "")

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_prices (
    shop LowCardinality(String),
    source_product_id String,
    canonical_id String,
    normalized_name String,
    name_match_method LowCardinality(String),
    name_match_confidence Float32,
    current_price_kopecks Nullable(UInt64),
    old_price_kopecks Nullable(UInt64),
    has_promotion Bool,
    in_stock Bool,
    source_url String,
    collected_at DateTime64(3, 'UTC'),
    normalized_at DateTime64(3, 'UTC')
) ENGINE = MergeTree
ORDER BY (shop, source_product_id, collected_at)
"""

COLUMN_NAMES = [
    "shop",
    "source_product_id",
    "canonical_id",
    "normalized_name",
    "name_match_method",
    "name_match_confidence",
    "current_price_kopecks",
    "old_price_kopecks",
    "has_promotion",
    "in_stock",
    "source_url",
    "collected_at",
    "normalized_at",
]


def to_row(data):
    res = []
    for name in COLUMN_NAMES:
        res.append(data.get(name))
    return res


def main():
    start_http_server(METRICS_PORT)
    client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST, port=CLICKHOUSE_PORT, database=CLICKHOUSE_DATABASE
    )
    client.command(TABLE_SCHEMA)
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

            product = json.loads(msg.value())
            client.insert("product_prices", [to_row(product)], column_names=COLUMN_NAMES)
            ROWS_INSERTED.inc()
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
