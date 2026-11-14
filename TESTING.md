# Manual verification checklist

The project has no automated test suite yet. This list defines the minimum to
go through on a small, controlled collection before a full indexing run or after
changing any pipeline stage.

## Preparation

- [ ] Work on a copy of a dozen or so images, not on the real collection. Set a
  separate `db_path` and `image_dir` in a test TOML.
- [ ] Include in the sample: several unrelated memes, two identical copies of a
  file, a pair of clearly similar images, one corrupted file with an image
  extension, and at least one meme in Polish and one in English.
- [ ] Do not use the "burned" samples listed in `AGENTS.md` for manual review of
  descriptions: `045f3bf5`, `0be81bf3`, `b8bbfce6`, `6e7aa3c5`.
  The last one is example 4 from `vcodex3`: a host of TVP's pandemic-era school
  lessons paired with Monkey Puppet.
- [ ] For `vcodex3`, also check an image without a joke: `the_clue` describes the
  topic or message, and `humor_type` may be `[]` if no category fits.
  This prompt version introduces empty-list support in the schema; older valid
  descriptions remain compatible with the extended contract.
- [ ] Run `ruff format . && ruff check .`; both must pass without changes or
  errors.
- [ ] After `uv sync --locked` (without the `gui` extra) the CLI stages work, and
  `uv run memy gui` prints the hint `uv sync --extra gui` and exits with a
  non-zero code. For GUI tests use `uv sync --locked --extra gui`.
- [ ] Relative `db_path` and `image_dir` in the test TOML also work when run
  from another directory (`--config` with an absolute path).

## Scanning and deduplication

```sh
uv run memy scan --config config.test.toml
```

- [ ] The `new` count matches the number of distinct file contents; identical
  copies do not create extra rows in `memes`, but each has its own entry in
  `meme_files`. A second scan keeps all paths and the primary path.
- [ ] Set `image_dir` to a relative path, then check in SQLite that every
  `memes.path` value is absolute.
- [ ] Set `exclude_paths` to one file and one subdirectory relative to
  `image_dir`. Check that both are skipped by `scan` and `all`, while a
  directory with a similar name is still scanned. Previously stored paths of
  excluded files disappear from `meme_files`. Absolute paths and paths
  containing `..` are rejected.
- [ ] A corrupted image is reported as skipped but does not abort the scan.
- [ ] A second identical scan reports `0 new` and creates no further records.
- [ ] In SQLite, every group of similar images has exactly one representative
  (`perceptual_duplicate_of IS NULL`), and the rest point to its hash.
- [ ] Temporarily change `phash_threshold`, run `scan --regroup-only` and check
  that only the groups change, not descriptions or vectors.
- [ ] Delete the primary identical copy and scan: another existing copy becomes
  primary, and the hash, descriptions and embedding remain. `--path` also
  accepts a non-primary copy. After replacing a file's contents, its
  `meme_files` entry points to the new hash, and the old meme uses another copy.
- [ ] Delete all copies of a test file and run a plain `scan`: paths, FTS and
  vector disappear, but `memes` and `descriptions` remain. Another scan does not
  restore the old paths, and `memes.path` is empty. `describe`, `embed`,
  `search` and the GUI skip the archived record; after restoring an identical
  file its description is reused and the embedding can be rebuilt. The
  remaining group is grouped correctly.
- [ ] With `--prune-missing`, also check a nonexistent path stored outside the
  current `image_dir`; its description stays in the database.

## VLM descriptions

```sh
uv run memy describe --config config.test.toml --limit 10 -v
```

1. [ ] `describe --shuffle --limit 1` picks randomly from the whole eligible
   queue before applying the limit. Saved descriptions, non-retried failures,
   missing files and duplicates are still skipped by the existing rules. Without
   the flag the order stays by hash. `--undescribed-first --shuffle` shuffles
   each group separately, keeping memes without a successful description first.
   `--path ... --shuffle` shuffles the given files. The flag also works in `all`
   and `describe embed`.

- [ ] Descriptions are created only for representatives of similar-image groups.
- [ ] Every saved JSON has all fields from `description_schema.json`, and
  `keywords` and `humor_type` respect the limits and enum from the schema.
- [ ] `transcription` matches the text in the image, and `the_clue` explains the
  specific joke rather than a generic "funny picture".
- [ ] A second run with the same configuration does not resend described memes.
- [ ] `--path file.jpg -v` deliberately replaces only the selected file's
  description for the active `(model, prompt_version, effort_level)`.
