"""SysEdge Amygdala Evaluator — real-time rule matching.

Adapted from Cortices (github.com/justifiai/cortices).
Standalone: no ArcadeDB, no daemon, no embeddings — keyword matching only.
Rules are loaded from a JSON cache file written by load_rules.py.

Three hook pathways:
  evaluate_stimulus()    — UserPromptSubmit: match user message against general rules
  evaluate_action()      — PreToolUse: match tool input against all rules; may block
  evaluate_post_action() — PostToolUse: validate what was written; inject feedback
"""

import json
import logging
import math
import os

logger = logging.getLogger("sysedge.amygdala")


def get_default_cache_path() -> str:
    return os.path.join(os.getcwd(), "data", "amygdala_cache.json")


# ── Cache I/O ──────────────────────────────────────────────────────────────

def load_rule_cache(cache_path: str = None) -> list[dict]:
    """Load rules from the JSON cache. Returns list of rule dicts."""
    if cache_path is None:
        cache_path = get_default_cache_path()
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path) as f:
            return json.load(f) or []
    except (json.JSONDecodeError, IOError):
        return []


# ── Pathway 1: Stimulus (UserPromptSubmit) ─────────────────────────────────

def evaluate_stimulus(prompt: str, rules: list[dict], max_rules: int = 3) -> dict:
    """Match user's message against general (non-code) rules."""
    if not prompt.strip() or not rules:
        return {"matched": False, "rules": [], "injection_text": ""}

    general_rules = [r for r in rules if not r.get("check_code", False)]
    matched = _match_rules(prompt, general_rules, max_rules)

    if not matched:
        return {"matched": False, "rules": [], "injection_text": ""}

    return {
        "matched": True,
        "rules": matched,
        "injection_text": _format_injection(matched, "stimulus"),
    }


# ── Pathway 2: Action (PreToolUse) ────────────────────────────────────────

def evaluate_action(tool_name: str, tool_input: dict, rules: list[dict],
                    max_rules: int = 5) -> dict:
    """Match tool input against all rules. May hard-block on critical guardrails."""
    if not rules:
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    file_path = tool_input.get("file_path", "")
    if file_path.endswith(".md"):
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    scoped = _filter_by_project(rules, file_path)
    if not scoped:
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    context_parts = []
    if tool_name == "Write":
        context_parts += [file_path, tool_input.get("content", "")]
    elif tool_name == "Edit":
        context_parts += [file_path, tool_input.get("old_string", ""),
                          tool_input.get("new_string", "")]
    elif tool_name == "Bash":
        context_parts += [tool_input.get("command", ""),
                          tool_input.get("description", "")]
    else:
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    context = " ".join(context_parts)
    if not context.strip():
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    matched = _match_rules(context, scoped, max_rules)
    if not matched:
        return {"action": "allow", "injection_text": "", "block_reason": ""}

    crit_guards = [r for r in matched
                   if r.get("severity") == "critical" and r.get("rule_type") == "guardrail"]
    if crit_guards:
        return {
            "action": "block",
            "injection_text": "",
            "block_reason": f"BLOCKED by SysEdge Amygdala:\n{crit_guards[0]['response']}",
        }

    return {
        "action": "warn",
        "injection_text": _format_injection(matched, "action"),
        "block_reason": "",
    }


# ── Pathway 3: Post-validate (PostToolUse) ────────────────────────────────

def evaluate_post_action(tool_name: str, tool_input: dict, rules: list[dict],
                         max_rules: int = 3) -> dict:
    """Validate what was written. Inject feedback for next turn."""
    if not rules:
        return {"matched": False, "injection_text": ""}

    file_path = tool_input.get("file_path", "")
    if file_path.endswith(".md"):
        return {"matched": False, "injection_text": ""}

    scoped = _filter_by_project(rules, file_path)
    if not scoped:
        return {"matched": False, "injection_text": ""}

    context_parts = []
    if tool_name == "Write":
        context_parts += [file_path, tool_input.get("content", "")]
    elif tool_name == "Edit":
        context_parts += [file_path, tool_input.get("new_string", "")]

    context = " ".join(context_parts)
    if not context.strip():
        return {"matched": False, "injection_text": ""}

    matched = _match_rules(context, scoped, max_rules)
    if not matched:
        return {"matched": False, "injection_text": ""}

    return {"matched": True, "injection_text": _format_injection(matched, "validation")}


# ── Internals ─────────────────────────────────────────────────────────────

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_STOP_WORDS = {
    "sys",  # too generic — only meaningful as part of "sys_graph"
    "the", "a", "an", "is", "was", "are", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "and", "but", "or", "not",
    "no", "so", "yet", "also", "that", "this", "it", "its", "they",
    "them", "their", "he", "she", "his", "her", "we", "our", "you",
    "your", "my", "me", "up", "out", "if", "then", "when", "how",
    "what", "which", "who", "why", "let", "just", "about", "want",
    "need", "like", "think", "know", "make", "take", "some", "any",
    "use", "set", "file", "new", "get", "def", "self", "none", "true",
    "false", "return", "import", "class", "str", "int", "list", "dict",
    "bool", "type", "name", "value", "data", "path",
}


def _extract_keywords(text: str) -> set[str]:
    import re
    # \b\w{3,}\b extracts word-character tokens (letters, digits, underscores) of
    # length ≥ 3. This correctly handles "run('DETACH" → ["run", "detach"] and
    # keeps "sys_graph" as one token while splitting on punctuation and hyphens.
    tokens = re.findall(r'\b\w{3,}\b', text.lower())
    return {t for t in tokens if t not in _STOP_WORDS}


def _filter_by_project(rules: list[dict], file_path: str) -> list[dict]:
    if not file_path:
        return rules
    return [r for r in rules
            if not r.get("project_paths")
            or any(file_path.startswith(p) for p in r["project_paths"])]


def _match_rules(context: str, rules: list[dict], max_rules: int) -> list[dict]:
    keywords = _extract_keywords(context)
    if not keywords:
        return []

    matched = {}
    for rule in rules:
        rule_keywords = _extract_keywords(rule.get("trigger", ""))
        overlap = keywords & rule_keywords
        # Require at least 2 overlapping keywords to avoid single-word false positives
        if len(overlap) >= 2:
            matched[rule.get("rule_id", id(rule))] = rule

    if not matched:
        return []

    return sorted(
        matched.values(),
        key=lambda r: (_SEVERITY_ORDER.get(r.get("severity", "medium"), 3),
                       -r.get("confirmed_count", 0)),
    )[:max_rules]


def _format_injection(rules: list[dict], pathway: str) -> str:
    if not rules:
        return ""

    icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
    headers = {
        "stimulus": "[AMYGDALA — Rules relevant to this request:]",
        "action":   "[AMYGDALA — Rules relevant to this operation:]",
        "validation": "[AMYGDALA — Post-action review:]",
    }

    lines = [headers.get(pathway, "[AMYGDALA — Relevant rules:]")]
    for rule in rules:
        icon = icons.get(rule.get("severity", "medium"), "⚪")
        lines.append(f"  {icon} [{rule.get('rule_type','warning').upper()}] {rule.get('response','')}")
        if rule.get("context"):
            lines.append(f"     Why: {rule['context']}")
    lines.append("[End of amygdala rules — apply these before proceeding]")
    return "\n".join(lines)
