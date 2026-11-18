# MemBuddy

**Your agents change. Your lessons should not disappear.**

Local-first shared preferences, instructions, mistakes/fixes and useful decisions.
Python CLI + MCP server + bundled skills; SQLite stores both notes and vectors.
The host agent writes lessons, so no additional generative AI or paid API is needed.
See [the specification](SPEC.md) for the agreed behavior and boundaries.

## Install

Python 3.11+ and Git are required. From this folder in PowerShell:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .
& .\.venv\Scripts\python.exe -m membuddy setup --download-model
```

The model download is public `sentence-transformers/all-MiniLM-L6-v2`, pinned to
revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Setup downloads weights only;
no conversations are uploaded. Normal operation requires no network/model download.
On a new machine, run setup with the same model before using a copied database.

Default config: `<user home>\MemBuddy\config.json`. Example:

```json
{
  "data_dir": "D:\\Private\\MemBuddy",
  "visibility": "silent",
  "budget_chars": 2000,
  "max_results": 5
}
```

Set `MEMBUDDY_CONFIG` or pass `--config <path>` **before** the subcommand.
Relative configured paths resolve against the config file's directory.
Optional `model_dir` overrides `<data_dir>\model`; optional `history_store` overrides
the Copilot CLI history database read by `scan-history`. Relocating data requires copying
a consistent backup and updating configuration; changing config is not migration.
Visibility may be `silent` or `brief`; it governs routine agent chatter, not errors.

### Explicit Copilot setup

After installation, choose the clients to configure. These commands **modify**
only the named client's MCP entry, skills and memory instructions; changed files
receive timestamped `.bak` copies. Existing unrelated MCP entries are preserved.

```powershell
# Copilot CLI: target is your HOME, not .copilot itself.
& .\.venv\Scripts\python.exe -m membuddy configure --client cli --target $env:USERPROFILE

# VS Code: target is a WORKSPACE, not .vscode itself. Repeat per workspace.
& .\.venv\Scripts\python.exe -m membuddy configure --client vscode --target 'C:\work\my-repo'
```

CLI setup installs personal skills and a bounded block in personal instructions.
VS Code setup installs workspace skills, instructions and MCP configuration.
Strict JSON is required for existing MCP config; JSONC is rejected rather than
rewritten destructively. Keep the environment installed: configs use its absolute
Python interpreter path. Restart/reload Copilot and approve/trust the local server.
Check that `memory` and `identify_project` tools and the five skills are visible.
Host discovery, policy and custom-slash support vary; setup is not proof of loading.

Other agents: register a stdio MCP server executing:

```text
<absolute Python executable> -m membuddy --config <absolute config path> serve
```

### One shared localhost server

Clients supporting Streamable HTTP MCP can share one server and one loaded
embedding model instead of launching a server for each connection. SQLite notes
and metrics stay in the same data directory. Stdio remains the default/fallback.

```powershell
# Explicitly update only the Copilot CLI MCP entry; back up changed config.
# Does not replace installed skills or instructions.
& .\.venv\Scripts\python.exe -m membuddy configure --client cli --target $env:USERPROFILE --transport http --mcp-only

# Start ONCE, independently of Copilot. Keep this terminal/process alive.
& .\.venv\Scripts\python.exe -m membuddy serve --transport http
```

Default endpoint: `http://127.0.0.1:8765/mcp`. Use the same `--port <number>`
(1024-65535) for configure and serve to change it. For VS Code workspace setup,
use `--client vscode --target <workspace>`; it writes the standard workspace
MCP file, not personal Copilot SDK/CLI config. Existing personal configuration
continues to be the source of truth when that is what the host reads.

The server binds only to IPv4 loopback, validates Host/Origin and ignores forwarded
proxy headers. It has no authentication: **any local process can use the server**
and access or modify its memory. Do not expose this endpoint through a proxy,
tunnel, container port mapping or LAN binding. Use stdio instead if sharing with
other local processes is unacceptable. Old token files and configuration backups
are not automatically removed.

The process is not an installed Windows Service or automatic updater.
Launch only one instance.
One listening port prevents a second live listener. A venv on Windows may show a
launcher and its Python child: that is one server, not two loaded models.
Stop the known server PID (or Ctrl+C in its terminal), then run the same serve
command after code updates. Existing HTTP clients may need to reconnect.
A client will report connection failure if the server is not running; it will not
silently create a stdio fallback.

#### Keeping it running: `ensure`

`ensure` is a one-shot check-and-repair; it always exits afterwards:

```powershell
& .\.venv\Scripts\python.exe -m membuddy ensure   # optional: --port N --grace 60
```

