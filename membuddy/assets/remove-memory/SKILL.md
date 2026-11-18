---
name: remove-memory
description: "Forget or remove memories by natural-language intent, preview matches and ask before removing. Use for /remove-memory or the alias phrase /remove-memeory."
disable-model-invocation: true
---

# Remove memory

Search using the user's intent, then use paginated `list` for a broad request
such as "all preferences". Include archived notes when appropriate. Do not assume
the top semantic matches are the complete requested set.

Show the matching ids and full notes. Call `preview` with operation `remove` and
payload `{"ids":[...]}`. Explain: these notes stop appearing in recall, but history
and prior backups remain; this is **not permanent secure erasure**.

Ask for confirmation of this exact set. Only then call `commit` with proposal id
and `confirmed:true`. If stale, preview again and get fresh approval. Rejection
changes nothing. Errors must be surfaced. The misspelled `/remove-memeory` is a
natural-language alias; clients need not register that spelling as a slash command.
