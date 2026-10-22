"""Identifier lint for corpus seeds and the offline distiller.

Deliberately conservative. A false positive means a seed gets skipped and a human looks at
it, which is cheap. A false negative puts customer data into a shared training tool.
`scrub()` is the pre-distillation pass on raw ticket text; `find_pii()` is the gate on
what gets written to corpus/ and loaded by the app.
"""
import re

# Example/placeholder values that are fine to appear in seeds.
_ALLOW = {
    "example.com", "example.org", "example.net", "datadoghq.com", "datadoghq.eu", "ddog-gov.com",
    "us3.datadoghq.com", "us5.datadoghq.com", "ap1.datadoghq.com", "docs.datadoghq.com",
    "app.datadoghq.com", "p.datadoghq.com", "localhost", "github.com", "hub.docker.com",
    "gcr.io", "public.ecr.aws", "docker.io",
}

PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
    "api_key": re.compile(r"\b[a-f0-9]{32}\b"),
    "app_key": re.compile(r"\b[a-f0-9]{40}\b"),
    "public_ipv4": re.compile(
        r"\b(?!10\.)(?!127\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)(?!192\.168\.)(?!0\.)(?!169\.254\.)"
        r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b"),
    "zendesk_ref": re.compile(r"(?i)\b(?:zd|ticket|case)\s*#?\s*\d{5,8}\b|zendesk\.com/agent/tickets/\d+"),
    "org_id": re.compile(r"(?i)\borg(?:anization)?[\s_-]*id\s*[:=#]?\s*\d{3,}\b"),
    "aws_account": re.compile(r"\b\d{12}\b"),
    "phone": re.compile(r"\+\d{1,3}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}\b"),
}


# Documentation ranges (RFC 5737) and well-known public resolvers are not identifiers.
_IP_OK = re.compile(r"^(?:192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)|^(?:8\.8\.8\.8|8\.8\.4\.4|1\.1\.1\.1|1\.0\.0\.1)$")


def _email_ok(match):
    return match.group(1).lower() in _ALLOW


def find_pii(text):
    """Return a list of (kind, snippet) for anything that looks like an identifier."""
    hits = []
    for kind, pat in PATTERNS.items():
        for m in pat.finditer(text):
            if kind == "email" and _email_ok(m):
                continue
            if kind == "public_ipv4" and _IP_OK.search(m.group(0)):
                continue
            hits.append((kind, m.group(0)[:60]))
    return hits


def scrub(text):
    """Replace identifiers with neutral placeholders (used before distillation)."""
    def email(m):
        return m.group(0) if _email_ok(m) else "user@example.com"

    text = PATTERNS["email"].sub(email, text)
    text = PATTERNS["api_key"].sub("<API_KEY>", text)
    text = PATTERNS["app_key"].sub("<APP_KEY>", text)
    text = PATTERNS["public_ipv4"].sub(lambda m: m.group(0) if _IP_OK.search(m.group(0)) else "203.0.113.10", text)
    text = PATTERNS["zendesk_ref"].sub("<TICKET_REF>", text)
    text = PATTERNS["org_id"].sub("org id <ORG_ID>", text)
    text = PATTERNS["aws_account"].sub("<ACCOUNT_ID>", text)
    text = PATTERNS["phone"].sub("<PHONE>", text)
    return text
