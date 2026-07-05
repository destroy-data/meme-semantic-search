-- sqlite-vec extension must be loaded first.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memes (
    hash                    TEXT    PRIMARY KEY,           -- sha256 hex of file contents
    path                    TEXT    NOT NULL,              -- last known location on disk
    phash                   TEXT    NOT NULL,              -- perceptual hash (hex), used for regrouping
    perceptual_duplicate_of TEXT    REFERENCES memes(hash),-- phash group representative; NULL = representative
    description_json        TEXT,                          -- raw VLM output, verbatim JSON string
    prompt_version          INTEGER,                       -- which prompt produced description_json
    status                  TEXT    NOT NULL
        CHECK (status IN ('scanned', 'described', 'embedded', 'failed_describe', 'failed_embed'))
);

CREATE INDEX IF NOT EXISTS memes_status_idx ON memes(status);
CREATE INDEX IF NOT EXISTS memes_dup_idx    ON memes(perceptual_duplicate_of);

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
