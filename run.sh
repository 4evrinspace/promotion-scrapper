#!/usr/bin/env bash

set -u

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$script_dir" || exit 1

interval="${CSV_FETCH_INTERVAL_SECONDS:-600}"
output_file="${CSV_OUTPUT_FILE:-$script_dir/data/products.csv}"
temp_file="${output_file}.tmp"

case "$interval" in
  ""|*[!0-9]*)
    echo "CSV_FETCH_INTERVAL_SECONDS must be a positive integer" >&2
    exit 1
    ;;
esac

if [ "$interval" -lt 1 ]; then
  echo "CSV_FETCH_INTERVAL_SECONDS must be greater than zero" >&2
  exit 1
fi

if [ -z "$output_file" ]; then
  echo "CSV_OUTPUT_FILE must not be empty" >&2
  exit 1
fi

if ! mkdir -p "$(dirname -- "$output_file")"; then
  echo "Unable to create the CSV output directory" >&2
  exit 1
fi

trap 'rm -f "$temp_file"' EXIT

echo "Building and starting containers..."
if ! docker compose up -d --build; then
  echo "Unable to start containers" >&2
  exit 1
fi

echo "CSV output: $output_file"
echo "Download interval: $interval seconds"

download_csv() {
  if docker compose exec -T api python3 -c \
    "import sys, urllib.request; sys.stdout.buffer.write(urllib.request.urlopen('http://localhost:8080/products.csv', timeout=30).read())" \
    > "$temp_file"; then
    if mv "$temp_file" "$output_file"; then
      echo "CSV updated: $(date '+%Y-%m-%d %H:%M:%S')"
      return 0
    fi
  fi

  rm -f "$temp_file"
  return 1
}

until download_csv; do
  echo "API is not ready, retrying in 5 seconds" >&2
  sleep 5
done

while true; do
  sleep "$interval"
  if ! download_csv; then
    echo "CSV download failed, the next attempt will be made later" >&2
  fi
done
