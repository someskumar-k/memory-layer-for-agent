---
name: show-summary
description: "Preview the current chat as concise proposed memory notes without writing memory. Use for /show-summary."
disable-model-invocation: true
---

# Show proposed memory

Read the current available conversation. Explain if history is incomplete or
compacted. Generate a list of preferences/instructions, verified mistakes with
fixes, and useful decisions/context. Each note has 1-6 meaningful lines and its
applicable conditions. Omit routine chatter, raw logs, secrets and speculation.

Display this list directly in the chat. Label it **unsaved preview**. Do not call
remember, preview, commit or any other mutating memory operation.
If existing confirmed notes are requested, retrieve and label them separately.
Suggest `/add-to-mem` to save a user-approved list.