- [ ] Changing `prompt_version`, model or effort creates a description variant
  next to the old one instead of deleting it.
- [ ] A model error or a single bad file records `failed_describe` and does not
  stop the rest of the queue; `--retry-failed` picks it up again.
- [ ] For the `claude-code-cli` backend, check separately that the result comes
  from `structured_output`, and that hitting the usage limit is not recorded as
  `failed_describe`.

## Embedding and indexes

```sh
uv run memy embed --config config.test.toml
```

- [ ] Every described representative gets an entry in `index_entries` (with an
  `entries_fts` row with the same rowid) for the description chosen by the
  fallback order, and a vector in `embeddings` from every model in `[embed]` and
  `[[embed.extra]]`.
- [ ] `embed --all-variants` adds entries for the remaining descriptions;
  existing entries and vectors stay unchanged.
- [ ] Re-describing a meme (`describe --path`) produces a new `descriptions.id`;
  the old entry drops out of search, and the next embed replaces it.
- [ ] `search --exact` with another variant (`--desc-model/--prompt/--effort`)
  returns only memes with that variant; `--embed-model` uses another model's
  vectors.
- [ ] FTS contains the transcription, tags and humor type; the embedding text
  contains the scene and `the_clue`.
- [ ] A second run does not call the embeddings endpoint for already embedded
  memes.
- [ ] Temporarily stop the embeddings endpoint: the failing meme is skipped, and
  after the endpoint is back the next run fills it in.
- [ ] After manually deleting `index_entries` rows, another embed rebuilds FTS
  and vectors from scratch (a trigger deletes FTS rows, a cascade deletes
  vectors).

## CLI search

```sh
uv run memy search --config config.test.toml "description or quote" -n 10
```

- [ ] A Polish query finds an English meme with the same meaning and vice versa.
- [ ] An exact quote or an unusual name returns the right meme thanks to FTS.
- [ ] A description of the situation or joke mechanic (`the_clue`), without words
  visible in the image, returns a sensible result thanks to vectors.
- [ ] Results contain no non-representatives of similar-image groups.
- [ ] Every printed path exists and opens the right file. `--show` does not
  change the order or set of results.

## GUI

```sh
uv run memy gui --config config.test.toml
```

1. [ ] In List, click Embed described. The button works on the whole
   collection, regardless of prompt, model, filter and checkboxes. It skips
   existing vectors and non-representative duplicates. For each meme it picks
   the newest successful description across all configurations. On completion
   the table shows the new "indexed" state, and search includes the new memes
   without a restart.
2. [ ] During embedding, progress and the error count are visible; the
   collection can be browsed and modes switched. Another run and deletion are
   blocked. An endpoint error does not stop the remaining memes. Clicking again
   picks up only missing vectors; an empty queue and a start error have a clear
   message.

- [ ] At the top there is a Search / List / Compare switch. List shows a table
  on the left and the shared preview on the right. Typed text filters names and
  paths without calling a model. Returning to each mode preserves its query,
  preview, selection and the scroll position of both panes.
- [ ] Next to the search field there is an FTS/embedding slider (`50% emb.`).
  The default 50% keeps the existing ranking. Fully left: FTS only (also works
  without the embeddings endpoint); fully right: embeddings only. A change
  refreshes the results after a short delay. The slider is hidden in List, and
  its position and the control order survive repeated mode switches.
- [ ] The table contains the name, folder, number of identical copies (including
  the current file) and description state for the active configuration; vector
  presence is shown separately. Clicking a row changes the preview, a checkbox
  only the file's selection.
- [ ] Check all five filters. Undescribed covers no attempt for the active
  configuration, Errors a failed attempt, even when another variant has a valid
  description. Duplicates contains expandable groups of at least two available
  files, including groups of only identical copies. Group size counts all
  available files, even after path filtering.
- [ ] The filter, expanded groups and checkboxes survive switching to Search and
  back. No matches gives a message and clears a non-matching preview.
- [ ] Without a running Ollama, the list, filters and preview still work.
- [ ] After collection changes made through the CLI, restart the GUI to read the
  new data.
- [ ] Typing a query runs the search after a short pause, and Enter runs it
  immediately.
- [ ] Clicking and arrow keys change the selection, image preview and
  description of the same result.
