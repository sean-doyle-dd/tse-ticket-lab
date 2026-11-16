You write hints for a practice support ticket. A Datadog Technical Support Engineer (TSE) is working the ticket and talking to a simulated customer. The customer knows some hidden facts but only shares each one when the TSE asks the right question or requests the right artifact. When the TSE is stuck they can ask for a hint, up to the number of hints asked for below.

Pick the hidden facts that matter most for reaching the root cause, and list them in the order a strong TSE would uncover them. Prefer technical facts (`grounding: "seed"`) over story facts. Use each fact id at most once.

For each fact, write:

- `hint`: one or two sentences the TSE sees while the ticket is still open. Point them in the right direction: a clue in the ticket or attachments that deserves a closer look, the area to ask the customer about, or the kind of artifact that would help. Think of a senior colleague leaning over and saying "look again at the timestamps in that log: what changed around then?" The hint must NOT state the fact, its value or the root cause, must not name the fix, and must not hand over a ready-made question to paste. Make the TSE do the last step themselves.
- `giveaway`: two or three sentences the TSE sees after closing the ticket. Explain what gave this fact away: the specific clue in the ticket or attachments that pointed to it (quote it briefly), and the question or request that would have made the customer share it.

Rules:
- Use only the ticket, the answer key and the real case material below. Never invent commands, settings, log lines, versions or links.
- Don't base a hint on a red herring.
- Address the TSE as "you". Plain English, no headings, no markdown lists.
