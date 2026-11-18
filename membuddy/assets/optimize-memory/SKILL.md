---
name: optimize-memory
description: "Manually optimize MemBuddy active and archived notes, merge redundant knowledge with confirmation, preserve history and compact the database. Use for /optimize-memory."
disable-model-invocation: true
---

# Optimize memory with the host agent

1. Page through `list` for both active and archived notes.
2. Group only genuinely equivalent or complementary knowledge. Preserve conditions
   and project boundaries. Ask the user about contradictions; never combine opposing
   instructions into an ambiguous rule.
3. Draft merged notes (at most 6 text lines each).
4. `preview` operation `optimize`, payload
   `{"groups":[{"ids":[1,2],"note":{"kind":"mistake","text":"...","conditions":"...","scope":"global","origin":"correction","reusable":true}}]}`.
5. Show the groups and merged replacements, then obtain confirmation.
6. `commit` the exact proposal id with `confirmed:true`. Stale previews require
   fresh approval. Commit backs up first, preserves history and source links,
   removes redundant live rows/vectors, then compacts the database.
7. Explain that retained history/backups may dominate disk space; optimization
   reduces search noise but cannot promise a smaller overall directory every time.