- [ ] Page Down moves the preview 10 results forward and Page Up 10 back, also
  with focus in the query field. The list scrolls to the selected result; jumps
  stop at the first and last result.
- [ ] If a meme has several description variants, Tab and the variant list
  switch the description without changing the image. An expanded list stays
  open for a few seconds without clicking; the mouse can be moved to another
  entry to pick it. Focus returns to the query only after a choice.
- [ ] Replace a file's contents under the same path without scanning. A result
  with the old hash must not appear in the list; the status should report the
  number of hidden outdated records.
- [ ] Check the basic actions: copying the image/path, opening in a viewer,
  clearing with Esc and changing the number of results.
- [ ] After a load, copy or open error on one meme, moving to another clears the
  previous message. If the next meme also fails, the status bar shows the new
  message.
- [ ] The status bar with "F1: shortcuts" is visible without scrolling, also with
  a long description, many results, a long status message, a window resize and
  full-width preview. Only the content panes scroll.

1. [ ] In Search and List, drag the vertical divider between the list and the
   preview both ways, also beyond the window. Both panes keep a minimum width,
   the image resizes and the description rewraps without a restart.
2. [ ] Set different widths in both modes and switch several times. Each mode
   restores its width, also after opening and closing duplicates or identical
   copies. F11 hides the list and divider; F11 again restores the previous
   split, also inside an open group.
3. [ ] Shrink and enlarge the window after widening the panes. The preview and
   footer stay inside the window, and enlarging restores the chosen list width.

Verified 2026-10-26: a script passed with real Dear PyGui rendering and
simulated drag events on a temporary database and image. Checked both modes,
separate widths, minimum sizes, image scaling, descriptions, both duplicate
views, F11, and shrinking and restoring the window. Formatting and lint passed.

### Comparing descriptions

1. [ ] Compare is a separate view next to Search and List. It can be entered
   without a selected meme, with one description or with none, also with an
   empty collection. Compare descriptions opens the comparison from the current
   preview. Each column has independent model, prompt and reasoning filters.
2. [ ] Scene sits next to scene even when the JSON field order differs.
   Different text lengths do not shift later fields between columns; a missing
   field is marked. Both columns scroll together, and descriptions rewrap after
   a window resize. The footer stays visible.
3. [ ] Changing the variant affects only the selected column. The pair survives
   moving between memes and closing and reopening the comparison. A meme without
   the selected variant shows a message, with no automatic substitution; a meme
   without any successful description does not appear in the comparison
   sequence.
4. [ ] Previous/next meme and the arrow keys move through Search results, the
   filtered List or an open duplicate group, depending on where comparison was
   entered. At the ends of the sequence the buttons are disabled. An expanded
   variant choice stays open and does not switch memes while choosing.
5. [ ] Clicking the thumbnail enlarges the image. Esc first closes the
   enlargement, then the comparison. Back restores the original meme,
   description variant, query, selection, scroll and the F11 or open-group
   state.
6. [ ] An unavailable, replaced or corrupted file when moving to the next meme
   gives a message and keeps the current image and descriptions. Repeated meme
   changes, enlarging and leaving leave no stale images behind.
7. [ ] Every filter has an Any option. Values cover all successful descriptions
   in the database; failed attempts add no variants. Not set selects a NULL
   effort, and `none` only an explicit `none`. Set a prompt in one column and Any
   in the other; both columns keep their own constraints and selected variant.
8. [ ] With several matching configurations, the variant list is visible below
   the filters. With one, the choice is automatic and the list disappears. A
   parameter combination without matches shows a message. Filters and the last
   meme survive switching to another view and back to Compare.
9. [ ] Parameter filters restrict the current sequence to memes with at least one
   matching successful description on each side. Any allows every variant but
   still requires a description. A failed attempt does not qualify. One side's
   parameters must match the same description.
10. [ ] Narrowing keeps the current meme if it still matches; otherwise it
    selects the first match. The counter and buttons reflect the result at once.
    Zero matches clears the previous image and descriptions, leaving the filters
    and a message. Loosening restores hidden memes in their earlier order, also
    after switching views with an empty result. Comparison does not add memes
    from outside the source results/list/group or change the source list.
11. [ ] Copy comparison copies just the JSON with keys `meme`, `left` and
    `right`. The meme has its relative path, SHA-256 and dimensions, and each
    side the actually selected model, prompt, effort and full description,
    including empty fields and lists. Polish characters, newlines and quotes are
    preserved. A missing side or empty result disables the button; choosing an
    available variant enables copying again.
