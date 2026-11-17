# feed-filter

Filtered copies of RSS and Atom feeds, rebuilt every 3 hours by GitHub Actions
and published on GitHub Pages.

Feeds: https://gkaramanis.github.io/feed-filter/ (index, plus `feeds.opml`).

## Add a feed

Add an entry to `feeds.yaml` and push:

```yaml
- name: some-blog-no-sponsored
  url: https://example.com/feed.xml
  drop: 'sponsored'
  field: category      # title (default), description, category or link
```

`keep` keeps only matching items; `drop` removes them. Matching ignores case.
`limit: N` keeps the first N matches. `fulltext: true` fetches each kept
article and puts its text in the feed; text already published is reused, so
only new posts are fetched. See `feeds.yaml` for all options.
The new feed appears at `https://gkaramanis.github.io/feed-filter/<name>.xml`.

## Behaviour

- Parses leniently, so feeds with small XML errors still work.
- Leaves everything except the removed items untouched.
- If a source fails, the last published copy stays up and the run is marked
  failed (GitHub emails you).

## Run locally

```sh
uv run --with-requirements requirements.txt filter.py
```

Output goes to `public/`, which is not committed.
