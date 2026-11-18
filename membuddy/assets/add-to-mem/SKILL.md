---
name: add-to-mem
description: "Add an existing/current chat to MemBuddy after showing proposed notes and receiving confirmation; reruns replace that chat's contribution. Use for /add-to-mem."
disable-model-invocation: true
---

# Add this conversation to memory

1. Load `membuddy`. Obtain the stable platform-qualified conversation source id
   and project scope. Do not reuse another conversation's id.
2. Read available history; explicitly disclose inaccessible/compacted portions.
   If the current history is incomplete, do not replace the old contribution
   until the user supplies a transcript or knowingly approves the partial replacement.
3. Extract a concise list of notes, not a generic recap: preferences, instructions,
   verified mistakes/fixes, decisions/context, with their conditions. No raw chats
   or credentials. Each note has at most 6 lines.
4. Call `memory` action `preview` with:
   `{"operation":"conversation","payload":{"source":"PLATFORM:ID","notes":[...]}}`.
5. Resolve `needs_review` candidates: distinct vs refinement/opposition. Ask about
   contradictions. Regenerate preview with explicit related decisions.
6. Show the exact proposed list and existing contribution (`before`). Explain what
   will change or disappear. This replaces only this chat's contribution; shared
   knowledge supported by other conversations survives.
7. Ask for explicit confirmation. No `commit` before approval. After approval,
   call `commit` with the exact proposal id and `confirmed:true`.
8. If rejected, do not commit. If the preview is stale, show a fresh one and ask
   again. Report errors; never claim an unsuccessful operation saved anything.

An empty extraction is not permission to erase a conversation. Use a confirmed
removal workflow for intentional removal.