12. [ ] Copy all pairs copies `{"comparisons": [...]}` in the current list's
    order, with exactly the two variants selected below the filters. With Any,
    the export does not substitute another variant available for the meme. A
    meme with only one selected variant, a failed attempt, or an unavailable or
    replaced file is skipped. The status gives the number of pairs and skips.
13. [ ] Exporting many pairs keeps the current image, selection and filters. It
    also works when the current meme lacks a selected variant but other memes
    have the full pair. An empty export result keeps the previous clipboard.
14. [ ] A missing selected variant shows Describe and embed this meme on the
    right side. Only the current hash is described, with the exact model, prompt
    and effort from the lower list, also with Any. Not set removes effort from
    the active configuration; `none` stays a separate value.
15. [ ] The saved description appears immediately, still during embedding,
    without changing variants, meme, image or field scroll. Navigation and view
    changes work while it runs. Finishing the task does not bring back the
    previous meme. Re-adding the same hash and variant, embedding the whole
    collection and deletion are blocked until the queue finishes.
16. [ ] The embedding is built from the new variant, also for a group
    non-representative and a physical copy. The existing vector and FTS of that
    hash are replaced together. Other memes and descriptions stay unchanged. An
    embedding error gives a small message by the side, keeps the description and
    previous indexes and starts no further attempt. A description error does not
    start embedding; a replaced or unindexed file is not sent to the model.
17. [ ] While running, Add to queue accepts further memes and different variants
    of the same meme. Each task keeps its own model, prompt, effort, backend and
    embedding settings from the moment of the click. Later filter and
    configuration changes do not replace them. Order follows the clicks, tasks
    do not run in parallel, and physical copies do not duplicate a task for the
    same variant. The waiting count and the variant's position are visible.
18. [ ] A description, embedding or file error does not stop the next task.
    Embedding after an error is still not retried. The queue keeps working after
    a view change; once empty it re-enables collection embedding and adding
    tasks and hides the counter. Refreshing does not change the viewed meme.

Verified 2026-10-26: a script passed with a real Dear PyGui window, a temporary
database and images. Checked field alignment in different orders, shared
scrolling, navigation through results, the filtered list and a group, pair
preservation, one or both variants missing, image enlargement, Esc, state
restoration, narrowing the window and a load error. The pane divider and
background embedding checks passed again. After adding the separate view, also
checked entering without a selection, resuming, independent filters, Any, the
NULL/none distinction, zero/one/many matches and an empty collection.

Filter verification 2026-10-26: a script checked in the rendered GUI the
intersection of both filters, Any, NULL/none, failed descriptions, parameters
from one description, physical copies, order, navigation, an empty result and
resuming, restoring hidden memes and preserving the source list. The database
stayed unchanged. The pane divider check also passed.

Copy verification 2026-10-26: a rendered GUI test intercepted the clipboard
write and checked valid JSON, the parameters of the actually selected variants,
full descriptions with Polish characters, empty fields and lists, and blocking
when a variant is missing or the result is empty. The desktop clipboard stayed
unchanged.

Multi-pair export verification 2026-10-26: a test checked that a single pair
matches the bulk export, exact variants despite Any, list order and scope,
physical copies, skipping incomplete pairs and replaced files, preserving the
current preview, and not overwriting the clipboard on an empty export.

Variant generation verification 2026-10-26: a script passed with the rendered
GUI, a temporary database and simulated services. Checked the exact
model/prompt, NULL/none, a physical copy of a non-representative, the
description appearing before embedding finishes, navigation and view changes
during work, blocking parallel tasks and deletion, index replacement and
preservation on a request error and write rollback. A failed embedding had one
attempt, and a description error and a replaced file did not start embedding.
The configuration stayed unchanged. Earlier filter, comparison copy and
collection embedding tests, formatting and lint also passed.

Queue verification: a script passed with the rendered GUI, a temporary database
and simulated services. Consecutive tasks had different models, prompts, effort
(including NULL/none), backends and embedding settings. Checked that a nested
configuration copy is preserved, click order, no parallel processing,
deduplication across sides and physical copies, different variants of one meme,
queue positions and counter. Description, embedding and replaced-file errors let
the next task through; embedding had one attempt. Waiting tasks also finished
after switching to List. The preview and variants stayed unchanged, and once the
queue was empty collection embedding became available again. Regression checks
of single-variant generation, filters, export and collection embedding passed.

