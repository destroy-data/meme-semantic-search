-- sqlite-vec extension must be loaded first.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memes (
    hash                    TEXT    PRIMARY KEY,           -- sha256 hex of file contents
    path                    TEXT    NOT NULL,              -- active primary path; empty when archived
    phash                   TEXT    NOT NULL,              -- perceptual hash (hex), used for regrouping
    perceptual_duplicate_of TEXT    REFERENCES memes(hash) -- phash group representative; NULL = representative
);

CREATE INDEX IF NOT EXISTS memes_dup_idx ON memes(perceptual_duplicate_of);

-- Physical copies share metadata, descriptions and embeddings by content hash.
CREATE TABLE IF NOT EXISTS meme_files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL REFERENCES memes(hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS meme_files_hash_idx ON meme_files(hash);

-- One row per describe attempt. A meme can have multiple rows (different models,
-- prompt versions, or reasoning_effort settings). Uniqueness
-- is on the full config tuple; NULL effort_level counts as its own value via the
-- IFNULL expression index so re-runs with the same (model, prompt, no-effort)
-- combo are still deduplicated.
CREATE TABLE IF NOT EXISTS descriptions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hash             TEXT    NOT NULL REFERENCES memes(hash),
    description_json TEXT,                       -- raw VLM output; NULL when status='failed_describe'
    prompt_version   TEXT    NOT NULL,           -- matches prompts/<name>.txt
    model            TEXT    NOT NULL,           -- VLM model that produced this row
    effort_level     TEXT,                       -- reasoning_effort passed to the API; NULL = not sent
    total_tokens     INTEGER,                    -- reported input + output usage; NULL = unknown
    usage_json       TEXT,                       -- backend's full usage breakdown; NULL = unknown
    status           TEXT    NOT NULL
        CHECK (status IN ('described', 'failed_describe')),
    error            TEXT,                       -- error message when status='failed_describe'
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS descriptions_hash_idx ON descriptions(hash);
CREATE UNIQUE INDEX IF NOT EXISTS descriptions_config_idx
    ON descriptions(hash, model, prompt_version, IFNULL(effort_level, ''));

CREATE VIRTUAL TABLE IF NOT EXISTS memes_fts USING fts5(
    hash UNINDEXED,
    transcription,
    keywords,
    template,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE VIRTUAL TABLE IF NOT EXISTS memes_vec USING vec0(
    hash TEXT PRIMARY KEY,
    embedding float[1024]
);
