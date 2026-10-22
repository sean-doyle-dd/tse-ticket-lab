You check one customer reply in a support-training simulation.

1. `revealed_fact_ids`: list the ids of the hidden facts that this reply discloses, either stated outright or clearly shown in an attachment. List only facts that are actually present in the reply, not facts that are merely related to it.
2. `ungrounded_details`: list every technical detail in the reply (output lines, error messages, config values, versions, product behaviour) that is not supported by the hidden facts or the case material below. Quote each one briefly. Ignore fictional names, hosts, timestamps and ordinary conversational content. If everything is supported, return an empty list.
