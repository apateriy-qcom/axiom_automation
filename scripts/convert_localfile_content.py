#!/usr/bin/env python3
"""
Convert a text file into an escaped JSON string suitable for localFile.content.

Behavior:
- Read UTF-8 text.
- Normalize line endings to CRLF.
- Emit JSON-escaped string (including surrounding quotes).
"""

import argparse
import json
from pathlib import Path


def to_crlf(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert file content into JSON-escaped CRLF string."
    )
    parser.add_argument("input_file", help="Path to input text file")
    args = parser.parse_args()

    content = Path(args.input_file).read_text(encoding="utf-8")
    print(json.dumps(to_crlf(content), ensure_ascii=False))


if __name__ == "__main__":
    main()