1. Sends an MCP `initialize`. Healthy: does nothing (`"status":"healthy"`).
2. Otherwise finds MemBuddy `serve --transport http` processes for that port. A
   server still loading its model gets `--grace` seconds; if it stays unhealthy its
   process tree is killed.
3. Starts a detached server (no window; output in `<data_dir>\http-server.log`),
   waits until healthy, returns `started`/`restarted`, then exits.

It never kills a non-MemBuddy process holding the port (it reports an error), and
a lock file (`<data_dir>\ensure.lock`, stale after 10 minutes) makes concurrent
runs return `busy`. It cannot start the server at the moment of an MCP call:
the HTTP client gets no hook. Schedule it instead (no admin rights needed):

```powershell
$py = (Resolve-Path .\.venv\Scripts\pythonw.exe).Path   # pythonw: no console window
$action = New-ScheduledTaskAction -Execute $py -Argument "-m membuddy --config `"$env:USERPROFILE\MemBuddy\config.json`" ensure"
$triggers = @(
  (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"),
  (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)))
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'MemBuddy Ensure' -Action $action -Trigger $triggers -Settings $settings -Principal $principal

# Remove: Unregister-ScheduledTask -TaskName 'MemBuddy Ensure' -Confirm:$false
```

Worst-case downtime is about the repetition interval plus startup. After a code
update, stop the server and run `ensure` (or wait for the next run).

To revert a named client to launching its own server:

```powershell
& .\.venv\Scripts\python.exe -m membuddy configure --client cli --target $env:USERPROFILE --transport stdio --mcp-only
```

This backs up and replaces only the named MemBuddy entry. Other MCP entries
are preserved. Reconnect the client afterward.

Copy the bundled `membuddy\assets` skill folders to that client's supported skill
location and include the short memory instructions in its supported instruction
file. No platform can be forced to obey solely by installing a skill.

## Chat workflow

- Normal tasks: the skill retrieves relevant preferences/lessons, records user
  corrections and verified self-corrections, and reports actual successful use.
- `/show-summary`: display unsaved proposed notes; no database write.
- `/add-to-mem`: summarize existing/current chat into notes, show a preview,
  and save only after approval. Repeated use replaces that chat's contribution,
  not other chats' shared knowledge.
- `/remove-memory forget all about …`: show matching notes, confirm, then exclude
  from recall. History/backups remain; this is **not secure erasure**.
- `/optimize-memory`: examine active + archived notes and propose merges. Confirmed
  merges preserve history and source links, remove redundant live vectors, compact.

The host agent performs summarization. The tool does not read arbitrary chats or
pretend it can see a compacted-away conversation. Stable source ids such as
`copilot-cli:<session-id>` link contributions. A manually generated id must be
retained for repeated imports of the same chat.

## CLI/API examples

CLI operations accept a JSON object, `@file.json`, or `-` for stdin. Stdin is useful
in PowerShell to avoid native-executable quoting differences:

```powershell
@'
{"source":"copilot-cli:example","note":{"kind":"mistake","text":"Failure: && chaining failed in Windows PowerShell 5.\nFix: use ; and if ($?) for dependent commands.\nVerified: corrected commands succeeded.","conditions":"Windows PowerShell 5 only","scope":"global","origin":"self","reusable":true}}
'@ | & .\.venv\Scripts\python.exe -m membuddy remember -

'{"query":"How should I chain PowerShell commands?"}' |
  & .\.venv\Scripts\python.exe -m membuddy recall -

