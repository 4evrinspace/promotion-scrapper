import json
import os
import sys
from pathlib import Path

import redis

from product_identity import (
    PRODUCT_KEY_PREFIX,
    SYNONYMS_KEY,
    TOKEN_KEY_PREFIX,
    normalize_text,
)


REDIS_URL = os.getenv("REDIS_URL")


def load_catalog(file_path, cache):
    with file_path.open(encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            id = item["canonical_id"]
            aliases = []
            for name in item.get("aliases", []):
                aliases.append(normalize_text(name))
            aliases.append(normalize_text(item["canonical_name"]))
            search_name = normalize_text(item["canonical_name"])
            card = {
                "canonical_name": item["canonical_name"],
                "search_name": search_name,
                "is_fallback": False,
            }

            pipeline = cache.pipeline()
            pipeline.set(
                f"{PRODUCT_KEY_PREFIX}{id}",
                json.dumps(card, ensure_ascii=False),
            )
            for alias in aliases:
                pipeline.hset(SYNONYMS_KEY, alias, id)
                for token in alias.split():
                    pipeline.sadd(f"{TOKEN_KEY_PREFIX}{token}", id)
            for gtin in item.get("gtins", []):
                pipeline.set(f"catalog:gtin:{gtin}", id)
            pipeline.execute()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 catalog_loader.py catalog.jsonl")
        sys.exit()
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    load_catalog(Path(sys.argv[1]), cache)
