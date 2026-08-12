#!/bin/bash
# Usage: ./scripts/load-env-vars.sh [.env file path]
# Reads a .env file and sets each variable in the active AZD environment.
set -e

ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: $ENV_FILE not found. Copy .env.example to .env and fill in your values."
  exit 1
fi

echo "Loading env vars from $ENV_FILE into AZD environment..."

while IFS= read -r line || [ -n "$line" ]; do
  # Skip comments and blank lines
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue

  key="${line%%=*}"
  value="${line#*=}"

  # Skip placeholder values
  [[ "$value" == "<"*">" ]] && continue
  [[ -z "$value" ]] && continue

  azd env set "$key" "$value"
  echo "  ✓ $key"
done < "$ENV_FILE"

echo "Done."
