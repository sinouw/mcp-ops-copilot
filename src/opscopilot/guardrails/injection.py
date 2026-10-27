"""Prompt-injection handling for untrusted text (ticket bodies, CRM free-text fields).

Detection here is a *signal*, not the defence. The defence is structural: untrusted text is
fenced as data, and every side effect goes through deterministic policy checks that ignore
what the text says. A positive scan only "taints" the run, which tightens policy (every
write needs human approval) and surfaces the signals to the reviewer.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from pydantic import BaseModel

_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿]")
# "i g n o r e" -> "ignore"
_SPACED = re.compile(r"\b(?:[a-z] ){3,}[a-z]\b")

_PATTERNS: list[tuple[str, float, str]] = [
    (r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules|prompts?|policy)", 0.6,
     "override-instructions"),
    (r"\byou are now\b|\bact as\b|\bpretend to be\b", 0.4, "role-reassignment"),
    (r"\b(admin|developer|god|debug|jailbreak) mode\b", 0.5, "privileged-mode"),
    (r"^\s*(system|assistant)\s*(notice|instruction|message)?\s*:", 0.4, "fake-role-header"),
    (r"<\s*/?\s*(system|assistant|untrusted-data|instructions?)\b", 0.5, "tag-breakout"),
    (r"\b(approval|authori[sz]ation)\b.{0,30}\b(already|pre-?)\s*(granted|given|approved)", 0.5,
     "claimed-approval"),
    (r"\bwithout (review|approval|verification|checks?)\b|\bdo not escalate\b", 0.4,
     "bypass-review"),
    (r"\bassistant instruction\b|\bnew (policy|instructions?)\b", 0.5, "embedded-instruction"),
    (r"\b(every|all) (other )?customers?\b.{0,60}\b(emails?|phones?|addresses|list)\b"
     r"|\bcustomer list\b", 0.5, "data-exfiltration"),
    (r"\b(issue_refund|send_reply|escalate_ticket|tool call|function call)\b", 0.3,
     "tool-name-mention"),
]  # fmt: skip
_COMPILED = [(re.compile(p, re.IGNORECASE | re.MULTILINE), w, label) for p, w, label in _PATTERNS]

SUSPICION_THRESHOLD = 0.5


class InjectionReport(BaseModel):
    score: float
    signals: list[str]
    suspected: bool
    obfuscated: bool


def normalize(text: str) -> tuple[str, bool]:
    """NFKC-fold, strip invisible characters and re-join spaced-out words."""
    folded = unicodedata.normalize("NFKC", text)
    stripped = _ZERO_WIDTH.sub("", folded)
    obfuscated = stripped != folded
    lowered = stripped.lower()
    joined = _SPACED.sub(lambda m: m.group(0).replace(" ", ""), lowered)
    obfuscated = obfuscated or joined != lowered
    return joined, obfuscated


def scan(text: str) -> InjectionReport:
    norm, obfuscated = normalize(text)
    score, signals = 0.0, []
    for rx, weight, label in _COMPILED:
        if rx.search(norm):
            score += weight
            signals.append(label)
    if obfuscated and signals:
        score += 0.3
        signals.append("obfuscation")
    return InjectionReport(
        score=round(min(score, 1.0), 2),
        signals=signals,
        suspected=score >= SUSPICION_THRESHOLD,
        obfuscated=obfuscated,
    )


_FENCE_LIKE = re.compile(r"<\s*/?\s*(untrusted-data|system|assistant|instructions?)\b[^>]*>", re.I)


def wrap_untrusted(text: str, source: str, *, max_chars: int = 4000) -> str:
    """Fence untrusted text with a per-call random boundary the text cannot predict.

    Tag-like sequences that could impersonate the fence or a role header are defanged, and
    invisible characters are removed so the model sees what a human reviewer sees.
    """
    clean = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    clean = _FENCE_LIKE.sub("[tag removed]", clean)
    if len(clean) > max_chars:
        clean = clean[:max_chars] + " [truncated]"
    boundary = secrets.token_hex(6)
    return (
        f'<untrusted-data source="{source}" boundary="{boundary}">\n'
        f"{clean}\n"
        f'</untrusted-data boundary="{boundary}">'
    )
