#!/usr/bin/env python3
"""
YouTube Subscription Manager — local web UI
Opens http://localhost:5000 in your browser.

Setup:
  pip install google-auth-oauthlib google-api-python-client flask

Google Cloud setup (one-time):
  1. console.cloud.google.com -> new project
  2. APIs & Services > Library > enable "YouTube Data API v3"
  3. APIs & Services > Credentials > Create Credentials > OAuth client ID > Desktop app
  4. Download JSON -> save as client_secret.json next to this script
"""

import json
import pickle
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
except ImportError:
    print("Run:  pip install google-auth-oauthlib google-api-python-client flask")
    sys.exit(1)

try:
    from flask import Flask, jsonify, render_template_string, request as freq
except ImportError:
    print("Run:  pip install flask")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_FILE = Path(__file__).parent / "yt_token.pickle"
SECRET_FILE = Path(__file__).parent / "client_secret.json"
CACHE_FILE = Path(__file__).parent / "yt_cache.json"
CACHE_MAX_AGE_HOURS = 24

app = Flask(__name__)
_youtube = None   # set after auth
_subs = []        # list of enriched subscription dicts


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def save_cache(subs):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"cached_at": datetime.now(timezone.utc).isoformat(), "subs": subs}, f)


def load_cache():
    if not CACHE_FILE.exists():
        return None
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    cached_at = datetime.fromisoformat(data["cached_at"])
    age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
    if age_hours > CACHE_MAX_AGE_HOURS:
        return None
    return data["subs"]


# ---------------------------------------------------------------------------
# Auth & data fetching
# ---------------------------------------------------------------------------

