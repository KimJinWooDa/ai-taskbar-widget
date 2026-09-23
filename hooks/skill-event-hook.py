# -*- coding: utf-8 -*-
"""Fast local hook: queue skill events for the widget and print nothing.

The hook never opens the database -- it appends to the widget's inbox file and
the widget (the only writer) ingests it. See skill_tracker's module docstring.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skill_tracker import queue_hook_payload  # noqa: E402


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(
            sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        )
        if isinstance(payload, dict):
            queue_hook_payload(args.client, payload)
    except Exception:
        pass
    # No stdout/additionalContext: the model receives zero extra tokens.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
