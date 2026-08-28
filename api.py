import json
import os
import threading

import redis
from confluent_kafka import Consumer
from fastapi import FastAPI, HTTPException
from prometheus_client import make_asgi_app


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
NORMALIZED_PRODUCTS_TOPIC = os.getenv("NORMALIZED_PRODUCTS_TOPIC")
REDIS_URL = os.getenv("REDIS_URL")

app = FastAPI(title="Product Promotions API")
app.mount("/metrics", make_asgi_app())
cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def product_key(data):
    return "latest_product:" + data["shop"] + ":" + data["source_product_id"]


def consume_products():
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP_SERVERS,
            "group.id": "api-cache",
            "auto.offset.reset": "latest",
        }
    )
    consumer.subscribe([NORMALIZED_PRODUCTS_TOPIC])
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            product = json.loads(msg.value())
            cache.set(product_key(product), json.dumps(product, ensure_ascii=False))
    finally:
        consumer.close()


@app.on_event("startup")
def start_consumer():
    t = threading.Thread(target=consume_products)
    t.daemon = True
    t.start()


@app.get("/products/{shop}/{product_id}")
def get_product(shop: str, product_id: str):
    data = cache.get(f"latest_product:{shop}:{product_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Product not found")
    return json.loads(data)


@app.get("/promotions/{shop}")
def get_promotions(shop: str):
    res = []
    for key in cache.scan_iter(f"latest_product:{shop}:*"):
        item = cache.get(key)
        if item:
            data = json.loads(item)
            if data["has_promotion"]:
                res.append(data)
    return res
