from __future__ import annotations

import argparse
import json

from git_query_tools import GatewayClient, get_available_tools, run_tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run agent git query tools against the gateway.")
    parser.add_argument(
        "tool_name",
        choices=[tool.name for tool in get_available_tools()],
        help="Tool name to execute.",
    )
    parser.add_argument(
        "--project-id",
        required=True,
        help="Project ID, for example demo.",
    )
    parser.add_argument(
        "--filename",
        required=True,
        help="Ontology filename, for example student.json.",
    )
    parser.add_argument(
        "--base-url",
        help="Gateway base URL, for example http://127.0.0.1:8080.",
    )
    parser.add_argument(
        "--api-key",
        help="Gateway service API key.",
    )
    parser.add_argument(
        "--bearer-token",
        help="Gateway bearer token.",
    )
    parser.add_argument(
        "--username",
        help="Gateway login username. If provided together with --password, the CLI will login first.",
    )
    parser.add_argument(
        "--password",
        help="Gateway login password. Use with --username.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    client = None
    if any([args.base_url, args.api_key, args.bearer_token]):
        client = GatewayClient(
            base_url=args.base_url,
            api_key=args.api_key,
            bearer_token=args.bearer_token,
        )
    if client is None:
        client = GatewayClient()
    if args.username or args.password:
        if not (args.username and args.password):
            raise SystemExit("--username and --password must be provided together")
        client.login(args.username, args.password)

    result = run_tool(
        name=args.tool_name,
        arguments={
            "project_id": args.project_id,
            "filename": args.filename,
        },
        client=client,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
