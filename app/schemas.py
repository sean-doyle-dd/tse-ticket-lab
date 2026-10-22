"""Pydantic models for everything that crosses the Claude boundary, plus corpus seeds.

Structured outputs need strict JSON schemas, meaning every property is required and there are no
extra keys. So optional data is modelled as empty strings or empty lists, not None, and
no field carries constraints (min/max) the schema dialect can't express. `strict_schema`
in llm.py does the rest of the conversion.
"""
from typing import List, Literal

from pydantic import BaseModel, ConfigDict

Difficulty = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- corpus seeds

class QAPair(_Strict):
    tse_question: str
    customer_answer: str


class CustomerStyle(_Strict):
    tone: str
    technical_level: Literal["low", "medium", "high"]
    urgency: Literal["low", "normal", "high", "urgent"]


class Provenance(_Strict):
    """Where a seed came from. Never holds ticket ids (those stay in the local ledger)."""
    tickets: int = 0          # how many real tickets the pattern was distilled from
    csat: Literal["good", "offered", "unoffered", "bad", ""] = ""
    complexity: Literal["low", "medium", "high", ""] = ""   # the ticket's real TICKET_COMPLEXITY
    solved_month: str = ""    # YYYY-MM


class TseApproach(_Strict):
    """How the real TSE actually worked the ticket, distilled from their replies."""
    summary: str = ""
    steps: List[str] = []
    key_questions: List[str] = []
    communication: List[str] = []        # what made their replies land well
    customer_feedback: str = ""          # paraphrase of the CSAT comment, if any


class Seed(_Strict):
    """An anonymized pattern distilled from a real solved ticket. Contains no identifiers.

    `source="distilled"` seeds come from real tickets via tools/refresh_corpus.py.
    `source="bootstrap"` seeds were hand-written to get started and are off by default
    (CORPUS_ALLOW_BOOTSTRAP)."""

    id: str
    spec: str
    source: Literal["bootstrap", "distilled"]
    recency: str  # ISO date of the newest ticket the pattern was seen in
    topic: str
    product_area: str
    environment: List[str]
    symptom_pattern: str
    root_cause: str
    resolution_path: List[str]
    info_needed: List[str]
    qa_pairs: List[QAPair]
    customer_style: CustomerStyle
    difficulty_range: List[Difficulty]
    sandbox_hint: str  # sandbox template id that can reproduce it, or ""
    # Technical strings that appear verbatim in the scrubbed ticket thread: error messages,
    # log lines, config keys, commands, versions. The generator may only use these.
    evidence: List[str] = []
    doc_links: List[str] = []     # docs.datadoghq.com links the TSE actually sent
    tse_approach: TseApproach = TseApproach()
    provenance: Provenance = Provenance()


# ------------------------------------------------------------------- generated scenario

class Attachment(_Strict):
    filename: str
    kind: Literal["log", "config", "flare_excerpt", "command_output", "screenshot_description", "other"]
    content: str


class Customer(_Strict):
    name: str
    title: str
    company: str
    company_profile: str
    technical_level: Literal["low", "medium", "high"]
    tone: str
    timezone: str


class PublicTicket(_Strict):
    subject: str
    body: str
    priority: Literal["low", "normal", "high", "urgent"]
    product_area: str
    environment: List[str]
    attachments: List[Attachment]
    customer: Customer


class Fact(_Strict):
    id: str
    statement: str
    reveal_when: str
    category: Literal["environment", "configuration", "timeline", "symptom", "evidence", "constraint", "business_impact"]
    # "seed": technical, must be backed by the source ticket (source says which part).
    # "story": fictional colour only (people, company, timeline, business impact).
    grounding: Literal["seed", "story"]
    source: str


class Truth(_Strict):
    summary: str
    root_cause: str
    resolution_steps: List[str]
    acceptable_alternatives: List[str]
    facts: List[Fact]
    red_herrings: List[str]
    customer_misconceptions: List[str]
    escalation: Literal["not_needed", "optional", "required"]
    escalation_reason: str
    learning_objectives: List[str]
    doc_links: List[str]


class SandboxParam(_Strict):
    name: str
    value: str


class SandboxSpec(_Strict):
    needed: bool
    template_id: str
    params: List[SandboxParam]
    repro_steps: List[str]
    rationale: str


class GeneratedScenario(_Strict):
    ticket: PublicTicket
    truth: Truth
    sandbox: SandboxSpec
    difficulty_rationale: str


# ----------------------------------------------------------------------- chat + grading

class DisclosureCheck(_Strict):
    revealed_fact_ids: List[str]
    # Technical details in the reply that the answer key and source ticket don't support.
    ungrounded_details: List[str]


class UnsupportedClaim(_Strict):
    where: str    # e.g. "ticket.body", "attachment agent-status.txt", "truth.facts f3"
    claim: str
    reason: str


class GroundingCheck(_Strict):
    unsupported: List[UnsupportedClaim]


class CriterionScore(_Strict):
    score: int  # 1-5
    justification: str


class GradeResult(_Strict):
    diagnosis: CriterionScore
    information_gathering: CriterionScore
    communication: CriterionScore
    resolution: CriterionScore
    resolved: bool
    escalation_judgement: str
    overall_summary: str
    strengths: List[str]
    improvements: List[str]
    missed_facts: List[str]
    approach_comparison: str
    model_first_reply: str
