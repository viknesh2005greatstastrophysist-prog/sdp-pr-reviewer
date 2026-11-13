from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .agents import SPECIALIZATIONS, LLMReviewAgent
from .llm import OpenAICompatibleClient
from .pipeline import review_diff
from .settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence-grounded advisory PR reviewer")
    parser.add_argument("diff", type=Path)
    parser.add_argument("--repo", type=Path, help="Trusted or isolated repository checkout")
    parser.add_argument("--offline", action="store_true", help="Disable LLM specialists")
    args = parser.parse_args()
    config = Settings.from_env()
    agents = None
    if config.llm_api_key and not args.offline:
        client = OpenAICompatibleClient(
            api_key=config.llm_api_key, model=config.llm_model, base_url=config.llm_base_url
        )
        agents = [LLMReviewAgent(category, client) for category in SPECIALIZATIONS]
    result = asyncio.run(
        review_diff(
            args.diff.read_text(encoding="utf-8"),
            str(args.repo.resolve()) if args.repo else None,
            agents,
            agent_timeout=config.agent_timeout,
        )
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
