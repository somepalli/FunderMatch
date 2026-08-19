"""Call a running FinDocIQ instance and validate its extraction contract."""

from __future__ import annotations

import argparse
import asyncio
import os

from fundermatch.clients.findociq_client import FinDocIQClient, FinDocIQClientConfig
from fundermatch.clients.findociq_contract import ExtractRequest


async def smoke(base_url: str, question: str, question_id: str | None) -> None:
    config = FinDocIQClientConfig(base_url=base_url)
    async with FinDocIQClient(config) as client:
        result = await client.extract(
            ExtractRequest(question=question, question_id=question_id)
        )
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("FINDOCIQ_BASE_URL", "http://127.0.0.1:8989"),
    )
    parser.add_argument("--question", required=True)
    parser.add_argument("--question-id")
    args = parser.parse_args()
    asyncio.run(smoke(args.base_url, args.question, args.question_id))


if __name__ == "__main__":
    main()
