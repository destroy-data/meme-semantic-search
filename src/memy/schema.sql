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

-- One row per description indexed for a meme; a meme can have many (one per
-- description variant). The description's config tuple is copied here so the
-- provenance survives a deleted meme handing its index to a group survivor
-- (then hash != source_hash and description_id no longer exists). An own entry
-- whose description_id is gone (describe replaced the row) is stale: search
-- ignores it and the next embed run deletes it.
CREATE TABLE IF NOT EXISTS index_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- = entries_fts rowid
    hash           TEXT    NOT NULL REFERENCES memes(hash),  -- meme it indexes
    source_hash    TEXT    NOT NULL,           -- meme the description belongs to
    description_id INTEGER NOT NULL,           -- descriptions.id it was built from
    model          TEXT    NOT NULL,           -- description model
    prompt_version TEXT    NOT NULL,
    effort_level   TEXT,
    text_version   TEXT    NOT NULL,           -- embed.TEXT_VERSION: text composition
    text_sha256    TEXT    NOT NULL,           -- of the embedded text
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS index_entries_source_idx
    ON index_entries(hash, description_id);

-- FTS over each entry's description; rowid = index_entries.id.
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    transcription,
    keywords,
    template,
    tokenize = "unicode61 remove_diacritics 2"
);

-- One L2-normalized float32 vector per (entry, embedding model). Models may
-- differ in dimensions; search compares within one model only.
CREATE TABLE IF NOT EXISTS embeddings (
    entry_id    INTEGER NOT NULL REFERENCES index_entries(id) ON DELETE CASCADE,
    embed_model TEXT    NOT NULL,
    dims        INTEGER NOT NULL,
    embedding   BLOB    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entry_id, embed_model)
);
CREATE INDEX IF NOT EXISTS embeddings_model_idx ON embeddings(embed_model);

CREATE TRIGGER IF NOT EXISTS index_entries_delete AFTER DELETE ON index_entries
BEGIN
    DELETE FROM entries_fts WHERE rowid = old.id;
END;

-- Speech in a video's soundtrack, transcribed once per content hash at the
-- start of describe and given to the describing model with the frames. Shared
-- by every description config. has_audio = 0: the video has no audio stream;
-- text = '': audio without recognised speech (music, silence).
CREATE TABLE IF NOT EXISTS audio_transcripts (
    hash       TEXT    PRIMARY KEY REFERENCES memes(hash) ON DELETE CASCADE,
    has_audio  INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    language   TEXT,                             -- as reported by the ASR service
    model      TEXT,                             -- [transcribe].model; NULL = not sent
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