### Duplicate and identical-copy preview

- [ ] The Duplicate group, Identical copies and Back to result buttons are
  visible only when the group contains more than one available file (a similar
  meme or an identical copy). For a single meme and for a group with only
  outdated extra paths, the whole set of buttons is hidden.
- [ ] Below the image in results and duplicate views, the path relative to
  `image_dir` is shown, also for files in subdirectories. A file outside
  `image_dir` has a readable relative path with `..`.
- [ ] From a selected meme its group can be opened, each member previewed and the
  representative recognized. A meme without duplicates has a clear message.
- [ ] Group views also include non-representatives and memes without
  descriptions and embeddings; normal search still shows only representatives.
- [ ] Identical copies shows all available scanned paths with the same hash,
  including non-primary copies. They share description variants.
- [ ] After clicking a path, preview, copy and open apply to that copy. The
  group/copy list is in the main window. Back restores the preview from which
  the group was opened, also in List; Esc closes the list. Switching modes keeps
  the open group or identical-copy list.
- [ ] Outdated paths are hidden with a message giving their count. A database
  without `meme_files` still allows browsing known groups of similar images.

### Deleting files

Moving, renaming and indexing from the GUI are deferred. Test deletion only on a
copy of the collection prepared for verification.

- [ ] In List and groups, one or several memes can be selected for deletion.
  Only the selected files disappear from disk, and their `meme_files` entries
  disappear from the database. The meme record, descriptions, FTS and vector
  disappear only after the last identical copy is deleted. The list and preview
  refresh after the operation.
- [ ] Deleting a representative leaves correct groups with a new representative;
  descriptions and embeddings of surviving memes are neither deleted nor
  regenerated. Deleting a whole group removes it from the view, and leaving one
  member makes it no longer shown as a duplicate group.
- [ ] On a file deletion error, the GUI names the file concerned, keeps its
  records and correctly shows the other successful deletions.
- [ ] Groups include all scanned identical copies from `meme_files`, also a group
  made only of identical files. Group size is the number of physical files.
  Deleting one copy keeps the shared hash's descriptions and indexes while
  another copy exists.

## Full run, separate stages and resuming

```sh
uv run memy all --config config.test.toml --limit 10
```

- [ ] `all` runs scan, describe and embed in order, while `scan`, `describe` and
  `embed` alone run only their own stage.
1. [ ] `scan embed --config config.test.toml --limit 1` runs scan and then embed
   without describing. Flags after the stage names are shared; `--limit` limits
   only embed. `describe embed` accepts the shared `--model` and `--limit` and
   the describe flags. `search "scan embed"` is still a query.
- [ ] `all` accepts stage flags: `--regroup-only` and `--prune-missing` for
  scanning, `--path` or `--undescribed-only`, `--any-effort`, `--effort` and
  `-v` for describing, and `--model` and `--limit` for describing and embedding.
- [ ] Interrupt `describe` after one image is finished, run `describe` again and
  check that saved descriptions are not redone. Repeat `embed` and check that it
  skips existing embeddings.
- [ ] Finally run a few of the example queries from `AGENTS.md` and judge the
  results manually before indexing the whole collection.

## List implementation verification (2026-10-09)

On a temporary collection, all filters, physical copy counts, missing/replaced
paths and a database without `meme_files` were checked programmatically. A test
with a real Dear PyGui window and frame rendering passed table creation, preview
and checkbox callbacks, mode switching with separate queries, returning from
identical copies, an empty result and F1 visibility. This does not replace
manual review of layout, clicking and scrolling on a large collection.

## Claude and OpenCode isolation

1. Claude: run the real CLI against a local Anthropic endpoint with a fake login.
   The request must contain base64 images and only `StructuredOutput`. There must
   be no Read, Bash, network/MCP tools or source paths. Check reading the last
   JSONL `result`, `structured_output` and usage.
2. OpenCode V2: a local OpenAI-compatible provider, isolated test data. The
   request must contain `tools: []`, the images and the schema; the system
   instructions must contain the exact prompt. `cwd` and `PWD` point to the same
   temporary directory. Repository settings, global AGENTS.md, MCP, skills and
   external plugins must not reach the request. The CLI keeps its own login and
   session handling. Also check that OPENCODE_CONFIG_DIR and XDG_CONFIG_HOME are
   temporary, and that OPENCODE_CONFIG and OPENCODE_CLI_CONFIG_CONTENT are not
   inherited. An inotify monitor on the real ~/.config/opencode must report no
   opens or reads while the client and server run.
