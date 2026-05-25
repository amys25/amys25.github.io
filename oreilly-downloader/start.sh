#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Installing dependencies..."
pip install -r requirements.txt -q

echo "Starting O'Reilly Book Downloader at http://localhost:8000"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
