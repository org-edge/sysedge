#!/usr/bin/env python3
"""SysEdge Amygdala Guard — standalone Claude Code hook script.

Reads a JSON payload from stdin, evaluates it against the rule cache,
and outputs a hook response (injection or block). Never raises — always
exits 0 on error so Claude Code is never blocked by a guard failure.

Usage (wired by setup.py into .claude/settings.json):
  guard.py stimulus-check   ← UserPromptSubmit
  guard.py action-enforce   ← PreToolUse
  guard.py post-validate    ← PostToolUse
"""

import json
import os
import sys

# Resolve evaluator relative to this file (works from any CWD)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluator import load_rule_cache, evaluate_stimulus, evaluate_action, evaluate_post_action


def main():
    event_type = sys.argv[1] if len(sys.argv) > 1 else ""
    if not event_type:
        sys.exit(0)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    rules = load_rule_cache()
    if not rules:
        sys.exit(0)

    if event_type == "stimulus-check":
        prompt = payload.get("prompt", "")
        result = evaluate_stimulus(prompt, rules)
        if result["matched"]:
            _output_injection("UserPromptSubmit", result["injection_text"])

    elif event_type == "action-enforce":
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        result = evaluate_action(tool_name, tool_input, rules)
        if result["action"] == "block":
            print(result["block_reason"], file=sys.stderr)
            sys.exit(2)
        elif result["action"] == "warn" and result["injection_text"]:
            _output_injection("PreToolUse", result["injection_text"])

    elif event_type == "post-validate":
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        result = evaluate_post_action(tool_name, tool_input, rules)
        if result["matched"]:
            _output_injection("PostToolUse", result["injection_text"])


def _output_injection(event_name: str, text: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }))


if __name__ == "__main__":
    _event_type = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        main()
    except Exception:
        if _event_type == "action-enforce":
            print("AMYGDALA ERROR: evaluator crashed — blocking action as precaution", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