3. Check a static image, frame order and timestamps, and files with spaces.
   OpenCode scales only static images to 1440; Python passes Claude a native PNG,
   but its CLI may downscale images further.
4. In the test database check saving/skipping a description, a separate effort,
   invalid JSON and retry. OpenCode: a process error, account limit, missing
   result or tool attempt must stop the queue without a record. A timeout and an
   invalid description are per-image failures. Claude: typed limits still trigger
   waiting without recording an image failure.
5. OpenCode: a valid complete JSON with exit 0 and no `step_finish` events is also
   a success (the V2 catch-up path). Usage then stays NULL; with native tokens,
   keep the whole object and count only an explicit `total`.
6. Check file cleanup after success/failure, timeout/Ctrl+C, and no Codex
   regressions after extracting the shared validation and PNG preparation.

Verified 2026-10-25: a script ran Claude 2.1.294 and OpenCode 2.1.3 against a
local server. Both returned a valid description; checked the actual tool lists
(`StructuredOutput` / empty), two images, resolution and no source paths.
OpenCode returned text without step_finish and without usage; the adapter
handled it correctly. No model services were called and the database was not
changed. A regression script passed 6 tests of the adapters and a temporary
database (images/frames, schema, Claude typed quota, resume/skip/retry,
isolation, errors, usage, cleanup). The existing Codex timeout check also
passed. Formatting and lint passed. Real login and model availability still
require a trial with a user account.

Catalog verification 2026-10-25: a script checked
`opencode/deepseek-v4.1-flash-free` with the isolated adapter's exact
environment. An inotify monitor recorded no opens or reads of ~/.config/opencode
while the client and server ran. opencode.models.dev was added to the module
allowlist; without it the built-in model catalog was disabled too. No model was
called and the database was not changed. The OpenCode configuration uses the
available free DeepSeek without an unverified effort variant. A full adapter
test with a local provider also recorded no opens/reads of that directory; it
kept `tools: []`, the images and a valid description. With the model catalog
enabled, step_start/step_finish and native usage without an explicit total were
also obtained.

Free-tier error verification 2026-10-25: session metadata confirm a successful
native build response on DeepSeek V4.1 Flash Free / max after a failed adapter
request for the same model/effort. Three minimal attempts without images and
without database changes reproduced provider.free-limit: custom agent, build,
and build with native agent/identity modules, always with tools forbidden. This
matches upstream #51241/#54744; the error name alone does not prove a spent
quota. Tools and access to ~/.config/opencode were not enabled to work around
the refusal.

## codex-cli backend

Profiles: check `config.deepseek.toml`. Inherited model/effort must also be
visible to embed, and explicit settings and CLI overrides take precedence. Do
not import the user's other settings, especially permissions and helpers. A
token/header must not appear in argv or in a timeout message. A test profile
with shell_tool=true and danger-full-access must still run the adapter without
tools.

Profile verification 2026-10-25: a script passed 4 tests of provider selection,
overrides, rejecting helpers and passing secrets through the environment. A wire
test checked the real CLI, a local endpoint, authorization with a fake token,
model/effort, tools: [], image and schema. A `--limit 1 -v` trial with real
OpenRouter described and saved one image (5 seconds, 4898 tokens, effort high).
Later runs of this configuration should skip the saved variant. The
`codex.deepseek-models.json` catalog passed the Codex 0.164.0 parser and
advertises none/low/high/max in line with OpenRouter's public metadata. The
default instructions were captured locally from a previously unknown model so
as not to change them.

Tool restriction check: run the adapter with the real CLI and a local test
endpoint (fake login, separate `CODEX_HOME`). The request to `/v1/responses`
must contain `tools: []`, the image/frames and the schema. A synthetic response
must pass through the adapter into description JSON. Check the model from the
configuration and a model outside the catalog. Checking process arguments alone
is not enough.

- [ ] Set `backend = "codex-cli"`, an available vision model and a prompt in a
  separate TOML (`prompt_version = "vcodex1-master"`); `base_url` and `api_key`
  are not required. Use the test database.
- [ ] `model_instructions_file` points to a file with the exact text of the
  selected prompt. Stdin contains only the request for the meme and frame
  information; the fixed prompt is not repeated there. Changing
  `prompt_version` still creates a separate variant.
