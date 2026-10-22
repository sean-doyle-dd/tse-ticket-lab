## Difficulty rubric

**Low**
- One root cause, usually a single misconfiguration or a well-documented limitation.
- The ticket clearly describes the symptom and includes most of the useful evidence (versions, the config snippet or the error message).
- Two to four hidden facts. Most come out on the first sensible question.
- The customer is cooperative and fairly technical, and has no wrong assumptions.
- A good TSE resolves it in one or two replies. Escalation: `not_needed`.

**Medium**
- One root cause, but the ticket describes it indirectly. The obvious first guess is plausible and wrong, or incomplete.
- The ticket is missing key evidence. The TSE has to ask for a flare, logs, a config file, a `status` output or exact timings.
- Four to seven hidden facts. At least two need a specific, well-aimed question.
- The customer has one mild misconception (for example, "it must be on your side, nothing changed") and one red herring is present.
- Takes two to four exchanges. Escalation: usually `not_needed`, sometimes `optional`.

**High**
- Two interacting causes, or one subtle cause wrapped in misleading context: a version-specific behaviour, an environment quirk, or a symptom that appears far from its cause.
- The ticket is vague or emotionally charged, there's business pressure, and the customer's description uses the wrong terminology.
- Six to ten hidden facts, including at least one that only comes out if the TSE asks exactly the right question or reads an attachment carefully.
- Two or more red herrings. The customer pushes back at least once (for example, "we already checked that") and holds one firm misconception until the TSE explains it with evidence.
- The TSE must decide whether to escalate. Escalation is `optional` or `required`, with a clear reason.
