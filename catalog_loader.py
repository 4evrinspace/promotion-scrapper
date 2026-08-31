import json
import os
import sys
from pathlib import Path

import redis

from product_identity import (
    SYNONYMS_KEY,
    normalize_text,
)


REDIS_URL = os.getenv("REDIS_URL")


def load_catalog(file_path, cache):
    with file_path.open(encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            aliases = []
            for name in item.get("aliases", []):
                aliases.append(normalize_text(name))
            aliases.append(normalize_text(item["canonical_name"]))
            universal_name = item["canonical_name"]

            pipeline = cache.pipeline()
            for alias in aliases:
                pipeline.hset(SYNONYMS_KEY, alias, universal_name)
            for gtin in item.get("gtins", []):
                pipeline.hset(SYNONYMS_KEY, "gtin:" + gtin, universal_name)
            pipeline.execute()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 catalog_loader.py catalog.jsonl")
        sys.exit()
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    load_catalog(Path(sys.argv[1]), cache)
