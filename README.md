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
| `describe` | `--limit N`, `--retry-failed`, `--any-effort`, `--model M`, `--effort E`, `--path FILE...`, `-v` |
| `embed` | `--limit N`, `--model M` |
| `search` | `QUERY...`, `-n/--top N`, `--show` |
| `gui` | — |

All take `--config` (default `config.toml`). Every stage also runs standalone:
`uv run describe.py --limit 10`. `--model` on `embed` picks *whose descriptions
get embedded*, not the embedding model.

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
then fuses the two rankings with Reciprocal Rank Fusion (`Σ 1/(60 + rank)`). The
vector side handles paraphrase; FTS catches exact strings and proper nouns the
embedder blurs. RRF needs no score calibration between them, which is the point.

## The database

One SQLite file is queue, metadata store and search index. DDL in `schema.sql`.

- **`memes`** — `hash` (sha256, PK), `path`, `phash`, `perceptual_duplicate_of`
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

## Gotchas

- **Switching description variants doesn't clear old vectors.** `memes_vec` holds
  one embedding per meme, so `DELETE FROM memes_vec; DELETE FROM memes_fts;`
  before re-running `embed`.
- **The describe timeout covers the whole generation**, since the response isn't
  streamed — 276 s in `describe.py`, against 69 s for embeddings. Raise it if
  `The read operation timed out` starts piling up in `descriptions.error`.
