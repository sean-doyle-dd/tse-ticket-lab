"""Specs and difficulty levels offered in the UI. Adding a spec = add it here, add
`corpus/<spec>/` seeds, and (optionally) `sandbox_templates/<spec>/` templates."""

SPECS = {
    "agent": {
        "label": "Agent",
        "blurb": "Install & config, DogStatsD, integrations and Autodiscovery, proxies, log collection.",
    },
    "apm": {
        "label": "APM",
        "blurb": "Tracer setup, Agent connectivity, sampling & ingestion, propagation, service tagging.",
    },
    "synthetics": {
        "label": "Synthetics",
        "blurb": "API & browser tests, private locations, network reachability, test configuration.",
    },
    "web-platform": {
        "label": "Web Platform",
        "blurb": "Dashboards, monitors & notifications, RBAC, org & account management, UI behaviour.",
    },
}

DIFFICULTIES = {
    "low": {
        "label": "Low",
        "blurb": "One clear misconfiguration. Most of what you need is in the ticket.",
    },
    "medium": {
        "label": "Medium",
        "blurb": "You'll need to gather information first. Expect logs or a flare, and a customer with a wrong assumption.",
    },
    "high": {
        "label": "High",
        "blurb": "Several interacting causes and red herrings. The customer pushes back, and you'll need to judge whether to escalate.",
    },
}
