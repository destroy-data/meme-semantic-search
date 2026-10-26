# TODO

## Support multiple embeddings per meme

The current index stores one vector per meme hash. Embedding skips hashes that
already have a vector, so generating a new description does not refresh either
its vector or its FTS entry. This prevents reliable comparisons between prompt
variants in the same database.

1. Support at least two independently selectable embedding variants for the same
   meme, retaining both without overwriting either. Allow further variants
   without another fixed-slot schema change.
2. Track each variant's source description and embedding configuration, including
   embedding model, dimensions, and text-composition version. Preserve enough
   provenance to distinguish prompt, description model, and effort variants.
3. Make embedding resumable per meme and variant. Detect changed source content
   or configuration so an existing vector cannot silently stand in for a new
   description. Preserve successful work when another variant fails.
4. Let search select the intended variant explicitly. Keep FTS content aligned
   with that variant's source description when comparing hybrid search results.
   Decide separately whether searching across variants is useful.
5. Migrate existing embeddings without losing descriptions or silently assigning
   provenance that cannot be established. Define CLI and GUI selection behavior.
6. Verify that two variants coexist, repeat runs skip only completed matching
   work, source changes are detected, and each search uses the selected vector
   and corresponding FTS content.

Storage layout and selection UI remain implementation decisions. This is planned
work, not a claim that multiple embeddings are currently supported.
