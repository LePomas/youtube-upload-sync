# YouTube Upload CLI

Small Python CLI for syncing local video files to a YouTube account with OAuth.
It is designed for camera-card or GoPro-style folders where video filenames are
stable and can be used as YouTube titles.

## Features

- Upload local videos to YouTube with resumable uploads.
- Keep a local state file so repeated runs skip completed videos.
- Check existing videos in your YouTube account and skip matching titles.
- Default uploads to private.
- Track YouTube upload-limit waits based on the latest account upload time.
- Support dry runs, upload limits, custom titles, tags, categories, and playlists.

## Install

This project uses `uv`.

```bash
uv sync
```

You can also install the runtime dependencies with pip:

```bash
python3 -m pip install -r requirements.txt
```

## First-Time Google Setup

You do not need a separate YouTube API key for uploads. You need an OAuth client
JSON file from Google Cloud.

1. Create or select a Google Cloud project.
2. Enable the YouTube Data API v3.
3. Configure the OAuth consent screen.
4. Create an OAuth client for a desktop app.
5. Download the client JSON and save it as:

```text
secrets.json
```

Place `secrets.json` in the directory where you run the command, or pass a
custom path with `--secrets`.

If your OAuth app is in testing mode, add your Google account as a test user in
the Google Cloud OAuth consent screen.

## Usage

Preview pending uploads without authenticating or uploading:

```bash
uv run youtube-upload-sync --dry-run --limit 10 /path/to/videos
```

Upload five missing videos as private:

```bash
uv run youtube-upload-sync --check-existing-account --privacy private --limit 5 /path/to/videos
```

Interactive terminal runs show Rich progress bars automatically. For script logs,
the CLI falls back to plain output; use `--progress rich` or `--progress plain`
to force a mode.

Upload all missing videos as unlisted:

```bash
uv run youtube-upload-sync --check-existing-account --privacy unlisted /path/to/videos
```

Use a title template:

```bash
uv run youtube-upload-sync --title-template "{date} {stem}" /path/to/videos
```

Add uploads to a playlist:

```bash
uv run youtube-upload-sync --playlist-id YOUR_PLAYLIST_ID /path/to/videos
```

Check whether a saved upload-limit wait is still active:

```bash
uv run youtube-upload-sync --upload-limit-status
```

The first real upload opens a browser for Google OAuth. The resulting user token
is stored in `token.json`.

## Local Files

The CLI creates local files next to where you run it:

- `token.json`: cached OAuth user token.
- `.youtube-upload-state.json`: uploaded and skipped video state.

These files are ignored by git and should not be committed.

## Duplicate Detection

`--check-existing-account` lists videos from your account's uploads playlist and
matches them by generated title. This works well when your YouTube titles remain
the same as local filenames, such as `GH010084` or `GX010086`.

YouTube's API does not expose an original local file hash, so this is not a
byte-for-byte duplicate check.

## Upload Limits

When YouTube returns `uploadLimitExceeded`, the CLI refreshes your account
uploads, finds the latest upload timestamp, and estimates the next retry time as
24 hours after that latest upload. The failed video is not marked as uploaded.

If the saved wait is still active during an interactive run, the CLI asks if you
want it to wait until the retry time and continue automatically. In
non-interactive runs, it prints the retry time and exits instead of waiting.

For non-interactive scripts that should wait and continue automatically, use:

```bash
uv run youtube-upload-sync --wait-until-upload-limit-reset --check-existing-account --limit 5 /path/to/videos
```

If you want to try anyway:

```bash
uv run youtube-upload-sync --ignore-upload-limit-wait --check-existing-account --limit 5 /path/to/videos
```

## Development

Run tests:

```bash
uv run python -m unittest discover -v
```

Run syntax checks:

```bash
uv run python -m py_compile sync_youtube.py tests/test_sync_youtube.py
```
