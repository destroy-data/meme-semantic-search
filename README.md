# Meme Semantic Search

Local semantic search over a personal meme collection. You type what you remember
about a meme ("the one with the widening belt"), how the joke goes ("wordplay on
a dead dad"), or loose keywords ("harry potter, little shits") — and get matching
image files back. Everything runs locally: Ollama serves the models, SQLite is
both the storage and the search index.

## The core idea

No CLIP. Images are never searched directly — a VLM writes a structured
description of each meme once, at indexing time, and queries run against those
descriptions. Two reasons:

- Your query and the text baked into the image are routinely in different
  languages. An image embedder has nothing to grab onto; a multilingual text
  embedder does.
- "Wordplay on a dead dad" describes how the meme *works*, not what it looks
  like. Embedding models don't reason at query time, so that judgment has to be
  made up front and written down. `the_clue` field carries most of the search quality.

Indexing is therefore slow and, on a cloud VLM, costs money — so raw VLM output
is stored verbatim, and re-embedding never requires re-describing.

## Setup

```fish
uv sync
ollama pull qwen3-vl:8b              # VLM; any vision model works
ollama pull bge-m3                   # embeddings, 1024-dim, multilingual
cp config.example.toml config.toml   # then set image_dir
```

`[describe]` and `[embed]` each carry their own `base_url` / `api_key` / `model`,
so describing can point at cloud while embedding
stays local. Any OpenAI-compatible endpoint works. Alternatively,
`backend = "claude-code-cli"` in `[describe]` runs the local `claude` CLI instead
of an HTTP call, billed against a Claude Code subscription; when its usage limit
runs out, `describe` waits and retries the same image rather than marking it
failed.

Animated GIF/WebP/APNG files share one frame-selection path across all describe
backends. Install `ffmpeg` on PATH. Pillow decodes frames and their durations;
FFmpeg detects cuts with `scene > 0.15`. Each shot contributes its middle frame;
without cuts, the middle and last frames are sent. Repeated selections of the
same frame are collapsed. Selected PNGs retain native resolution and are sent
in chronological order with timestamps (HTTP image URLs, Claude file paths,
Codex image attachments). Temporary files are removed after the request.
No model is asked to extract animation frames. Extraction errors become
per-image failures; existing descriptions are still skipped. Use a new prompt
version when comparing the new inputs with older first-frame descriptions.

### Describe through Codex CLI

In a copy of your TOML, replace `[describe]` with:

```toml
[describe]
backend = "codex-cli"
model = "gpt-5.4" # use a vision-capable model available to your Codex account
prompt_version = "vcodex1-master" # prompts/vcodex1-master.txt
effort_level = "low" # optional; supported values depend on the model
```

Run `uv run memes.py describe --config config.codex.toml --limit 1 -v`.
Requires a logged-in `codex` on PATH and the flags available in CLI 0.162.0
(the version used for implementation). It uses existing CLI authentication;
`base_url` and `api_key` from the meme config are unused. Model and effort
CLI overrides work as before. Billing follows the CLI's login method.

Each still is attached as PNG (max side 1440); animations attach the selected
frames at native resolution. `prompts/<prompt_version>.txt` is copied verbatim
to a temporary instructions file and passed via `model_instructions_file`,
replacing Codex's built-in main instructions. Stdin carries only the request to
describe the meme and, for animations, the frame order and timestamps.
The output schema and final-response file are separate temporary files. Runs use
a temporary working directory, read-only sandbox, no user config/rules or
AGENTS.md, and disable shell, web search, hooks, apps, remote plugins and host
skill discovery. Managed policy and additional environment/tool instructions
can still apply. Session logging is enabled for auditing.
With `-v`, the per-meme header also shows `[N tokens]` from Codex's completed
turn: input plus output tokens, including cached input once. If usage is absent,
the token count is omitted. Reported usage is stored in `descriptions.total_tokens`
for all three backends (input plus output, counting cached input once); unknown
usage is `NULL`. `descriptions.usage_json` preserves the backend's full usage
object, including input/output, cache and reasoning counts when reported, without
renaming or dropping fields. Existing databases gain both columns automatically
on connection, with older descriptions left as `NULL`.

Codex requires `additionalProperties: false`; the temporary schema adds it and
omits unsupported `uniqueItems`, which is checked locally instead. The canonical
schema and prompt version are unchanged. Raw final JSON is stored verbatim.

A Codex process failure, timeout or missing completed response stops the run
**without writing a description row**. Re-run after resolving the error; no
`--retry-failed` is needed for that image. Unlike Claude, Codex quota failures
are not automatically slept/retried: JSONL errors do not provide a verified
stable quota discriminator here. Invalid completed descriptions and unreadable
images still become `failed_describe`. Both CLIs share timeout/Ctrl+C handling.
The backend is not part of the existing DB uniqueness key; equal
`(model, prompt_version, effort_level)` tuples still share one row.

CLI protocol reference: [OpenAI non-interactive mode](https://developers.openai.com/codex/noninteractive).
Instructions setting: [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Usage

```fish
uv run memes.py index                         # scan -> describe -> embed
uv run memes.py search "widening the belt"    # top matches, one path per line
uv run memes.py search "harry potter" --show  # open in an image viewer
uv run memes.py gui                           # browse with previews
```

`index` is incremental and resumable — re-run it after adding memes and only new
work happens. The first full run takes hours; killing it loses at most one image.

| command | flags |
|---|---|
| `index` | `--limit N`, `--retry-failed` |
| `scan` | `--regroup-only`, `--prune-missing` |
| `describe` | `--limit N`, `--retry-failed`, `--any-effort`, `--undescribed-only`, `--model M`, `--effort E`, `--path FILE...`, `-v` |
| `embed` | `--limit N`, `--model M` |
| `search` | `QUERY...`, `-n/--top N`, `--show` |
| `gui` | — |

All take `--config` (default `config.toml`). Every stage also runs standalone:
`uv run describe.py --limit 10`. `--model` on `embed` picks *whose descriptions
get embedded*, not the embedding model.

`describe --undescribed-only` skips memes with any successful description,
regardless of model, prompt or effort. Failed attempts under the selected config
still require `--retry-failed`. This flag cannot be combined with `--path`.

## The pipeline

**`scan`** walks `image_dir`, keys each file by sha256 and stores a perceptual
hash. Then it groups near-duplicates (phash Hamming distance ≤
`phash_threshold`): one representative per group stays searchable, the rest drop
out of every downstream query. Regrouping only reads stored hashes, so
`scan --regroup-only` after changing the threshold is cheap.

**`describe`** sends each representative to the VLM and stores JSON enforced by
`response_format: json_schema`:

| field | what it holds |
|---|---|
| `transcription` | all visible text, verbatim |
| `template` | meme template name, if recognizable |
| `scene` | what is visually happening |
| `the_clue` | 2–3 sentences: the trigger and how the punchline works |
| `keywords` | 3–16 tags, each concept in both Polish and English |
| `humor_type` | 1–3 values from a closed enum (`irony`, `dark`, `wordplay`, …) |

`humor_type` is an enum because free text drifts ("ironic" / "with irony" /
"sarcastic-ironic") and dilutes the signal. Failures are recorded as
`failed_describe` rows rather than killing the run.

The schema lives in `description_schema.json`. `--path` re-describes specific
files regardless of what is already recorded, which is the quick loop for
iterating on a prompt.

**`embed`** joins `template` + `scene` + `the_clue` + `transcription` + tags
into one text, embeds it with bge-m3, and fills `memes_vec` plus the FTS index.
It takes the description matching `[describe]` and, failing that, falls back to
the same model at another effort level, then to any model with the same prompt.

**`search`** embeds the query, runs a KNN over the vectors and an FTS5 `MATCH`,
then fuses the two rankings with weighted Reciprocal Rank Fusion
(`Σ weight/(60 + rank)`). Equal weights preserve the original ranking. The
vector side handles paraphrase; FTS catches exact strings and proper nouns the
embedder blurs. RRF needs no score calibration between them, which is the point.

## The database

One SQLite file is queue, metadata store and search index. DDL in `schema.sql`.

- **`memes`** — `hash` (sha256, PK), `path`, `phash`, `perceptual_duplicate_of`
- **`meme_files`** — every scanned physical path, linked to its content hash;
  identical copies share a `memes` row and its descriptions/embedding
- **`descriptions`** — one row per describe *attempt*, unique on
  `(hash, model, prompt_version, effort_level)`
- **`memes_fts`** — FTS5, `remove_diacritics 2`
- **`memes_vec`** — sqlite-vec `vec0`, one 1024-dim embedding per meme

A meme's pipeline position is derived, not stored: *scanned* = no `descriptions`
row for the active config, *described* / *failed* = a row with that status,
*embedded* = a row in `memes_vec`.

Because `descriptions` keys on the full config tuple, model and prompt variants
accumulate side by side instead of overwriting each other. That's what makes
comparing two models a plain `GROUP BY`, and what the GUI's variant dropdown
reads.

## Code layout

```
memes.py    aggregate CLI: index + the individual stages
common.py   config loading, DB connection, query embedding
scan.py  describe.py  embed.py  search.py    stages 1-4
gui.py      Dear PyGui frontend over search()
schema.sql  DDL        prompts/  versioned prompt files
```

Each stage module exposes `add_arguments(parser)` / `run(args, cfg)` /
`main(argv=None)`, so `memes.py <stage>` and running the module directly share
one implementation. After editing: `ruff format . && ruff check .`

## GUI scope

The top row has a **FTS / Embeddingi** balance slider between the search field
and the **Szukaj / Lista** switch. It starts at 50% embeddings / 50% FTS. Move
left for exact text matching, right for semantic similarity; the endpoints use
only FTS or only embeddings. Results refresh after a short debounce. The slider
is hidden in Lista and retains its setting when switching modes. CLI search
continues to use equal weights.
**Szukaj** performs semantic search; **Lista** browses all scanned physical
files, including memes without descriptions or embeddings. Its query field
filters names and paths locally (case-insensitive, no model calls).

The collection table shows name, folder relative to `image_dir`, number of
identical copies (including the displayed file), and indexing state. Clicking a
row previews it; checkboxes independently select physical paths for future bulk
operations. Switching modes preserves each query, preview, selection and scroll,
as well as the collection filter and expanded groups. Status and F1 stay visible.

Filters: **Wszystkie**, **Duplikaty** (expandable perceptual groups with at least
two available physical files, including groups made only of identical copies),
**Identyczne kopie**, **Nieopisane**, **Błędy**. Description states refer to the
active `(model, prompt_version, effort_level)`; an additional “w indeksie” marker
means a vector exists, which may come from a different description variant.
“Nieopisane” means no attempt for the active config, “Błędy” means a failed attempt.
Group sizes count available physical files before the path filter is applied.
Collection data is loaded on first entering Lista; restart the GUI after a CLI
scan or indexing run to reload it.

In **Lista**, **Embeduj opisane** fills missing vector and FTS indexes across the
whole collection, regardless of prompt, model, filters or checked files. For each
representative, it uses the newest successful description across all prompts,
models and effort levels, without preferring the active configuration. Existing vectors
are skipped. Embedding runs in the background with progress and failure counts;
the table and search refresh after completion. Deletion is temporarily blocked
while it runs. Failed items can be retried with the same button.

The current GUI searches as you type, previews selected images, lets you inspect
description variants, and supports keyboard navigation, copying an image or its
path, and opening it in an external viewer.

The GUI also displays the path below the image, relative to `image_dir`.
From the selected meme, use **Grupa duplikatów** to browse its perceptual group
(including physical copies), or **Identyczne kopie** for files with exactly the
same content. Lists open in the main window’s left panel and identify the representative and primary copies. Click a
path to preview that file; descriptions, copying and opening then use the
previewed file. **Wróć do wyniku** returns to the selected search result. Esc
closes the list. Missing or replaced files are hidden with a count.

Legacy databases can still show perceptual groups from `memes`; run scan to
populate `meme_files` and discover all identical copies.

Use checkboxes and **Usuń zaznaczone pliki** in Lista or its duplicate
views to delete physical files. Search mode has no deletion controls.
The right-click menu on a file in Lista offers **Usuń plik**
and **Usuń resztę z grupy**. The latter keeps the clicked physical file and lists
all other available group members, including identical copies, for confirmation.
Deletion is permanent and requires confirmation. Immediately before deletion,
the file's SHA-256 must match the database; replaced files and filesystem failures
are reported and retain their records. Successful deletions update the database,
visible results and groups. Moving, renaming, scanning and describing from the
GUI remain deferred.

Deletion of a representative selects a new one from the remaining group using the
existing grouping rules, preserving surviving descriptions and embeddings.
If no surviving member has a vector, deletion transfers the removed member's
vector and FTS text to an available survivor, preserving group searchability
without inference. This inherited index describes the similar removed image;
description rows are not copied and existing survivor indexes are preserved.

Groups refer to perceptually similar images and can include false positives.
Identical copies share one metadata row but keep every scanned path in
`meme_files`. The first discovered copy remains the primary path for describing
and search; scanning switches to the first matching copy ordered by path only
when the primary disappears or changes content. `describe --path` accepts any
recorded copy. Re-scan an existing collection to discover copies whose paths
were previously overwritten. Pruning removes shared metadata only when no copy
remains. GUI groups include identical copies and count physical files. GUI
deletion of one copy preserves shared work while another remains. Indexing
remains in the CLI; tagging, image editing, folder
watching and a web UI are outside this extension.

## Gotchas

- **Switching description variants doesn't clear old vectors.** `memes_vec` holds
  one embedding per meme, so `DELETE FROM memes_vec; DELETE FROM memes_fts;`
  before re-running `embed`.
- **The describe timeout covers the whole generation**, since the response isn't
  streamed — 276 s in `describe.py`, against 69 s for embeddings. Raise it if
  `The read operation timed out` starts piling up in `descriptions.error`.
