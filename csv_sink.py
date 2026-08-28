import csv
import json
import os
from pathlib import Path

from confluent_kafka import Consumer
from prometheus_client import Counter, start_http_server


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
CSV_FILE = Path(os.getenv("CSV_FILE"))
METRICS_PORT = int(os.getenv("METRICS_PORT"))
ROWS_WRITTEN = Counter("csv_sink_rows_written_total", "")

CSV_HEADERS = [
    "shop", "source_product_id", "canonical_id", "normalized_name",
    "name_match_method", "name_match_confidence", "current_price_kopecks",
    "old_price_kopecks", "has_promotion", "in_stock", "source_url", "collected_at",
]


def main():
    start_http_server(METRICS_PORT)
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "csv-sink",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([NORMALIZED_PRODUCTS_TOPIC])
    is_new_file = not CSV_FILE.exists()

    with CSV_FILE.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_HEADERS, extrasaction="ignore")
        if is_new_file:
            writer.writeheader()

        try:
            while True:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    print(msg.error())
                    continue
                writer.writerow(json.loads(msg.value()))
                file.flush()
                ROWS_WRITTEN.inc()
        finally:
            consumer.close()


if __name__ == "__main__":
    main()