- [ ] The `-v` header shows `[N tokens]` as the input/output sum from
  `turn.completed`; cached input is not counted twice. Without usage the counter
  is omitted.
- [ ] `describe --limit 1 -v` saves the raw JSON; a second run skips it.
  `--model`, `--effort`, `--path` and `--retry-failed` work as before.
- [ ] Check PNG/JPEG and GIF/WebP/APNG animations, also a path with spaces.
  All selected frames reach the model in chronological order.
- [ ] A process error, account limit and missing final response stop the run
  without a record for the current image. Running again picks it up.
- [ ] A timeout records `failed_describe` with a timeout message and moves on to
  the next image. `--retry-failed` retries it.
- [ ] Invalid final JSON, repeated keywords, a wrong humor_type or a corrupted
  image give `failed_describe`; the remaining images are still processed.
- [ ] The first Ctrl+C finishes and saves the current description; the second
  aborts the process. A timeout also kills child processes. Temporary files are
  cleaned up.
- [ ] The Claude backend still reads `structured_output` and recognizes its usage
  limit by typed fields, despite the shared process-launching function.

Verified 2026-10-09: a script passed adapter tests with a simulated CLI response
(arguments, GIF/PNG, schema, preserving raw JSON, validation and cleanup), a
temporary database (error without a row, resume/skip, effort variant and invalid
JSON), Claude response/limit regressions and a real subprocess
(stdin/stdout/timeout). Process interruption was checked with a mock. No
description was made through the Codex service; login, availability of the
chosen model and the full network path require a `--limit 1` trial.

Instructions-file switch verification: the Codex adapter with a simulated
response received the exact text of `vcodex1-master.txt` in a UTF-8 file passed
via `model_instructions_file`. Checked a static image and an animation: stdin
contained only the task and frame timestamps, and the instructions file only
the prompt. Checked static image scaling, file cleanup and the other backends'
transports. Formatting and lint passed. The Codex service was not called.

## Shared animation frames

- [ ] Always the first frame (GIF and video, also after thinning to 12).
- [ ] Animation with cuts: one frame from the middle of each shot (`scene > 0.15`).
- [ ] No cuts: the middle and last frame; the same frame index only once.
- [ ] Variable frame durations in GIF/WebP/APNG and APNG with a separate static
  preview.
- [ ] Frames keep native resolution; static images are still scaled on the
  HTTP/Codex paths. Temporary files disappear after the request/error.
- [ ] Missing `ffmpeg` or an extraction error records an error for that image
  only; the next static image is still described.
- [ ] Video (mp4/webm): the scan computes the phash from the middle frame, a
  corrupted file is skipped, a repeated scan adds nothing. Description: cuts at
  `scene > 0.3`, frame selection as for GIFs, at most 12 frames, and the prompt
  notes that the soundtrack was not included.
- [ ] `[transcribe]`: speech reaches the prompt, music alone gives a "no speech
  recognised" note, a video without audio "no audio stream"; the transcript is
  saved once per hash and reused; an unavailable service stops the run without a
  row in `descriptions`; an old database gets `audio_transcripts`.
- [ ] GUI: a video preview shows the first frame with a "video" note; the `<` /
  `>` buttons (video and animations) step through the same frames that are sent
  to the model; Ctrl+C copies the file as `video/*`, Ctrl+O opens a player via
  `xdg-open`.

Verified 2026-11-12: scanning copies of mp4/webm/gif (including a path with a
space, an identical copy and a corrupted mp4), frame selection on 9 videos from
the collection, comparing frame selection with the previous version on 89
GIF/WebP files (no differences), describing one mp4 through `claude-code-cli`,
embedding and finding it. The GUI was checked only at the level of the
frame-extraction function, without a window. Transcription: the local
asr-whisper-service (`/inference`) on a video with speech, with music only and
without audio; migrating an old database; stopping with the service down;
describing two videos through `claude-code-cli` with a transcript.

Verified 2026-10-09: a script used real FFmpeg on GIF/WebP/APNG (cuts, no cuts,
variable durations, native resolution, APNG with a separate preview, paths with
spaces). The HTTP/Claude/Codex transports were checked with simulated model
responses and a temporary database, including order, timestamps, JSON saving,
cleanup and continuing without FFmpeg. Extraction also passed on three GIFs
from the real collection (20, 24 and 51 source frames). No paid model calls
were made and the database was not changed.
