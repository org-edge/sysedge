"""Data models for the SysEdge Amygdala guard system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


class RuleType(str, Enum):
    WARNING = "warning"       # "Last time we did X, it broke Y"
    LESSON = "lesson"         # "Approach A works better than B here"
    GUARDRAIL = "guardrail"   # "Never do X" (hard guidance)
    PREFERENCE = "preference"  # "Always do X this way" (soft guidance)


class Severity(str, Enum):
    CRITICAL = "critical"  # Hard block on PreToolUse if keyword-matched
    HIGH = "high"          # Inject warning — significant risk
    MEDIUM = "medium"      # Inject guidance — helpful reminder
    LOW = "low"            # Inject suggestion — minor preference


class RuleSource(str, Enum):
    ONBOARDING = "onboarding"        # Loaded from YAML at setup
    USER_FEEDBACK = "user_feedback"  # User explicitly corrected Claude
    CLAUDE_MD = "claude_md"          # Imported from CLAUDE.md


@dataclass
class AmygdalaRule:
    rule_id: str = field(default_factory=lambda: f"rule_{uuid.uuid4().hex[:12]}")
    rule_type: RuleType = RuleType.WARNING
    trigger: str = ""
    response: str = ""
    severity: Severity = Severity.MEDIUM
    source: RuleSource = RuleSource.ONBOARDING
    context: str = ""
    check_code: bool = False  # True → only fire on Write/Edit, not on user prompt
    project_paths: list[str] = field(default_factory=list)
    confirmed_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
