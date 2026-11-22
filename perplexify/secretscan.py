"""Keeps secrets out of the index.

Passages from a code repository are sent to a cloud model whenever a question is asked, so files that
are meant to hold secrets are never indexed, and strings that look like credentials are replaced with a
placeholder before anything is stored. This is a safety net, not a guarantee: do not index repositories
whose history or files you would not be comfortable sending to your chosen provider.
"""
from __future__ import annotations

import fnmatch
import re

SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
    ".git-credentials", "*.tfstate", "*.tfvars", "*.kubeconfig", "kubeconfig", "*.secret", "*.secrets",
    "credentials", "credentials.json", "credentials.yml", "credentials.yaml", "credentials.ini", "credentials.xml",
    "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml", "secrets.ini", "secrets.txt",
    "service-account*.json", "*serviceaccount*.json", "*-credentials.json",
)

_FILE_RE = re.compile("|".join(fnmatch.translate(p) for p in SECRET_FILE_PATTERNS), re.I)

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "[REDACTED GITHUB TOKEN]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), "[REDACTED GITLAB TOKEN]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED API KEY]"),
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"), "[REDACTED API KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED API KEY]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "[REDACTED SLACK TOKEN]"),
    (re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}"), "[REDACTED STRIPE KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED JWT]"),
]

# password = "hunter2hunter2"  /  "api_key": "abcd..."  ->  keep the name, hide the value
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (\b(?:api[_-]?key|apikey|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key
        |password|passwd|pwd)["']?\s*[:=]\s*)
    (["'])([^"'\s]{12,})\2""")


def is_secret_file(name: str) -> bool:
    return bool(_FILE_RE.fullmatch(name))


def redact(text: str) -> tuple[str, int]:
    """Replace credential-looking strings. Returns (clean text, number of replacements)."""
    count = 0
    for pattern, replacement in _PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    text, n = _ASSIGNMENT.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}", text)
    return text, count + n
