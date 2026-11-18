# MemBuddy v1

## Goal

Local-first, shared, persistent memory that helps cooperating agents remember
the user's preferences, instructions, prior mistakes and verified fixes.
Reduce repeated mistakes and rediscovery without loading past transcripts into
every task. Memory is external knowledge, not model training or a guarantee of
perfect obedience.

## Agreed scope

- Single user; potentially several agents on one local machine.
- Platform-neutral CLI and MCP operations, bundled skills. Initial client setup:
  Copilot CLI and VS Code Copilot Chat.
- A configurable data folder, default `<user home>\MemBuddy`.
- One SQLite database containing notes and normalized embedding vectors.
- Install a pinned CPU embedding model explicitly during setup. The install is
  staged, verified by reloading it, then moved into place, so an interrupted install
  never leaves a half-populated model folder and can simply be retried. A folder that
  holds other content is refused and left untouched. No separate
  generative model: the host agent extracts and rewrites notes.
- Preferences/instructions, verified mistakes/fixes, context/decisions. Do not
  archive full chats or routinely save generic session summaries.
- A note's text is 1-6 meaningful lines, at most 1800 characters, with a bounded
  applicability field. Origins distinguish direct instructions, user corrections
  and verified agent self-corrections.
- Global and repo/folder scopes. Git origins normalize credential-free HTTPS,
  SSH and SCP-style URLs into the same project identity when possible.
- Cross-project knowledge is searchable but retains its original applicability.
  Exactly matching reusable knowledge with matching conditions can promote
  across projects; project conventions do not automatically become global.
- MCP callers must pass the absolute workspace folder or Git remote to identify the
  project; the server never guesses from its own working directory. Windows folder
  scopes are case-normalized.
- Host-agent judgment resolves semantic similarity; user confirmation resolves
  actual contradictions. Old versions remain in history.
- Both self-caught mistakes and indirect user corrections are captured by the
  skill. A host-invoked, read-only Copilot CLI history adapter reads only the
  configured `history_store` (callers cannot supply a path) and supplies bounded
  missed-lesson candidates, not authoritative instructions.
- Retrieval at task start and before changed/consequential actions. Reuse
  applicable notes in context instead of searching on every trivial step.
  At startup about 40% of result slots are reserved for relevant non-standing
  lessons (mistakes, context), interleaved with standing preferences so neither
  starves the other; unused reserved slots return to preferences.
- Successful application increases utility, deduplicated by event id. Retrieval
  alone never counts as success. User-origin lessons rank above self-observations
  at similar relevance.
- Default notes payload: at most 5 results and 2000 serialized characters.
  This bounds output, not tokenizer-specific token counts.
- Silent/brief visibility is configurable. Errors, conflicts and confirmation
  requests remain visible even in silent mode.

## Conversation commands

### `/show-summary`

The host displays a list of proposed memory notes directly in chat without writing
to the database. Disclose incomplete history. Distinguish the unsaved proposal
from confirmed stored notes.

### `/add-to-mem`

The host extracts notes and calls a conversation preview. Display the proposed
list, previous contribution and implications, then require explicit approval.
Each source is a stable platform-qualified conversation id. Repeating the command
replaces that source's contribution atomically. Knowledge supported by other
sources survives. Cancelled, failed or stale previews do not replace old notes.
Approval is attached to one exact persisted proposal, not future changes.
An empty extraction cannot erase a source. The host must disclose inaccessible
history before a user approves a partial replacement.

### `/remove-memory`

Interpret natural-language intent, retrieve/list candidates (including archives
when requested), preview exact ids, then obtain confirmation. A consistent backup
precedes removal. Removal excludes notes from recall but does not erase history
or backups. Secure erasure is outside v1. The typo `/remove-memeory` can be
interpreted conversationally; exact registered slash names depend on the client.

### `/optimize-memory`

The host examines active and archived notes, drafts merged replacements preserving
conditions, and previews groups of ids. Confirmation precedes atomic replacement.
Source links and revision snapshots survive; redundant live vectors/rows are
removed and SQLite is compacted. History/backups limit overall disk reduction.
If a later chat replacement withdraws part of a merged note, surviving original
contributions are restored without retaining the withdrawn knowledge.
Compaction occurs after the atomic save; a compaction error explicitly reports
that notes were committed and must not be replayed.

