from __future__ import annotations

import argparse
import json

from git_query_agent import GitQueryAgent
from git_query_tools import GatewayClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the natural-language git query agent.")
    parser.add_argument("question", help="Natural-language question to ask the git query agent.")
    parser.add_argument("--project-id", help="Default project ID, for example demo.")
    parser.add_argument("--filename", help="Default filename, for example student.json.")
    parser.add_argument("--base-url", help="Gateway base URL, for example http://127.0.0.1:8080.")
    parser.add_argument("--api-key", help="Gateway service API key.")
    parser.add_argument("--bearer-token", help="Gateway bearer token.")
    parser.add_argument("--username", help="Gateway login username.")
    parser.add_argument("--password", help="Gateway login password.")
    parser.add_argument("--include-raw", action="store_true", help="Include raw LLM responses.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    gateway_client = GatewayClient(
        base_url=args.base_url,
        api_key=args.api_key,
        bearer_token=args.bearer_token,
    )
    if args.username or args.password:
        if not (args.username and args.password):
            raise SystemExit("--username and --password must be provided together")
        gateway_client.login(args.username, args.password)

    agent = GitQueryAgent(gateway_client=gateway_client)
    result = agent.answer(
        question=args.question,
        project_id=args.project_id,
        filename=args.filename,
        include_raw=args.include_raw,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