& .\.venv\Scripts\python.exe -m membuddy list
& .\.venv\Scripts\python.exe -m membuddy backup
```

MCP has two tools: `identify_project(folder | remote)` and
`memory(action,payload)`. `identify_project` requires the **absolute workspace
folder** (or a Git remote URL) and rejects calls without one, because the MCP
server's own working directory is not the agent's project. CLI and MCP share this action vocabulary:

| Action | Payload |
|---|---|
| `remember` | `{"source":"platform:chat-id","note":{...}}` |
| `recall` | `{"query":"...", "scope":"global", "startup":false, "budget_chars":2000}` |
| `list` | `{"state":"active","limit":50,"offset":0}`; also `archived`, `retired` |
| `feedback` | `{"note_id":1,"event":"platform:chat:action-id","successful":true}` |
| `history` | `{"note_id":1}` |
| `backup` | `{}` |
| `preview` | `{"operation":"conversation","payload":{"source":"platform:chat-id","notes":[...]}}` |
| `preview` | `{"operation":"remove","payload":{"ids":[1,2]}}`; also `archive` |
| `preview` | `{"operation":"scope","payload":{"ids":[1],"scope":"global"}}` |
| `preview` | `{"operation":"optimize","payload":{"groups":[{"ids":[1,2],"note":{...}}]}}` |
| `commit` | `{"proposal_id":"token-from-preview","confirmed":true}` |
| `scan-history` | `{"limit":100}` (reads the `history_store` from config; takes no path) |
| `candidates` | `{"limit":10,"offset":0}` |
| `metrics` | `{"days":7}`; range 1-365 |
| `telemetry` | `{"event_id":"opaque-id","run_id":"opaque-run","kind":"task_started","data":{...}}`; schema below |

A note requires `kind` and `text`. Optional fields: `conditions`, `scope`,
`origin`, `reusable`, `related`. Text: 1-6 lines, <=1800 chars; conditions:
one line, <=300 chars. Kinds: `preference`, `instruction`, `mistake`, `context`.
Origins: `user`, `correction`, `self`. Scope: `global`, normalized `repo:...`,
or absolute `folder:...`; use `scope` CLI command / `identify_project` to obtain it.

### Local metrics and an agent-reported pilot

CLI/MCP memory actions automatically record operation counts, failures, duration,
recall note IDs/context size, and new feedback assessments in `<data_dir>\metrics.db`.
It is a separate SQLite database with WAL and a 15-second lock timeout. Existing
memory tables, success counters, confirmation rules and operation results are unchanged.
Direct Python `Store` calls are not instrumented; use `api.execute`, CLI or MCP.
Metrics/telemetry commands do not count themselves as memory operations.

```powershell
# On-demand report; days defaults to 7.
& .\.venv\Scripts\python.exe -m membuddy metrics
'{"days":30}' | & .\.venv\Scripts\python.exe -m membuddy metrics -
```

The JSON report includes per-action sample counts, median/p95 duration, failures,
mean recalled characters, recalls and confirmed-help feedback grouped by note,
explicit task outcomes/durations, unresolved errors and repeated error signatures.
When an error resolution links a lesson, later matching errors in the same project identify whether
that lesson was recalled beforehand. This is an observation, not proof of causation.
Missing measurements are `null` or absent, not inferred successes.

The pilot uses **explicit agent/host reporting**, not SDK hooks, a background
watcher or access to VS Code Chat internals. A participating agent reports:

1. `task_started`, once per task, with a retained unique run/event ID.
2. Its normal memory operations, linking them to that run.
3. Errors and verified resolutions where applicable.
4. `task_finished` only after outcome verification, or with failed/cancelled status.

MCP: pass optional `run_id` alongside `action` and `payload` to `memory`.
CLI: add `--run-id <id>` to a memory action. Unlinked operations still contribute
to operation totals but cannot establish task-level recall or lesson/error links.

```powershell
'{"event_id":"pilot-1:start","run_id":"pilot-1","kind":"task_started","data":{"host":"vscode","agent":"primary","project":"project-a","category":"bugfix","memory_mode":"enabled"}}' |
  & .\.venv\Scripts\python.exe -m membuddy telemetry -

'{"query":"Relevant verified fixes","scope":"global","startup":true}' |
  & .\.venv\Scripts\python.exe -m membuddy recall - --run-id pilot-1

# After verification; use failed/cancelled instead when appropriate.
'{"event_id":"pilot-1:end","run_id":"pilot-1","kind":"task_finished","data":{"outcome":"success"}}' |
  & .\.venv\Scripts\python.exe -m membuddy telemetry -