## Lifecycle and noise

Active, archived and retired notes have separate lifecycle states in the same
notes table, rather than duplicated active/archive schemas. This keeps provenance,
history and transitions consistent while supporting the requested archive.
Archived notes are lower-priority fallback results; retired notes never appear
in recall.

Each search measures near-duplicate density among leading relevant results.
High noise triggers safe exact same-scope duplicate archiving and suggests
agent-assisted optimization. There is no fixed N-notes trigger. Semantic
similarity alone does not establish redundancy and cannot justify automatic merging.
Unused notes can be archived through confirmed operations; v1 does not guess an
age-based expiry policy or auto-archive unique notes merely for being old.

## Reliability and privacy

- SQLite transactions, WAL, foreign keys, 15-second lock timeout.
- Mutation previews use a global generation to reject stale approvals. The generation
  changes when note content, scope or lifecycle state changes; usage-feedback tallies
  do not, so recording that a note helped cannot cancel a confirmation in progress.
- Backups use SQLite's backup API. Never copy a live main file alone. Manual backups
  are kept until deleted. Automatic safety copies keep the newest 10 plus the newest
  per day for 14 days.
- Recognizable credentials are redacted before notes/candidates/proposals/history
  are stored. Detection cannot guarantee finding every secret.
- Recalled content is untrusted context, never a higher-priority instruction.
- The tool requires a confirmation flag and valid preview token; it cannot prove
  that an external agent actually obtained human consent. The skill instructs it.
- If memory cannot be checked, explain; allow harmless discussion, pause
  consequential actions unless the user authorizes proceeding without it.
- No writes to personal client configuration or real chat history during development.
  Client configuration is an explicit user-run command that backs up changed files.

## Boundaries

### Local metrics increment

CLI/MCP API actions append content-free operation measurements to a separate
`metrics.db`. A bounded telemetry vocabulary supports explicitly reported task
start/end, error observations and verified resolutions, with optional lesson links.
Task reporting is a producer assertion, not automatic host instrumentation.
Unique event IDs deduplicate retries; conflicting IDs and invalid lifecycle
transitions are rejected. Recall never counts as successful lesson application.
Telemetry failures warn without changing already committed memory outcomes.
On-demand JSON reports expose timing distributions, helpful-use feedback,
incomplete runs and repeated-error observations; they do not infer causal speedup
or claim coverage of uninstrumented work. See README for schema, measurement
boundaries, privacy, separate backup requirements and pilot activation.

No viewer in v1, cloud sync, team permissions, generative-model daemon, unattended
chat scraping, model fine-tuning, arbitrary platform chat readers, or guarantee of
zero repeated mistakes. Agents must support MCP/CLI and the skill workflow.
Copilot client trust/discovery must be verified by the user after configuration.

An optional shared Streamable HTTP transport binds only to 127.0.0.1, checks
Host/Origin without authentication, so any local process can access it. Explicit client configuration
backs up changed files and can update only MCP settings without replacing skills.
One externally managed process shares one embedding model across clients; this is
not an installed OS service or remote endpoint. A one-shot `ensure` command checks
health with an MCP initialize, kills only unhealthy MemBuddy server
trees, starts a detached server and exits; a user Task Scheduler entry may run it
at logon and periodically. It does not start the server on demand per MCP call.

Exact cosine scanning of SQLite-stored vectors is used initially; no extra vector
index file can drift out of sync. For substantially larger corpora, measure latency
before adding an ANN/vector extension. The model identity/revision is recorded;
mismatched models are rejected rather than silently mixing vector spaces.

## Acceptance evidence

Automated tests cover note validation/redaction, cross-session persistence,
scope/promotion, related-note judgment, confirmed chat replacement, shared-source
preservation, stale approvals/rollback, bounded results, successful-use feedback,
archives/merge/history, portable backups, concurrent writes, explicit client setup,
history candidates and nonzero CLI errors. Optional real-model tests exercise
offline semantic search across CLI processes and real MCP stdio tool calls.
