# Meme Semantic Search

> **Status: exploration phase.** Nothing here is settled — schema, prompt, model
> choices, RRF constants, and even the core "describe-then-embed" approach are all
> subject to change once we see how they behave on real data. Treat decisions
> below as the current best guess, not fixed requirements. If something looks
> wrong or a better alternative appears mid-task, surface it instead of silently
> working around it. Prefer small, throwaway-friendly code over premature
> hardening; we will rewrite freely.

Local semantic search over a personal meme collection. The user types a description
("mem z poszerzaniem paska"), a usage context ("reakcja na coś głupiego, co powiedział kolega"),
or loose keywords ("harry potter, little shits") and gets matching meme files back.

## Stage → file mapping

One module per stage at the repo root, named after the stage:

- `scan.py` — Stage 1 scan & dedup
- `describe.py` — Stage 2 describe (VLM)
- `embed.py` — Stage 3 embed (bge-m3)
- `search.py` — Stage 4 search (hybrid vec + FTS, RRF fusion); importable as `search()`
- `gui.py` — Stage 5 Dear PyGui GUI wrapping stage 4 (extra dep: `dearpygui`)
- `common.py` — shared plumbing: `load_config`, `connect`, `request_embedding`
- `memes.py` — aggregate CLI: `uv run memes.py <stage> [flags]`, plus
  `memes.py index` which runs scan→describe→embed in one process

Every stage module exposes the same trio, which is what makes both entry points
work off one implementation:

- `add_arguments(parser)` — registers the stage's flags (including `--config`)
- `run(args, cfg)` — does the work against a parsed namespace + loaded config
- `main(argv=None)` — standalone entry point; `uv run describe.py …` still works

`memes.py` hangs each module's `add_arguments`/`run` off a subparser; `gui` is
imported lazily there so headless stages never pay for `dearpygui`.

`schema.sql` is executed by `common.connect(..., init_schema=True)`, which the
scan stage and `index` pass (idempotent `CREATE ... IF NOT EXISTS`); every other
stage expects the schema to already exist. `index` calls the stage functions
directly over a single shared connection and takes only the flags that make
sense for a whole run (`--config`, `--limit`, `--retry-failed`) — maintenance
flags like `--regroup-only` stay on the individual stages.

Two more data files at the root, easy to confuse: `schema.sql` is the SQLite
DDL above, while `description_schema.json` is the JSON Schema for the VLM's
output, loaded by `describe.py` and sent to the model on both backends.

## Core architectural principle

**Do not search images directly (no CLIP).** Instead: a VLM generates a structured
text description of each meme once, at indexing time, and all search happens over
those descriptions. Reasons:

- Memes are text-centric; queries are in Polish while meme text is often English.
- Queries like "reaction to a dumb take" describe *usage context*, not visual
  content. That context must be inferred at indexing time and materialized in the
  description — embedding models only compare texts, they don't reason at query time.

## Hard constraints

- Everything runs locally. Ollama (ROCm) is already installed and working; use it
  for both the VLM and embeddings via its OpenAI-compatible endpoint
  (`http://localhost:11434/v1`).
- The VLM backend must be swappable via config only (base_url + model + api_key):
  the same describe script must be able to point at `api.anthropic.com` for a
  quality comparison run. No provider-specific code paths beyond config.
- Queries are Polish and English; meme text is Polish and English. Use a
  multilingual embedding model: `bge-m3`.
- Python, managed with `uv`. Dependencies: `sqlite-vec`, `imagehash`, `Pillow`,
  plus `dearpygui` for the GUI only. Nothing else without asking. No SDK for the LLM APIs — talk to the
  Ollama/Anthropic OpenAI-compatible endpoints with stdlib `urllib`.

## Data model

Single SQLite database. SQLite is the job queue, the metadata store, and the
search index all at once. Canonical DDL lives in `schema.sql`; the summary below
tracks intent, not the exact CREATE statements.