```

Telemetry payload fields:

| Kind | Required data | Optional data |
|---|---|---|
| `task_started` | `host`, `agent`, `project`, `category`, `memory_mode` (`enabled`/`disabled`) | `parent_run_id`, `model`, `host_version` |
| `task_finished` | `outcome` (`success`/`failed`/`cancelled`) | None |
| `error_observed` | `category` (`implementation`/`tool`/`infrastructure`/`correction`/`unknown`), `fingerprint` | `note_id` |
| `error_resolved` | `error_event_id` referencing an error in the same run | `note_id`, only when a verified reusable lesson exists |

Identifiers/labels accept 1-120 letters, digits, underscores, dots, colons or
hyphens. Use opaque IDs/labels, never secrets. `project` is an opaque project ID,
not a folder or remote URL. Note IDs must exist. `fingerprint` is a lowercase
64-character SHA-256 digest of a **sanitized, versioned error signature**: normalize
variable IDs/timestamps/paths, remove secrets first, and include relevant tool/OS
conditions. Hashing alone is not anonymization. Matching text is not proof of the
same root cause. The producer owns normalization; MemBuddy does not ingest raw errors.

Identical event replays count once; conflicting reuse of an ID is rejected.
Finished tasks reject new events; missing finishes remain incomplete. Child tasks
name their parent; never add overlapping parent/child elapsed times. Marking an
error resolved is an explicit producer assertion after verification, not automatic
validation by MemBuddy. Only normal successful-use `feedback` increases utility;
recalls, error links, and resolutions do not.

Report boundaries:
- Runs are selected by start time; operations/errors by observation time. There is
  no automatic speedup percentage, prevented-error count or comparison of unlike tasks.
- Durations measure API action execution, excluding store/model initialization,
  transport and telemetry writes. Task elapsed time includes idle/user waiting.
- Helpful feedback is agent-reported. Missing feedback does not mean no help.
- A telemetry write failure prints a warning to stderr without masking a memory
  result/error; the operation may already be committed and must not be replayed.
  Reports cannot detect every dropped event or uninstrumented host.
- No historical timings are backfilled. Existing success counters are not new
  feedback events. No telemetry leaves this machine.
- Telemetry is append-only in this increment. Reports scan retained events; there
  is no automatic retention cleanup, scheduled report or dashboard yet.
- Memory backups do **not** include the separate metrics database. For portability,
  use SQLite's backup API for `metrics.db`; do not copy its live main file alone.

Bundled skills describe this pilot. Existing installed skills are not automatically
overwritten: adopt the updated instructions deliberately, and reload a running MCP
server after updating the package. Configuration commands remain explicit.

`needs_review` means similar existing notes need host-agent judgment. Similarity
does not imply agreement. Acknowledge independent notes with
`related:[{"id":1,"decision":"distinct"}]`. For a replacement, ask about genuine
contradictions, then generate a conversation preview with
`related:[{"id":1,"decision":"replace"}]`; it cannot bypass confirmation via
`remember`.

Preview tokens are single-use and bound to the current memory generation.
Concurrent changes require a fresh preview and fresh approval. Tool confirmation
guards cannot prove an external agent obtained actual human consent.
An empty extraction is rejected; it is not permission to erase a conversation.

### History fallback

`scan-history` is an explicit, read-only Copilot CLI adapter. It reads only the file
named by the `history_store` config key (default `~/.copilot/session-store.db`);
callers cannot supply a path, so an agent cannot point it at arbitrary databases.
It checks recent
stored turns for correction language, redacts bounded evidence and stages
**candidates**, not active memories. The agent reviews candidates and uses the
confirmed import flow. It does not import whole transcripts, install a background
watcher, or claim support for VS Code's internal chat storage. It can miss older
turns outside its bounded recent scan.

### Backups and moving machines

`backup` uses SQLite's consistent backup API, including notes, vectors, provenance
and history, and writes a `memory-*.db` file that is **never pruned**. Separately,
MemBuddy takes safety copies (`auto-*.db`) before destructive operations; it keeps
the newest 10 plus the newest one per day for the last 14 days and deletes the rest.
Take a manual backup for anything you want to keep. Close writers before copying the live database, or copy a backup
instead. Do not copy just an open database's main file: committed data may still
be in WAL. On the destination, install MemBuddy/model, place the snapshot at
`<configured data_dir>\memory.db`, and point config to that folder. Do not overwrite
an open database or leave old WAL/SHM sidecars beside a restored database.
Client configs contain machine-specific Python paths and must be configured anew.
Multiple-machine concurrent editing and cloud-sync conflict resolution are not
implemented.

## Validation

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v

# Real model + actual CLI subprocess and MCP stdio tests; needs the cached pinned model.
$env:MEMBUDDY_REAL_MODEL = '1'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests create isolated temporary data/client folders, not personal memory/config.
Semantic tests use the real embedding model; unit tests use deterministic vectors
to exercise transactions and policy without provisioning model weights.

## Limits

- No viewer yet. Manage via CLI/MCP and chat.
- The skill guides agents; it cannot guarantee retrieval, obedience or zero mistakes.
- Secret redaction is best-effort, not a substitute for keeping secrets out of chat.
  Local databases/backups are not encrypted; use protected storage/access controls.
- Exact cosine search scans stored vectors. This avoids separate-index drift;
  benchmark larger corpora before adding ANN indexing.
- Output uses a serialized-character budget, not an exact token count. A large
  note may not fit a small budget and is omitted rather than truncated deceptively.
- Archiving reduces search noise, not disk usage. Kept history/backups can outweigh
  compaction savings. No guessed age-based expiry or secure-erasure policy.
