# yt-unsub — Project Context for Claude

## What this is

A single-file Python app (`yt_unsub.py`) that serves a local Flask web UI for bulk-managing YouTube subscriptions. The user authenticates via OAuth2, the script fetches all subscribed channels, enriches them with stats from the YouTube Data API, and serves a browser UI at `localhost:5000` for reviewing and unsubscribing.

## Architecture

- **Single file**: all backend (Flask routes, API calls) and frontend (HTML/CSS/JS embedded as a string) live in `yt_unsub.py`
- **Auth**: OAuth2 via `google-auth-oauthlib`, token cached in `yt_token.pickle`
- **Data flow**: fetch subscriptions → batch-enrich with channel stats → auto-tag → serve as JSON via `/api/subs`
- **Cache**: enriched data saved to `yt_cache.json` (24 h TTL) so repeat startups are instant
- **Unsubscribe**: `/api/unsub` POST calls `youtube.subscriptions().delete()` per channel; done one at a time with a progress bar in the UI

## Key files

| File | Notes |
|------|-------|
| `yt_unsub.py` | Everything — do not split unless the user asks |
| `client_secret.json` | OAuth credentials — gitignored, user must provide |
| `yt_token.pickle` | Cached auth token — gitignored |
| `yt_cache.json` | Cached subscription data — gitignored |

## Frontend notes

- Dark theme, YouTube red (`#ff4444`) accent
- Sticky header uses `--header-h` CSS variable set by JS (`updateHeaderHeight()`) — must be called after tag filter buttons are injected via `requestAnimationFrame`, otherwise the `<thead>` overlaps the first row
- `overflow-x: auto` must be on `body`, NOT on `.table-wrap` — putting it on the wrapper breaks `position: sticky` on `<th>` elements
- Sort state: `sortKey` + `sortAsc` globals; column headers have `onclick="setSort('key')"` and get `.sort-asc` / `.sort-desc` classes for arrow indicators

## Auto-tags generated

| Tag | Condition |
|-----|-----------|
| `dead` | No upload in 365+ days |
| `inactive` | No upload in 180–365 days |
| `no-uploads` | No upload date found |
| `no-videos` | `video_count == 0` |
| `small` | < 1,000 subscribers |
| `large` | ≥ 1,000,000 subscribers |
| `hidden-subs` | `subscriber_count == 0` (hidden by channel) |
| `no-description` | Empty channel description |

## API quota considerations

- Enriching channels makes one `channels.list` call per 50 channels (cheap)
- Last-upload fetch makes one `playlistItems.list` call **per channel** (expensive for large subscription lists) — cache mitigates this
- Deletes cost 50 quota units each

## Dependencies

```
google-auth-oauthlib
google-api-python-client
flask
```

## GitHub

https://github.com/mikotorz/yt-unsub