```sql
CREATE TABLE memes (
    hash                    TEXT PRIMARY KEY,           -- sha256 of file contents
    path                    TEXT NOT NULL,              -- primary physical copy on disk
    phash                   TEXT NOT NULL,              -- perceptual hash (dedup)
    perceptual_duplicate_of TEXT REFERENCES memes(hash) -- phash group representative; NULL = representative
);

-- One row per describe attempt. A single meme can have many rows: different
-- (model, prompt_version, effort_level) combos live side by side so we can A/B
-- prompts and models without throwing away prior work.
CREATE TABLE descriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hash             TEXT NOT NULL REFERENCES memes(hash),
    description_json TEXT,                              -- raw VLM output; NULL when failed
    prompt_version   TEXT NOT NULL,                     -- matches prompts/<name>.txt
    model            TEXT NOT NULL,                     -- VLM model that produced this row
    effort_level     TEXT,                              -- reasoning_effort passed to the API; NULL = not sent
    status           TEXT NOT NULL                      -- 'described' | 'failed_describe'
        CHECK (status IN ('described', 'failed_describe')),
    error            TEXT,                              -- error message when failed
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Uniqueness on the full config tuple; NULL effort collapses via IFNULL so
-- re-runs with the same (model, prompt, no-effort) are still deduplicated.
CREATE UNIQUE INDEX descriptions_config_idx
    ON descriptions(hash, model, prompt_version, IFNULL(effort_level, ''));

CREATE VIRTUAL TABLE memes_fts USING fts5(
    hash UNINDEXED, transcription, keywords, template,
    tokenize = "unicode61 remove_diacritics 2"          -- Polish diacritics fold to ASCII
);
CREATE VIRTUAL TABLE memes_vec USING vec0(hash TEXT PRIMARY KEY, embedding float[1024]);
```

Status lives on `descriptions`, not on `memes`. A meme's pipeline position is
derived, not stored:

- **scanned** = row in `memes`, no row in `descriptions` yet (for the active
  config).
- **described** = at least one `descriptions` row with `status='described'`.
- **embedded** = row exists in `memes_vec` (and `memes_fts`).
- **failed_describe** = `descriptions` row with `status='failed_describe'` for
  the active config; `--retry-failed` re-picks these.

There is no `failed_embed` state yet — embed errors are transient (Ollama being
Ollama) and re-running `embed` re-picks any `described` row missing a
`memes_vec` entry.

`perceptual_duplicate_of` is orthogonal to describe/embed progress:

- It's a similarity annotation recomputed from stored `phash` values, independent
  of any describe/embed work.
- Every pipeline query and every search query filters
  `perceptual_duplicate_of IS NULL`. Non-representatives keep their
  `descriptions` rows (invested work is preserved) but don't advance or appear
  in results, and re-enter automatically if they flip back to representative.
- GUI duplicate browsing is an exception: it reads all members of a group,
  including non-representatives, regardless of describe/embed progress.

Design intent behind this schema — preserve these properties:

- File hash as primary key on `memes` → incremental indexing for free (re-runs
  skip known files).
- Raw VLM JSON stored verbatim → re-embedding never requires re-describing
  (describing costs minutes/money; embedding costs seconds).
- `descriptions` as a separate table with `(hash, model, prompt_version,
  effort_level)` uniqueness → prompt tweaks, model swaps, and effort-level
  experiments accumulate side by side. Re-runs of `describe` with the same
  config are no-ops; changing any part of the config produces a new row without
  clobbering the old one. Cross-config comparison is a simple SQL query.
- `prompt_version` as TEXT matching `prompts/<name>.txt` → prompts are versioned
  as files, not integers; the DB references the same names a human sees on disk.
- `perceptual_duplicate_of` orthogonal to describe/embed → the phash threshold
  can be tuned freely at any time; regrouping just rewrites this column across
  every row (cheap, uses stored `phash`). Rows that flip from representative to
  duplicate silently drop out of queries; rows that flip the other way re-enter
  at whatever progress their `descriptions` / `memes_vec` state already reflects.
  Exact duplicates (same sha256) share metadata by the PK, with every physical
  path recorded in `meme_files`; this column is only
  for phash-similar near-duplicates, which can include false positives — the
  column name reflects that.

## Pipeline stages

Each stage is a separate CLI entry point. All stages are idempotent and resumable.

### Stage 1 — scan & dedup (`scan`)

Two sub-passes, both idempotent, run one after the other in a single `scan`
invocation:

1. **Ingest.** Walk a configured directory of images (jpg/png/webp/gif). For each
   new file (unknown sha256): compute sha256 + perceptual hash (`imagehash`),
   insert into `memes` with `perceptual_duplicate_of=NULL`. Every physical
   path is recorded in `meme_files(path PRIMARY KEY, hash REFERENCES memes(hash)
   ON DELETE CASCADE)`, including identical copies. A path whose content changes
   is reassigned to the new hash. Existing databases seed this table from
   `memes.path`; another scan discovers the previously unrecorded copies.
   Known content skips phash calculation and does not overwrite `memes.path`.
   The first discovered copy becomes the primary path; keep it while its content
   matches the hash. Otherwise select the first matching known copy ordered by
   absolute path. All identical copies share one description per config and one
   embedding; normal describe uses the primary path, while `--path` accepts any
   recorded copy. No `descriptions` rows are created here — a fresh mem is "scanned"
   by virtue of having no matching `descriptions` row for the active config.
2. **Regroup.** Recompute perceptual-duplicate groupings across **every** row
   (not just newly inserted ones) using stored `phash` values: near-duplicates
   share a group when phash Hamming distance ≤ threshold (`phash_threshold` in
   the TOML, currently 8). Pick one representative per group (leave its
   `perceptual_duplicate_of` NULL), set `perceptual_duplicate_of` on the rest to
   the representative's hash. Rep-selection prefers the row with the most
   invested pipeline work: `embedded` (row in `memes_vec`) > any successful
   `descriptions` row > nothing, ties broken by hash. That way already-done
   describe/embed work is never orphaned by a regroup. Rows that flip in or out
   of representative silently drop from / re-enter the pipeline; nothing on the
   `descriptions` or `memes_vec` side is touched.

Two useful flags: `--regroup-only` skips ingest and just re-runs the grouping
pass (cheap; useful when tuning `phash_threshold`); `--prune-missing` removes missing or replaced physical copies from `meme_files`,
selects another primary copy when available, and deletes a meme and its
`descriptions`, `memes_fts`, and `memes_vec` only when no matching copy remains.
Ingest runs before pruning so newly discovered surviving copies preserve work.

No GPU needed. Since regroup only reads `phash`, tuning the threshold on the M1
sample is cheap — re-run `scan --regroup-only`, look at the dedup stats, adjust.

### Stage 2 — describe (`describe`)

For each mem with `perceptual_duplicate_of IS NULL` that has no `descriptions`
row for the current `(model, prompt_version, effort_level)` tuple: send the
image to the VLM, insert a `descriptions` row with `status='described'` and the
raw JSON. Failures insert a `status='failed_describe'` row with the exception
message in `error` (and `description_json = NULL`). Skip logic uses `NOT EXISTS`
against `descriptions` filtered by the same tuple — `IS` handles the NULL
`effort_level` case correctly. `INSERT OR REPLACE` on the unique config index
means retrying a failed row overwrites the old failure cleanly.

- Default backend: Ollama, model `qwen3-vl:8b` (configurable). The exact same
  script runs against `api.anthropic.com` or xAI by swapping `base_url` +
  `api_key` + `model` — no provider-specific code paths.
