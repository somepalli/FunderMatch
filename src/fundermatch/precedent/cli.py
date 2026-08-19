"""CLI for seeding invented precedents into Qdrant."""

from __future__ import annotations

import argparse
from pathlib import Path

from fundermatch.precedent.corpus import load_cases
from fundermatch.precedent.embedder import BgeM3Config, BgeM3Embedder
from fundermatch.precedent.store import QdrantPrecedentConfig, QdrantPrecedentStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("data/synthetic_decided_loans.jsonl"),
    )
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6999")
    parser.add_argument("--collection", default="fundermatch_precedents")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--recreate", action="store_true")
    args = parser.parse_args()

    cases = load_cases(args.corpus)
    embedder = BgeM3Embedder(BgeM3Config(snapshot_dir=args.model_dir))
    store = QdrantPrecedentStore(
        QdrantPrecedentConfig(url=args.qdrant_url, collection=args.collection)
    )
    count = store.seed(cases, embedder, recreate=args.recreate)
    print(f"seeded {count} synthetic precedents into {args.collection}")
