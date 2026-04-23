#!/usr/bin/env python3
"""Generate base64(client_id:client_secret) for OAuth Basic auth header."""

import argparse
import base64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Base64 encoded client credentials for Basic auth."
    )
    parser.add_argument("--client-id", required=True, help="OAuth client ID")
    parser.add_argument("--client-secret", required=True, help="OAuth client secret")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = f"{args.client_id}:{args.client_secret}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