def authenticate():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not SECRET_FILE.exists():
                print(f"ERROR: {SECRET_FILE} not found.")
                print("Save your OAuth client secret JSON as 'client_secret.json' next to this script.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(SECRET_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("youtube", "v3", credentials=creds)


def fetch_subscriptions(youtube):
    print("Fetching subscriptions...", end="", flush=True)
    subs = []
    req = youtube.subscriptions().list(
        part="snippet", mine=True, maxResults=50, order="alphabetical"
    )
    while req:
        resp = req.execute()
        for item in resp.get("items", []):
            subs.append({
                "sub_id": item["id"],
                "channel_id": item["snippet"]["resourceId"]["channelId"],
                "title": item["snippet"]["title"],
                "thumbnail": item["snippet"].get("thumbnails", {}).get("default", {}).get("url", ""),
            })
        req = youtube.subscriptions().list_next(req, resp)
        print(".", end="", flush=True)
    print(f" {len(subs)} found.")
    return subs


def enrich_subscriptions(youtube, subs):
    """Fetch channel stats (subscriber count, video count, last upload) in batches of 50."""
    print("Fetching channel details...", end="", flush=True)
    channel_map = {}
    ids = [s["channel_id"] for s in subs]
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        resp = youtube.channels().list(
            part="statistics,contentDetails,brandingSettings",
            id=",".join(batch),
            maxResults=50,
        ).execute()
        for item in resp.get("items", []):
            cid = item["id"]
            stats = item.get("statistics", {})
            uploads_playlist = (
                item.get("contentDetails", {})
                    .get("relatedPlaylists", {})
                    .get("uploads", "")
            )
            description = (
                item.get("brandingSettings", {})
                    .get("channel", {})
                    .get("description", "")
            )
            channel_map[cid] = {
                "subscriber_count": int(stats.get("subscriberCount", 0)),
                "video_count": int(stats.get("videoCount", 0)),
                "uploads_playlist": uploads_playlist,
                "description": description[:200] if description else "",
            }
        print(".", end="", flush=True)

    # Fetch last upload date for each channel via their uploads playlist
    playlists = [
        channel_map[s["channel_id"]]["uploads_playlist"]
        for s in subs
        if channel_map.get(s["channel_id"], {}).get("uploads_playlist")
    ]
    last_upload_map = {}
    for playlist_id in playlists:
        try:
            r = youtube.playlistItems().list(
                part="contentDetails", playlistId=playlist_id, maxResults=1
            ).execute()
            items = r.get("items", [])
            if items:
                raw = items[0]["contentDetails"].get("videoPublishedAt", "")
                if raw:
                    last_upload_map[playlist_id] = raw[:10]  # YYYY-MM-DD
        except Exception:
            pass
    print(" done.")

    now = datetime.now(timezone.utc)
    for s in subs:
        info = channel_map.get(s["channel_id"], {})
        s["subscriber_count"] = info.get("subscriber_count", 0)
        s["video_count"] = info.get("video_count", 0)
        s["description"] = info.get("description", "")

        playlist_id = info.get("uploads_playlist", "")
        last_upload_str = last_upload_map.get(playlist_id, "")
        s["last_upload"] = last_upload_str

        # Auto-tags
        tags = []
        if last_upload_str:
            last = datetime.fromisoformat(last_upload_str).replace(tzinfo=timezone.utc)
            days_ago = (now - last).days
            if days_ago > 365:
                tags.append("dead")
            elif days_ago > 180:
                tags.append("inactive")
        else:
            tags.append("no-uploads")

        sub_count = s["subscriber_count"]
        if sub_count == 0:
            tags.append("hidden-subs")
        elif sub_count < 1_000:
            tags.append("small")
        elif sub_count >= 1_000_000:
            tags.append("large")

        if s["video_count"] == 0:
            tags.append("no-videos")

        if not s["description"]:
            tags.append("no-description")

        s["tags"] = tags

    return subs


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT Subscription Manager</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #e0e0e0; overflow-x: auto; }

  header {
    position: sticky; top: 0; z-index: 100;
    background: #1a1a1a; border-bottom: 1px solid #333;
    padding: 12px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  header h1 { font-size: 1.1rem; color: #ff4444; white-space: nowrap; }
  #search { flex: 1; min-width: 200px; padding: 7px 12px; background: #2a2a2a; border: 1px solid #444; color: #e0e0e0; border-radius: 6px; font-size: 0.9rem; }
  #search:focus { outline: none; border-color: #ff4444; }

  .tag-filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .tag-filter-btn {
    padding: 4px 10px; border-radius: 12px; border: 1px solid transparent;
    font-size: 0.75rem; cursor: pointer; background: #2a2a2a; color: #aaa;
    transition: all .15s;
  }
  .tag-filter-btn.active { border-color: currentColor; }

  .actions { display: flex; gap: 8px; margin-left: auto; }
  button {
    padding: 7px 14px; border-radius: 6px; border: none; cursor: pointer;
    font-size: 0.85rem; font-weight: 600; transition: opacity .15s;
  }
  button:hover { opacity: .85; }
  #btn-select-all { background: #2a2a2a; color: #ccc; }
  #btn-deselect-all { background: #2a2a2a; color: #ccc; }
  #btn-unsub { background: #c0392b; color: #fff; }
  #btn-unsub:disabled { opacity: .4; cursor: default; }

  .counter { font-size: 0.85rem; color: #999; white-space: nowrap; }

  /* table */
  .table-wrap { }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #222; vertical-align: middle; }
  th {
    background: #161616; font-size: 0.78rem; text-transform: uppercase;
    letter-spacing: .05em; color: #666; position: sticky; top: var(--header-h, 60px);
    cursor: pointer; user-select: none; white-space: nowrap;
  }
  th:hover { color: #aaa; }
  th.sort-asc::after  { content: ' ▲'; color: #ff4444; }
  th.sort-desc::after { content: ' ▼'; color: #ff4444; }
  th.no-sort { cursor: default; }
  tr:hover td { background: #1c1c1c; }
  tr.checked td { background: #1f1010; }

  .channel-cell { display: flex; align-items: center; gap: 10px; }
  .thumb { width: 36px; height: 36px; border-radius: 50%; object-fit: cover; background: #333; flex-shrink: 0; }
  .channel-name a { font-size: 0.9rem; font-weight: 500; color: inherit; text-decoration: none; }
  .channel-name a:hover { text-decoration: underline; color: #ff6666; }
  .channel-desc { font-size: 0.75rem; color: #777; margin-top: 2px; max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .num { font-size: 0.85rem; color: #bbb; }

  .tags { display: flex; gap: 4px; flex-wrap: wrap; }
  .tag {
    padding: 2px 8px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; white-space: nowrap;
  }
  .tag-dead         { background: #5c1010; color: #ff6b6b; }
  .tag-inactive     { background: #4a2e00; color: #ffaa44; }
  .tag-no-uploads   { background: #2a2a2a; color: #888; }
  .tag-no-videos    { background: #2a2a2a; color: #888; }
  .tag-small        { background: #1a2a1a; color: #66bb66; }
  .tag-large        { background: #1a1a3a; color: #7777ff; }
  .tag-hidden-subs  { background: #2a2a2a; color: #888; }
  .tag-no-description { background: #2a2a2a; color: #666; }

  input[type=checkbox] { width: 16px; height: 16px; accent-color: #ff4444; cursor: pointer; }

  #toast {
    position: fixed; bottom: 24px; right: 24px; background: #222; color: #eee;
    padding: 12px 20px; border-radius: 8px; font-size: 0.85rem;
    border-left: 4px solid #ff4444; display: none; z-index: 999;
    box-shadow: 0 4px 20px rgba(0,0,0,.5);
  }

  #progress-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7);
    z-index: 200; align-items: center; justify-content: center; flex-direction: column; gap: 16px;
  }
  #progress-overlay.show { display: flex; }
  #progress-box { background: #1a1a1a; border: 1px solid #333; border-radius: 12px; padding: 32px 40px; min-width: 320px; text-align: center; }
  #progress-title { font-size: 1rem; margin-bottom: 12px; color: #eee; }
  #progress-bar-wrap { background: #2a2a2a; border-radius: 6px; overflow: hidden; height: 8px; }
  #progress-bar { background: #ff4444; height: 100%; width: 0; transition: width .3s; }
  #progress-text { font-size: 0.8rem; color: #999; margin-top: 10px; }

  .no-results { padding: 40px; text-align: center; color: #555; }
</style>
</head>
<body>

<header>
  <h1>YT Unsub Manager</h1>
  <input id="search" type="text" placeholder="Search channels..." oninput="applyFilters()">
  <div class="tag-filters" id="tag-filters"></div>
  <div class="actions">
    <span class="counter" id="counter">0 selected</span>
    <button id="btn-refresh" onclick="refreshData()" title="Re-fetch from YouTube API">↻ Refresh</button>
    <button id="btn-select-all" onclick="selectVisible(true)">Select visible</button>
    <button id="btn-deselect-all" onclick="selectVisible(false)">Deselect visible</button>
    <button id="btn-unsub" disabled onclick="confirmUnsub()">Unsubscribe selected</button>
  </div>
</header>

<div class="table-wrap">
<table id="sub-table">
  <thead>
    <tr>
      <th class="no-sort" style="width:36px"></th>
      <th onclick="setSort('title')">Channel</th>
      <th onclick="setSort('subscriber_count')">Subscribers</th>
      <th onclick="setSort('video_count')">Videos</th>
      <th onclick="setSort('last_upload')">Last upload</th>
      <th class="no-sort">Tags</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
<div class="no-results" id="no-results" style="display:none">No channels match the current filter.</div>
</div>

<div id="toast"></div>

<div id="progress-overlay">
  <div id="progress-box">
    <div id="progress-title">Unsubscribing…</div>
    <div id="progress-bar-wrap"><div id="progress-bar"></div></div>
    <div id="progress-text"></div>
  </div>
</div>

<script>
let allSubs = [];
let checked = new Set();
let sortKey = 'title';
let sortAsc = true;
let activeTagFilter = null;

const TAG_COLORS = {
  'dead': 'tag-dead', 'inactive': 'tag-inactive', 'no-uploads': 'tag-no-uploads',
  'no-videos': 'tag-no-videos', 'small': 'tag-small', 'large': 'tag-large',
  'hidden-subs': 'tag-hidden-subs', 'no-description': 'tag-no-description',
};

// Map column header onclick key -> th index (0-based)
const SORT_COL = { title: 1, subscriber_count: 2, video_count: 3, last_upload: 4 };

function updateHeaderHeight() {
  const h = document.querySelector('header').offsetHeight;
  document.documentElement.style.setProperty('--header-h', h + 'px');
}

async function init() {
  updateHeaderHeight();
  window.addEventListener('resize', updateHeaderHeight);
  const resp = await fetch('/api/subs');
  allSubs = await resp.json();
  buildTagFilterButtons();
  updateSortHeaders();
  render();
}

async function refreshData() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true;
  btn.textContent = '↻ Refreshing…';
  showToast('Re-fetching from YouTube API, please wait…', 60000);
  const resp = await fetch('/api/refresh', { method: 'POST' });
  allSubs = await resp.json();
  checked.clear();
  buildTagFilterButtons();
  render();
  btn.disabled = false;
  btn.textContent = '↻ Refresh';
  showToast(`Refreshed — ${allSubs.length} subscriptions loaded.`);
}

function buildTagFilterButtons() {
  const tagCounts = {};
  for (const s of allSubs) for (const t of s.tags) tagCounts[t] = (tagCounts[t] || 0) + 1;
  const wrap = document.getElementById('tag-filters');
  wrap.innerHTML = '';
  for (const [tag, count] of Object.entries(tagCounts).sort()) {
    const btn = document.createElement('button');
    btn.className = `tag-filter-btn tag ${TAG_COLORS[tag] || ''}`;
    btn.textContent = `${tag} (${count})`;
    btn.onclick = () => { activeTagFilter = activeTagFilter === tag ? null : tag; buildTagFilterButtons(); render(); };
    if (activeTagFilter === tag) btn.classList.add('active');
    wrap.appendChild(btn);
  }
  // Recalculate after buttons are painted (they may wrap and change header height)
  requestAnimationFrame(updateHeaderHeight);
}

function updateSortHeaders() {
  const ths = document.querySelectorAll('#sub-table thead th');
  ths.forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
  const idx = SORT_COL[sortKey];
  if (idx !== undefined) {
    ths[idx].classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
  }
}

function visible() {
  const q = document.getElementById('search').value.toLowerCase();
  return allSubs.filter(s =>
    (!q || s.title.toLowerCase().includes(q)) &&
    (!activeTagFilter || s.tags.includes(activeTagFilter))
  );
}

function fmtNum(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return n.toString();
}

function render() {
  let rows = visible();
  rows.sort((a, b) => {
    let va = a[sortKey] ?? '', vb = b[sortKey] ?? '';
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return sortAsc ? -1 : 1;
    if (va > vb) return sortAsc ? 1 : -1;
    return 0;
  });

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';

  for (const s of rows) {
    const isChecked = checked.has(s.sub_id);
    const tr = document.createElement('tr');
    if (isChecked) tr.classList.add('checked');
    tr.innerHTML = `
      <td><input type="checkbox" data-id="${s.sub_id}" ${isChecked ? 'checked' : ''} onchange="toggle('${s.sub_id}', this)"></td>
      <td>
        <div class="channel-cell">
          <img class="thumb" src="${s.thumbnail}" onerror="this.style.display='none'" loading="lazy">
          <div>
            <div class="channel-name"><a href="https://www.youtube.com/channel/${s.channel_id}" target="_blank" rel="noopener">${escHtml(s.title)}</a></div>
            ${s.description ? `<div class="channel-desc" title="${escHtml(s.description)}">${escHtml(s.description)}</div>` : ''}
          </div>
        </div>
      </td>
      <td class="num">${s.subscriber_count ? fmtNum(s.subscriber_count) : '—'}</td>
      <td class="num">${s.video_count ?? '—'}</td>
      <td class="num">${s.last_upload || '—'}</td>
      <td><div class="tags">${s.tags.map(t => `<span class="tag ${TAG_COLORS[t] || ''}">${t}</span>`).join('')}</div></td>
    `;
    tbody.appendChild(tr);
  }

  document.getElementById('no-results').style.display = rows.length ? 'none' : 'block';
  updateCounter();
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toggle(subId, el) {
  if (el.checked) checked.add(subId); else checked.delete(subId);
  el.closest('tr').classList.toggle('checked', el.checked);
  updateCounter();
}

function selectVisible(val) {
  for (const s of visible()) { if (val) checked.add(s.sub_id); else checked.delete(s.sub_id); }
  render();
}

function updateCounter() {
  document.getElementById('counter').textContent = `${checked.size} selected`;
  document.getElementById('btn-unsub').disabled = checked.size === 0;
}

function setSort(key) {
  sortAsc = (sortKey === key) ? !sortAsc : key === 'title';
  sortKey = key;
  updateSortHeaders();
  render();
}

function applyFilters() { render(); }

function showToast(msg, duration = 3500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.style.display = 'none', duration);
}

async function confirmUnsub() {
  const ids = [...checked];
  const names = allSubs.filter(s => ids.includes(s.sub_id)).map(s => s.title);
  const preview = names.slice(0, 5).join('\n') + (names.length > 5 ? `\n…and ${names.length - 5} more` : '');
  if (!confirm(`Unsubscribe from ${ids.length} channel(s)?\n\n${preview}\n\nThis cannot be undone.`)) return;

  const overlay = document.getElementById('progress-overlay');
  const bar = document.getElementById('progress-bar');
  const txt = document.getElementById('progress-text');
  overlay.classList.add('show');
  bar.style.width = '0';

  let done = 0, errors = 0;
  for (const sub_id of ids) {
    const name = allSubs.find(s => s.sub_id === sub_id)?.title ?? sub_id;
    txt.textContent = `Removing ${name}…`;
    const r = await fetch('/api/unsub', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({sub_id}) });
    if (r.ok) { done++; checked.delete(sub_id); allSubs = allSubs.filter(s => s.sub_id !== sub_id); }
    else errors++;
    bar.style.width = `${Math.round(((done + errors) / ids.length) * 100)}%`;
  }

  overlay.classList.remove('show');
  buildTagFilterButtons();
  render();
  showToast(`Done: ${done} removed${errors ? ', ' + errors + ' errors' : ''}.`, 5000);
}

init();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/subs")
def api_subs():
    return jsonify(_subs)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _subs
    raw = fetch_subscriptions(_youtube)
    _subs = enrich_subscriptions(_youtube, raw)
    save_cache(_subs)
    return jsonify(_subs)


@app.route("/api/unsub", methods=["POST"])
def api_unsub():
    data = freq.get_json()
    sub_id = data.get("sub_id")
    if not sub_id:
        return jsonify({"error": "missing sub_id"}), 400
    try:
        _youtube.subscriptions().delete(id=sub_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _youtube, _subs

    _youtube = authenticate()

    cached = load_cache()
    if cached:
        print(f"Loaded {len(cached)} subscriptions from cache (use ↻ Refresh in the UI to re-fetch).")
        _subs = cached
    else:
        raw = fetch_subscriptions(_youtube)
        _subs = enrich_subscriptions(_youtube, raw)
        save_cache(_subs)

    url = "http://localhost:5000"
    print(f"\nOpening {url} ...")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=5000, debug=False)


if __name__ == "__main__":
    main()
