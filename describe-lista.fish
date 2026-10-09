#!/usr/bin/env fish

cd (status dirname); or exit 1

while read -l meme_path
    test -n "$meme_path"; or continue

    for model in gpt-6.1-sol gpt-6-astra
        for effort in low high
            echo "$model / $effort: $meme_path"
            uv run memes.py describe --config config.codex.toml \
                --model "$model" --effort "$effort" -v --path "$meme_path"
            or exit $status
        end
    end
end < lista