- Config: `[describe]` block with `base_url`, `api_key`, `model`,
  `prompt_version` (name of a file in `prompts/`, e.g. `"v1"`), and optional
  `effort_level` (`"none" | "low" | "medium" | "high"` — sent as
  `reasoning_effort`; omit for providers that don't support it).
- Second backend, `backend = "claude-code-cli"` (default `"openai"` is the HTTP path
  above): instead of POSTing to `base_url`, spawn the local `claude --print
  --output-format json` CLI, so the run is billed against the Claude Code
  subscription and needs no `api_key`/`base_url`. `model` goes to `--model`,
  `effort_level` to `--effort` (`low | medium | high | xhigh | max`), the image
  is passed as a file path for the CLI's `Read` tool rather than a downscaled
  data URL, and the schema goes in via `--json-schema` — the CLI's stand-in for
  `response_format`, which binds a `StructuredOutput` tool to the schema,
  validates the model's call against it and returns the parsed object as
  `structured_output` in the result envelope. Cheap way to A/B a frontier model
  against Ollama/xAI without an API key; the remaining tradeoff is the CLI's own
  system prompt in context. `--safe-mode` keeps `CLAUDE.md`, skills, plugins,
  hooks and MCP out of the subprocess while leaving OAuth intact.
- CLI flags: `--retry-failed` (also re-pick `failed_describe` rows — without
  it, both successes and failures count as "done"), `--limit N`, and
  `--model` / `--effort` overrides so an alternate variant can be tried without
  editing the config. `--any-effort` counts a row under the same
  `(model, prompt)` at any effort as done. `--path FILE…` is the prompt-iteration
  tool: it bypasses the queue, the duplicate filter and skip logic, and replaces
  an existing row for the same config (announcing it). `-v` prints each
  description as it lands.
- Usage limits on the `claude-code-cli` backend are not failures: a spent quota
  says nothing about the image, and a `failed_describe` row would count as done.
  Detection keys on the result envelope's typed fields (`api_error ==
  "usage_limit_reached"` or `api_error_status == 429`), never on the `result`
  text, whose wording changes between CLI releases. On a hit, no row is written,
  the loop sleeps `USAGE_LIMIT_WAIT` and re-sends the same image.
- Enforce valid JSON output via OpenAI-compatible `response_format:
  json_schema`. Both Ollama and Anthropic-compatible endpoints honor it.
- The schema itself lives in `description_schema.json`, not in `describe.py`,
  which loads it into `SCHEMA` at import (`HUMOR_TYPES` is derived from it).
  It is data the model sees verbatim on both backends, so it belongs in a file
  a human can diff. Caveat: unlike `prompt_version`, the schema is *not*
  recorded on `descriptions` rows and is not part of `descriptions_config_idx`
  — editing it changes what a re-run produces under an unchanged config tuple,
  and `INSERT OR REPLACE` will overwrite the older-shaped row. Convention:
  bump `prompt_version` whenever the schema changes.
- Images are downscaled to max side 1440 px (`MAX_SIDE`; JPEG, quality 90) before being sent
  as a data URL; smaller images pass through untouched, gifs are flattened to
  their first frame. `temperature=0`, `max_tokens` unset (qwen3-vl reasoning
  tokens count against the budget but not against `usage.completion_tokens`, so
  any cap silently truncates mid-CoT and returns empty content).
- The description JSON has these fields (the prompt itself is in Polish since
  the VLM reasons better in Polish about Polish memes, but the returned keys
  are English so schema/code stays one language):
  - `transcription` — all text visible in the image, verbatim (may be Polish or English)
  - `template` — meme template name if recognizable (e.g. "Distracted Boyfriend")
  - `scene` — what is visually happening
  - `the_clue` — always Polish, 2–3 sentences: the concrete trigger and how the
    punchline works (the wordplay, who the meme is aimed at, what reaction it
    goes for). This field carries most of the search quality. It replaced an
    earlier "when would you send this" field — models were consistently bad at
    it, drifting into generic filler ("humorous image expressing frustration"),
    so the field was reframed around the mechanics of the joke, which they can
    actually describe. Descriptions predating the rename were migrated to the
    new key, so the old one appears nowhere in the DB or code — only in the
    superseded `prompts/v1`, `v2` and `vgrok` files that produced those rows.
  - `keywords` — 3–16 unique tags (schema `minItems`/`maxItems`), the same
    concepts given in both Polish and English (`kobieta` + `woman`)
  - `humor_type` — 1–3 values from a closed enum in the JSON schema (the schema
    is authoritative; `cringe`, still listed by older prompts, is gone):
    `irony`, `absurd`, `dark`, `wholesome`, `relatable`,
    `situational`, `shitpost`, `savage`, `political`, `meta`, `wordplay`
    (the punchline *rests* on a rhyme, pun or double meaning). Closed list, not
    free text — a free-text field produces drifting phrasings ("ironiczny" /
    "z ironią" / "sarkastyczno-ironiczny") that dilute the signal; an enum
    stays consistent, works as an FTS tag, and later enables filters (`--typ
    dark`). Feeds into Stage 3's embedding text and the `keywords` FTS column.
    Caveat: humor classification is where a local 7B is weakest — validate on
    the M2 sample that the labels are coherent. If they're noisy, keep the
    field in the schema but drop it from the embedding text until the
    collection is re-described with a stronger model.
- Per-image commit; a single bad image must never kill a run.
- Prompts live in `prompts/<prompt_version>.txt`. Bumping `prompt_version` in
  the config produces a new `descriptions` row on next run without touching
  the old one — cross-prompt comparison is a plain SQL query.

### Stage 3 — embed (`embed`)

For each mem with `perceptual_duplicate_of IS NULL` that has a `status='described'`
row under the active `prompt_version` and is not yet in `memes_vec`, pick one
description: the exact `(model, effort_level)` from `[describe]` if present,
else the same model at any effort, else any model (newest row wins ties).
`prompt_version` never falls back — a prompt swap is a deliberate experiment.
Build the embedding text (`template` unless it is `"unknown"`, `scene`,
`the_clue`, `transcription`, `keywords + humor_type`, joined with newlines;
template names drift between runs, so they help the vector but are unreliable
as FTS tokens, and the shared `"unknown"` would pull memes together), embed with
`bge-m3` via the Ollama embeddings endpoint (1024 dims), L2-normalize the vector
(vec0's default L2 distance then ranks identically to cosine; Ollama doesn't
guarantee normalized output), insert into `memes_vec`, and (re-)insert
`transcription`, `keywords + humor_type`, `template` into `memes_fts`. Per-row
commit keeps the fts + vec inserts atomic and the run resumable.

Because the `[describe]` config drives which variant gets indexed, running
`embed --model <alt>` (or editing the config) picks a different set of
`descriptions` rows without touching prior work. There is only ever one
embedding per mem (PK on `memes_vec.hash`), so switching variants means the old
`memes_vec` row must be cleared first if you want the new one to take effect
— currently that's done manually; automating it can wait.

Embed errors are logged and skipped (no `failed_embed` status stored) — they're
almost always transient endpoint issues, and re-running `embed` re-picks any
`described` mem missing from `memes_vec`.

CLI flags: `--limit N` and `--model` (override which `[describe].model`'s rows
get embedded, without editing the config).

### Stage 4 — search (`search`)

CLI: takes a free-text query, returns top-N file paths.

1. Embed the query with the same model.
2. Run two retrievals: KNN over `memes_vec`, and FTS5 `MATCH` over `memes_fts`
   (sanitize/escape the user query for FTS5 syntax). Join both back to `memes`
   and drop rows where `perceptual_duplicate_of IS NOT NULL` so near-duplicates
   don't clutter results.
3. Fuse with Reciprocal Rank Fusion:
   `score[id] = Σ 1 / (60 + rank_in_list)` across both lists.
4. Output: top-N paths, one per line (composable with `imv`/`feh`/`xargs`),
   plus a `--show` flag that opens results in an image viewer.

### Stage 5 — GUI (`gui`)

The GUI supports finding, previewing and copying memes, inspecting description
variants, and now also reviewing duplicates and deleting selected files.

**Implemented in the GUI:**

- Show the selected meme's path directly below its image, relative to the
  configured `image_dir`. Apply this in search and duplicate views. Keep the
  absolute stored path for file operations; for a file outside `image_dir`,
  show a relative path with `..` rather than dropping location information.
- From a meme, open its duplicate group: the representative plus all rows
  pointing to it, with each image available for preview and the representative
  identified. A meme with no duplicates has a clear empty state.
The selected-meme views open file lists; clicking a path previews it in the
main pane. Copy/open actions use the previewed file and descriptions are loaded
by content hash, including for non-primary copies. `Wróć do wyniku` returns to
the selected search result. Esc closes the list. Hide missing or replaced files
and report their count. Legacy databases without `meme_files` use known paths
from `memes` until the next scan.

**Scope extension agreed; not implemented yet:**

- Offer a separate view of all duplicate groups in the indexed collection,
  independent of the current search query. Include only groups with at least
  two members; show their sizes and allow browsing their members, even when
  those members have no descriptions or embeddings.
- Allow selecting one or multiple memes and deleting their files from disk,
  from both search and duplicate views. The primary use is duplicate cleanup,
  but deletion is not restricted to non-representatives. Delete only explicitly
  selected files, never an entire group implicitly.
- Reflect successful deletions in `memes`, `descriptions`, `memes_fts` and
  `memes_vec`, and refresh visible results and groups. A filesystem failure
  must be reported for that file and must not discard its database records.
  Regroup remaining rows after deletion, using the existing representative
  selection rules; preserve all work belonging to surviving memes. A newly
  promoted representative may need describe/embed before it becomes searchable;
  deletion does not itself run model inference.

Here, a duplicate group means the existing perceptual-similarity annotation;
it may contain false positives, so users choose which files to remove.
Exact copies share one `memes` row but retain all scanned paths in `meme_files`.
The GUI exposes these physical copies too, including groups containing
only identical copies. Group sizes count physical files. Selecting a copy for
deletion removes only that path; retain shared metadata and indexes while another
copy survives. The primary path and the perceptual group representative are
separate choices. Files not yet scanned cannot be listed.

Indexing remains a CLI operation. This extension does not add tagging, image
editing, folder watching or a web UI.

## Build order (milestones — do not skip ahead)

1. **M1:** schema + Stage 1. Verify on the real collection; report dedup stats.
2. **M2:** Stage 2 on a ~30-meme sample only. This is a prompt-iteration loop:
   descriptions get read by a human, prompt gets adjusted, a new
   `prompts/<name>.txt` is added, `prompt_version` in config points at it. Also
   run the same sample against Anthropic / xAI for comparison — since
   `descriptions` keys on `(model, prompt_version, effort_level)`, alternate
   runs accumulate side by side rather than overwriting each other.
3. **M3:** Stages 3 + 4 on the sample. End-to-end search over 30 memes. Quality
   gate: the example queries from the top of this file return sensible results.
4. **M4:** full indexing run (this can take hours — must be resumable), then
   iterate.

All quality decisions (prompt wording, what goes into the embedding text, RRF
constant, FTS weighting) are made on the sample, never on the full run.

**Memes quoted inside the prompt are burned as evaluation samples.** Three
memes from the collection appear in `prompts/` verbatim, so any model
describing them is copying the prompt rather than reasoning, and their
descriptions look good for reasons that do not generalise:

- `045f3bf5` (`hope/682426711_…jpg`) — the Cowboy Bebop line in the `scene`
  bullet's `Dobrze:` example; present since `vgrok3`
- `0be81bf3` (`memes/3JGEMie…-2.jpg`) — the whole of example 3; `vgrok4` only
- `b8bbfce6` (`memes/racism/182884844_…png`) — the whole of example 1;
  `vgrok4` only (the original example 1, a rugby image macro, was not from
  the collection)

Exclude all three when comparing prompt versions. Deliberate tradeoff: the
examples teach real lessons (terse `scene`, naming the concrete utterance a
reaction image answers), which was judged worth three lost samples out of ~125.

## Explicitly out of scope for now

Do not build these unless asked: web UI, reranker (bge-reranker / LLM rerank),
OCR fallback (Tesseract), watch-mode/daemon, thumbnails, tagging UI. If one seems
genuinely needed, propose it first.

## Conventions

- Config in a single TOML file (paths, Ollama base_url, model names, thresholds).
- Small, boring, readable code. Standard library where possible.
- Print progress with counts (`described 142/3801, 3 failed`) — runs are long.
- Never re-do work that is already recorded in the DB.
- After editing Python, run `ruff format .` and `ruff check .` and fix what they flag.

## Commands

- Run a stage: `uv run memes.py <stage> [--config config.toml] [stage-specific flags]`
  (or the module directly: `uv run scan.py --regroup-only`)
- Full incremental pipeline: `uv run memes.py index` (scan → describe → embed)
- Search: `uv run memes.py search "your query" [-n 20] [--show]`
- GUI: `uv run memes.py gui`
- Lint/format after edits: `ruff format . && ruff check .`
- Re-describe a single meme while iterating on a prompt:
  `uv run memes.py describe --path memes/foo.jpg -v`
- Alternate configs sit next to `config.toml` (e.g. `config.opus.toml`: the
  `claude-code-cli` backend writing into the same DB); pick one with `--config`.
- Ad-hoc comparison queries live in `sql/` and run as
  `sqlite3 memes.db < sql/<name>.sql`.

There is no test suite. Behaviour is checked by running a stage on the sample
(`--limit N`, `--path`) and reading the output.

Config lives in `config.toml` (copy from `config.example.toml`). `[describe]` and
`[embed]` blocks each carry their own `base_url` / `api_key` / `model` — swap
`[describe]` to point at Anthropic/xAI without touching the embed side.
