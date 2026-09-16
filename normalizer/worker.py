import json
import os
import re

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from normalizer.product_identity import (
    PENDING_MATCHES_KEY,
    get_gtin,
    resolve_identity,
)
from scrapers.base import validate_product


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
RAW_PRODUCTS_TOPIC = os.getenv("RAW_PRODUCTS_TOPIC")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
REDIS_URL = os.getenv("REDIS_URL")
METRICS_PORT = os.getenv("METRICS_PORT")
MESSAGES_NORMALIZED = Counter("normalizer_messages_total", "")
PRODUCTS_WITHOUT_PROMOTION = Counter("normalizer_products_without_promotion_total", "")
NORMALIZATION_ERRORS = Counter("normalizer_errors_total", "")
MATCH_RESULTS = Counter("normalizer_match_results_total", "", ["method", "status"])
GTIN_RESULTS = Counter("normalizer_gtin_total", "", ["status"])
MATCH_CONFLICTS = Counter("normalizer_match_conflicts_total", "", ["field"])
MATCH_CONFIDENCE = Histogram(
    "normalizer_match_confidence", "", ["method"],
    buckets=[0, 0.5, 0.68, 0.8, 0.88, 0.95, 1.0],
)
PENDING_QUEUE_SIZE = Gauge("normalizer_pending_matches", "")


def price(value):
    if value is None:
        return None
    text = str(value).lower().replace("\u00a0", " ").strip()
    text = re.sub(r"[-+]?\d+(?:[,.]\d+)?\s*%", "", text)
    kopecks_match = re.search(r"(\d{1,2})\s*коп", text)
    currency_match = re.search(
        r"([0-9][0-9\s.,]*?)\s*(?:₽|руб(?:\.|ль|ля|лей)?|р\.)(?=\s|$|/)",
        text,
    )
    if currency_match:
        amount = currency_match.group(1).strip()
        if kopecks_match:
            rubles = re.sub(r"\D", "", amount) or "0"
            return int(rubles) * 100 + int(kopecks_match.group(1))
        return _price_number(amount)
    if kopecks_match:
        return int(kopecks_match.group(1))

    number = re.search(r"[0-9][0-9\s.,]*", text)
    return _price_number(number.group(0).strip()) if number else None


def _price_number(text):
    decimal = re.search(r"[,.]([0-9]{1,2})$", text)
    if decimal:
        rubles = re.sub(r"\D", "", text[:decimal.start()]) or "0"
        kopecks = (decimal.group(1) + "0")[:2]
        return int(rubles) * 100 + int(kopecks)
    numbers = re.findall(r"[0-9]+", text)
    if not numbers:
        return None
    if len(numbers) > 1 and len(numbers[-1]) <= 2:
        rubles = "".join(numbers[:-1])
        kopecks = (numbers[-1] + "0")[:2]
        return int(rubles) * 100 + int(kopecks)
    return int("".join(numbers)) * 100


def normalize_product(product, cache):
    validate_product(product, product.get("shop"))
    current_price = price(product.get("current_price_text", ""))
    old_price = price(product.get("old_price_text", ""))

    raw_gtin = product["gtin_raw"]
    if raw_gtin:
        status = "valid" if get_gtin(product) else "invalid"
    else:
        status = "missing"
    GTIN_RESULTS.labels(status=status).inc()
    if not old_price or not current_price or old_price <= current_price:
        PRODUCTS_WITHOUT_PROMOTION.inc()
        return None

    identity = resolve_identity(product, cache)
    MATCH_RESULTS.labels(method=identity.match_method, status=identity.status).inc()
    MATCH_CONFIDENCE.labels(method=identity.match_method).observe(identity.confidence)
    conflicts = list(identity.evidence.get("conflicts", []))
    for candidate in identity.evidence.get("candidates", []):
        conflicts.extend(candidate.get("evidence", {}).get("conflicts", []))
    for field in set(conflicts):
        MATCH_CONFLICTS.labels(field=field).inc()
    PENDING_QUEUE_SIZE.set(cache.hlen(PENDING_MATCHES_KEY))

    if identity.status == "quarantine":
        return None

    return {
        "canonical_product_id": identity.canonical_id,
        "name": identity.canonical_name,
        "shop": product["shop"],
        "date": product["collected_at"],
        "original_price_kopecks": old_price,
        "promotion_price_kopecks": current_price,
        "match_status": identity.status,
    }


def main():
    import redis
    from confluent_kafka import Consumer, Producer

    start_http_server(int(METRICS_PORT))
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "product-normalizer",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS})
    consumer.subscribe([RAW_PRODUCTS_TOPIC])

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                print(msg.error())
                continue

            try:
                raw = json.loads(msg.value())
                if not isinstance(raw, dict):
                    raise ValueError("Kafka message must contain a JSON object")
                product = normalize_product(raw, cache)
                if product is None:
                    consumer.commit(message=msg, asynchronous=False)
                    continue

                delivery_errors = []

                def delivered(error, _message):
                    if error:
                        delivery_errors.append(str(error))

                producer.produce(
                    NORMALIZED_PRODUCTS_TOPIC,
                    key=msg.key(),
                    value=json.dumps(product, ensure_ascii=False),
                    on_delivery=delivered,
                )
                not_sent = producer.flush(10)
                if not_sent or delivery_errors:
                    raise RuntimeError("Normalized product was not sent to Kafka")
                MESSAGES_NORMALIZED.inc()
                consumer.commit(message=msg, asynchronous=False)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                NORMALIZATION_ERRORS.inc()
                print(f"Normalization error: {error}")
                consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
