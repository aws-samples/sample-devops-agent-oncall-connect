# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ONE untrusted-text sanitizer, shared by every component that carries attacker-influenceable text.

Single source file, shipped as `sanitize.py` at the root of each package that imports it, so the
copies cannot drift:
- incident_opener, oncall_caller, connect_handler: Terraform `archive_file` adds this file to the
  Lambda zip (a `source` block per file, see terraform/modules/foundation/main.tf and
  terraform/modules/oncall/{caller,handler}.tf);
- the MCP ticketing server: scripts/build.py copies it next to server.py in build/mcp_server.zip.

What it protects: text an attacker may influence (CloudWatch alarm name/reason, the investigation
summary an LLM wrote over logs, the AI agent's Acknowledge note, DevOps Agent comments, contact
attributes) before it is stored on a ticket, interpolated into the Connect AI agent prompt, handed
back to the DevOps Agent, or filled into an SMS/email template.

Also the shared ticket bounds: the strict ticket id format and the per-ticket comment cap.
"""

import re
import unicodedata

MAX_SUMMARY = 3500  # investigation summary stored on the ticket item (and the default cap)
MAX_FIELD = 100  # short context fields (service, priority, statusText, ticketId)
# Per ticket: at most this many comments. Each comment is capped by its writer (2000 characters at
# most), so comments stay around 100 KB, far below the 400 KB DynamoDB item limit, and the fixed
# state-machine writes (ack, notify, receipt: a handful at most, not capped) always fit.
MAX_COMMENTS = 50

# incident_opener issues "INC-" + the first 8 upper-hex characters of a sha256. Nothing else is a
# ticket id (the per-alarm lock items "ALARM#..." share the table and must never match).
TICKET_ID_RE = re.compile(r"\AINC-[0-9A-F]{8}\Z")


def is_ticket_id(value) -> bool:
    return isinstance(value, str) and bool(TICKET_ID_RE.match(value))


# Format (zero-width, bidi overrides, Unicode tag characters), control (C0/C1), private-use, surrogate
# and unassigned code points are dropped; \t \n \r become a space.
_DROP_CATEGORIES = frozenset(("Cc", "Cf", "Co", "Cs", "Cn"))
# Brackets that could forge the <incident-context> / <oncall-role> tags (ASCII and Unicode look-alikes
# NFKC does not fold), braces that could forge a {{$.Custom.x}} placeholder, back-ticks (markdown code).
_STRIP_CHARS = frozenset("<>{}`\u2039\u203a\u00ab\u00bb\u27e8\u27e9\u3008\u3009\u300a\u300b")
# Fence look-alikes: 2+ dash-class characters (every Unicode Pd and box drawing U+2500-U+257F are
# mapped to "-" first) and 2+ of = # ~ * _ become one space.
_DASH_RUN = re.compile(r"-{2,}")
_MARK_RUN = re.compile(r"[=#~*_]{2,}")
# The prompt's own fence words ("--- END UNTRUSTED SUMMARY ---"), any case, any separator.
_FENCE_PHRASE = re.compile(r"untrusted[\W_]*summary", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def _drop_invisible(ch: str) -> str:
    if ch in "\t\n\r":
        return " "
    return "" if unicodedata.category(ch) in _DROP_CATEGORIES else ch


def _strip_delimiter(ch: str) -> str:
    if ch in _STRIP_CHARS:
        return " "
    if unicodedata.category(ch) == "Pd" or "\u2500" <= ch <= "\u257f":
        return "-"
    return ch


def sanitize_untrusted(text, limit: int = MAX_SUMMARY) -> str:
    """Make attacker-influenceable text safe to store, prompt or send as DATA.

    Order, repeated until the text is stable so removing one marker can never assemble
    another: (1) drop invisible/control code points, (2) NFKC (full-width and other compatibility
    forms fold to ASCII, French accents stay), (3) strip bracket and brace look-alikes, collapse fence
    look-alikes (dash-class and = # ~ * _ runs) and the fence phrase, collapse whitespace. Then cap.
    Non-string input becomes "". This cannot stop injection carried in plain prose (or in homoglyphs
    NFKC does not fold): the prompt still fences the result as untrusted content, the Acknowledge
    needs the callee's key 1, and DevOps Agent writes need operator approval.
    """
    text = text if isinstance(text, str) else ""
    previous = None
    while text != previous:
        previous = text
        text = "".join(_drop_invisible(ch) for ch in text)
        text = unicodedata.normalize("NFKC", text)
        text = "".join(_strip_delimiter(ch) for ch in text)
        text = _DASH_RUN.sub(" ", text)
        text = _MARK_RUN.sub(" ", text)
        text = _FENCE_PHRASE.sub(" ", text)
        text = _SPACES.sub(" ", text).strip()
    return text[:limit].rstrip()
