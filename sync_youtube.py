#!/usr/bin/env python3
"""Upload local GoPro videos to the authenticated YouTube account.

The script uses a Google OAuth client secrets file, stores the resulting user
token locally, and keeps a state file so repeated runs only upload new videos.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import pathlib
import sys
import time
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
DEFAULT_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".avi", ".mkv")


class UploadLimitExceeded(RuntimeError):
    """Raised when YouTube refuses more uploads for the authenticated account."""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync local videos to the authenticated YouTube account."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--secrets",
        default="secrets.json",
        help="OAuth client secrets JSON from Google Cloud. Default: secrets.json",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="OAuth user token cache. Default: token.json",
    )
    parser.add_argument(
        "--state",
        default=".youtube-upload-state.json",
        help="Local upload state file. Default: .youtube-upload-state.json",
    )
    parser.add_argument(
        "--privacy",
        choices=("private", "unlisted", "public"),
        default="private",
        help="YouTube privacy status for uploaded videos. Default: private",
    )
    parser.add_argument(
        "--title-template",
        default="{stem}",
        help=(
            "Title template. Available fields: path, name, stem, mtime, date, "
            "datetime, size_mb. Default: {stem}"
        ),
    )
    parser.add_argument(
        "--description",
        default="Uploaded from local GoPro sync script.",
        help="Description text for every uploaded video.",
    )
    parser.add_argument(
        "--tags",
        default="gopro",
        help="Comma-separated tags. Use an empty string for no tags. Default: gopro",
    )
    parser.add_argument(
        "--category-id",
        default="22",
        help="YouTube category ID. Default: 22 (People & Blogs).",
    )
    parser.add_argument(
        "--playlist-id",
        help="Optional playlist ID to add every uploaded video to.",
    )
    parser.add_argument(
        "--extensions",
        default=",".join(DEFAULT_VIDEO_EXTENSIONS),
        help="Comma-separated video extensions to include. Default: mp4,mov,m4v,avi,mkv",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Upload at most this many pending videos in this run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pending uploads without authenticating or uploading.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upload files even when they are already present in the local state.",
    )
    parser.add_argument(
        "--check-existing-account",
        action="store_true",
        help=(
            "Before uploading, list videos already in your YouTube account and "
            "skip local files whose generated title already exists."
        ),
    )
    parser.add_argument(
        "--upload-limit-status",
        action="store_true",
        help=(
            "Refresh the latest account upload time, print the upload-limit wait "
            "estimate, and exit."
        ),
    )
    parser.add_argument(
        "--ignore-upload-limit-wait",
        action="store_true",
        help="Try uploading even if the saved 24-hour upload-limit wait has not passed.",
    )
    parser.add_argument(
        "--wait-until-upload-limit-reset",
        action="store_true",
        help=(
            "When an upload-limit wait is active, sleep until the retry time and "
            "continue without prompting. Useful for non-interactive runs."
        ),
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into directories.",
    )
    return parser.parse_args()


def load_state(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    with path.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    if "uploaded" not in state or not isinstance(state["uploaded"], dict):
        raise ValueError(f"Invalid state file: {path}")
    return state


def empty_state() -> dict[str, Any]:
    return {"uploaded": {}}


def save_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def file_key(path: pathlib.Path) -> str:
    return str(path.resolve())


def video_metadata(path: pathlib.Path) -> dict[str, str]:
    stat = path.stat()
    modified = dt.datetime.fromtimestamp(stat.st_mtime).astimezone()
    return {
        "path": str(path),
        "name": path.name,
        "stem": path.stem,
        "mtime": str(int(stat.st_mtime)),
        "date": modified.strftime("%Y-%m-%d"),
        "datetime": modified.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "size_mb": f"{stat.st_size / 1024 / 1024:.1f}",
    }


def discover_videos(
    paths: list[str], extensions: set[str], recursive: bool
) -> list[pathlib.Path]:
    videos: list[pathlib.Path] = []
    for raw_path in paths:
        path = pathlib.Path(raw_path).expanduser()
        if path.is_file() and path.suffix.lower() in extensions:
            videos.append(path)
        elif path.is_dir():
            pattern = "**/*" if recursive else "*"
            videos.extend(
                candidate
                for candidate in path.glob(pattern)
                if candidate.is_file() and candidate.suffix.lower() in extensions
            )
        else:
            print(f"warning: skipping missing path: {path}", file=sys.stderr)

    return sorted(videos, key=lambda item: (item.stat().st_mtime, item.name))


def get_youtube_client(secrets_path: pathlib.Path, token_path: pathlib.Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not credentials.has_scopes(SCOPES):
            print(
                "OAuth token is missing the read/upload scopes; re-authentication is required.",
                file=sys.stderr,
            )
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not secrets_path.exists():
            raise FileNotFoundError(f"OAuth secrets file not found: {secrets_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        credentials = flow.run_local_server(port=0)

    with token_path.open("w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())

    return build("youtube", "v3", credentials=credentials)


def is_upload_limit_error(error: Exception) -> bool:
    return "uploadLimitExceeded" in str(error)


def parse_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def record_upload_limit(
    state: dict[str, Any],
    now: dt.datetime | None = None,
    last_account_upload_at: dt.datetime | None = None,
) -> dict[str, str]:
    hit_at = now or utcnow()
    basis_time = last_account_upload_at or hit_at
    retry_after = basis_time + dt.timedelta(hours=24)
    upload_limit = {
        "hit_at": hit_at.isoformat(),
        "retry_after": retry_after.isoformat(),
        "basis": (
            "24 hours after the latest account upload"
            if last_account_upload_at
            else "24 hours after the upload-limit error"
        ),
    }
    if last_account_upload_at:
        upload_limit["last_account_upload_at"] = last_account_upload_at.isoformat()
    state["upload_limit"] = upload_limit
    return upload_limit


def latest_account_upload_at(
    account_videos: dict[str, list[dict[str, str]]]
) -> dt.datetime | None:
    latest = None
    for matches in account_videos.values():
        for video in matches:
            published_at = video.get("published_at")
            if not published_at:
                continue
            parsed = parse_datetime(published_at)
            if latest is None or parsed > latest:
                latest = parsed
    return latest


def refresh_upload_limit_from_account(
    state: dict[str, Any],
    account_videos: dict[str, list[dict[str, str]]],
    now: dt.datetime | None = None,
) -> dict[str, str] | None:
    last_upload_at = latest_account_upload_at(account_videos)
    if not last_upload_at:
        return None

    current_time = now or utcnow()
    retry_after = last_upload_at + dt.timedelta(hours=24)
    if current_time >= retry_after:
        state.pop("upload_limit", None)
        return None

    upload_limit = {
        "hit_at": current_time.isoformat(),
        "last_account_upload_at": last_upload_at.isoformat(),
        "retry_after": retry_after.isoformat(),
        "basis": "24 hours after the latest account upload",
    }
    state["upload_limit"] = upload_limit
    return upload_limit


def get_active_upload_limit(
    state: dict[str, Any], now: dt.datetime | None = None
) -> dict[str, Any] | None:
    upload_limit = state.get("upload_limit")
    if not isinstance(upload_limit, dict) or "retry_after" not in upload_limit:
        return None

    current_time = now or utcnow()
    retry_after = parse_datetime(upload_limit["retry_after"])
    if current_time >= retry_after:
        return None

    return {
        "hit_at": parse_datetime(upload_limit["hit_at"]),
        "retry_after": retry_after,
        "last_account_upload_at": (
            parse_datetime(upload_limit["last_account_upload_at"])
            if upload_limit.get("last_account_upload_at")
            else None
        ),
        "remaining_seconds": (retry_after - current_time).total_seconds(),
        "basis": upload_limit.get("basis", ""),
    }


def print_upload_limit_status(state: dict[str, Any]) -> bool:
    active_limit = get_active_upload_limit(state)
    if not active_limit:
        print("no active upload-limit wait saved")
        return False

    retry_after = active_limit["retry_after"].astimezone()
    remaining = format_duration(active_limit["remaining_seconds"])
    print(f"upload limit wait: about {remaining} remaining")
    print(f"try again after: {retry_after.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if active_limit["last_account_upload_at"]:
        last_upload = active_limit["last_account_upload_at"].astimezone()
        print(f"latest account upload: {last_upload.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    return True


def print_upload_limit_summary(active_limit: dict[str, Any]) -> None:
    retry_after = active_limit["retry_after"].astimezone()
    remaining = format_duration(active_limit["remaining_seconds"])
    print(f"Upload limit active: about {remaining} remaining.")
    print(f"Retry after: {retry_after.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("To try now anyway, rerun with --ignore-upload-limit-wait.")


def prompt_auto_retry() -> bool:
    if not sys.stdin.isatty():
        return False

    answer = input("Wait until retry time and continue automatically? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def wait_until_retry_time(active_limit: dict[str, Any]) -> None:
    remaining_seconds = max(0, active_limit["remaining_seconds"])
    retry_after = active_limit["retry_after"].astimezone()
    print(f"Waiting until {retry_after.strftime('%Y-%m-%d %H:%M:%S %Z')}...")
    if remaining_seconds:
        time.sleep(remaining_seconds)


def refresh_and_print_upload_limit_status(youtube, state: dict[str, Any]) -> bool:
    account_videos = list_account_videos_by_title(youtube)
    upload_limit = refresh_upload_limit_from_account(state, account_videos)
    if upload_limit:
        return print_upload_limit_status(state)

    last_upload_at = latest_account_upload_at(account_videos)
    if last_upload_at:
        retry_after = last_upload_at + dt.timedelta(hours=24)
        print("no active upload-limit wait")
        print(
            "latest account upload: "
            f"{last_upload_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        print(
            "24-hour window ended: "
            f"{retry_after.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
    else:
        print("no account uploads found")
    return False


def upload_video(
    youtube,
    path: pathlib.Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    privacy: str,
) -> str:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy},
    }
    mimetype = mimetypes.guess_type(path.name)[0] or "video/mp4"
    media = MediaFileUpload(str(path), mimetype=mimetype, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as error:
            if is_upload_limit_error(error):
                raise UploadLimitExceeded(
                    "YouTube says this account has exceeded the number of videos "
                    "it may upload right now. Already completed uploads and "
                    "account-matched skips remain saved; rerun the same command "
                    "later to continue."
                ) from error
            if error.resp.status in {500, 502, 503, 504}:
                print(f"temporary YouTube error {error.resp.status}; retrying...")
                time.sleep(5)
                continue
            raise
        if status:
            print(f"  progress: {status.progress() * 100:.1f}%")

    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload response did not contain an id: {response}")
    return video_id


def add_to_playlist(youtube, playlist_id: str, video_id: str) -> None:
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def get_uploads_playlist_id(youtube) -> str:
    response = youtube.channels().list(
        part="contentDetails",
        mine=True,
        maxResults=1,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("Could not find a YouTube channel for this account.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_account_videos_by_title(youtube) -> dict[str, list[dict[str, str]]]:
    uploads_playlist_id = get_uploads_playlist_id(youtube)
    videos_by_title: dict[str, list[dict[str, str]]] = {}
    page_token = None

    while True:
        response = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            title = snippet.get("title")
            video_id = snippet.get("resourceId", {}).get("videoId")
            if not title or not video_id:
                continue
            videos_by_title.setdefault(title, []).append(
                {
                    "video_id": video_id,
                    "title": title,
                    "published_at": snippet.get("publishedAt", ""),
                }
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            return videos_by_title


def mark_existing_upload(
    state: dict[str, Any],
    path: pathlib.Path,
    title: str,
    match: dict[str, str],
) -> None:
    stat = path.stat()
    state["uploaded"][file_key(path)] = {
        "video_id": match["video_id"],
        "title": title,
        "found_in_account_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "published_at": match.get("published_at", ""),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "matched_on": "title",
    }


def format_title(template: str, path: pathlib.Path) -> str:
    try:
        return template.format(**video_metadata(path))
    except KeyError as error:
        raise ValueError(f"Unknown title template field: {error.args[0]}") from error


def main() -> int:
    args = parse_args()
    secrets_path = pathlib.Path(args.secrets).expanduser()
    token_path = pathlib.Path(args.token).expanduser()
    state_path = pathlib.Path(args.state).expanduser()
    extensions = {
        item.strip().lower() if item.strip().startswith(".") else f".{item.strip().lower()}"
        for item in args.extensions.split(",")
        if item.strip()
    }
    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]

    videos = discover_videos(args.paths, extensions, recursive=not args.no_recursive)
    state = load_state(state_path)

    if args.upload_limit_status:
        youtube = get_youtube_client(secrets_path, token_path)
        has_active_limit = refresh_and_print_upload_limit_status(youtube, state)
        save_state(state_path, state)
        return 2 if has_active_limit else 0

    pending = [
        path
        for path in videos
        if args.force or file_key(path) not in state["uploaded"]
    ]
    print(f"found {len(videos)} video(s); {len(pending)} pending local upload(s)")
    if not pending:
        return 0

    preview = pending[: args.limit] if args.limit is not None else pending
    if args.check_existing_account and not args.dry_run and args.limit is not None:
        print("account check will run before applying the upload limit")
    for path in preview:
        metadata = video_metadata(path)
        title = format_title(args.title_template, path)
        print(f"- {path} ({metadata['size_mb']} MB) -> {title!r}")
    if len(preview) < len(pending):
        print(f"... {len(pending) - len(preview)} more pending local video(s)")

    if args.dry_run:
        return 0

    youtube = get_youtube_client(secrets_path, token_path)
    account_videos = None

    active_limit = get_active_upload_limit(state)
    if active_limit and not args.ignore_upload_limit_wait:
        account_videos = list_account_videos_by_title(youtube)
        refresh_upload_limit_from_account(state, account_videos)
        save_state(state_path, state)
        active_limit = get_active_upload_limit(state)
        if active_limit:
            print_upload_limit_summary(active_limit)
            if not args.wait_until_upload_limit_reset and not prompt_auto_retry():
                return 2
            wait_until_retry_time(active_limit)
            state.pop("upload_limit", None)
            save_state(state_path, state)

    if args.check_existing_account and not args.force:
        print("checking existing videos in your YouTube account...")
        if account_videos is None:
            account_videos = list_account_videos_by_title(youtube)
        still_pending = []
        skipped = 0
        for path in pending:
            title = format_title(args.title_template, path)
            matches = account_videos.get(title, [])
            if matches:
                match = matches[0]
                mark_existing_upload(state, path, title, match)
                skipped += 1
                print(f"  already exists: {path} -> https://youtu.be/{match['video_id']}")
            else:
                still_pending.append(path)

        if skipped:
            save_state(state_path, state)
            print(f"skipped {skipped} existing account video(s)")
        pending = still_pending

    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"{len(pending)} video(s) ready to upload")
    if not pending:
        return 0

    for index, path in enumerate(pending, start=1):
        title = format_title(args.title_template, path)
        print(f"[{index}/{len(pending)}] uploading {path}")
        try:
            video_id = upload_video(
                youtube=youtube,
                path=path,
                title=title,
                description=args.description,
                tags=tags,
                category_id=args.category_id,
                privacy=args.privacy,
            )
        except UploadLimitExceeded as error:
            last_upload_at = None
            if account_videos is None:
                try:
                    account_videos = list_account_videos_by_title(youtube)
                except Exception as account_error:
                    print(
                        "warning: could not refresh latest account upload after "
                        f"limit error: {account_error}",
                        file=sys.stderr,
                    )
            if account_videos is not None:
                last_upload_at = latest_account_upload_at(account_videos)
            upload_limit = record_upload_limit(
                state, last_account_upload_at=last_upload_at
            )
            save_state(state_path, state)
            retry_after = parse_datetime(upload_limit["retry_after"]).astimezone()
            remaining = format_duration(
                (parse_datetime(upload_limit["retry_after"]) - utcnow()).total_seconds()
            )
            print(f"upload stopped: {error}", file=sys.stderr)
            print(
                f"estimated wait: about {remaining}; try again after "
                f"{retry_after.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                file=sys.stderr,
            )
            return 2

        if args.playlist_id:
            add_to_playlist(youtube, args.playlist_id, video_id)

        stat = path.stat()
        state["uploaded"][file_key(path)] = {
            "video_id": video_id,
            "title": title,
            "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "privacy": args.privacy,
        }
        save_state(state_path, state)
        print(f"  uploaded: https://youtu.be/{video_id}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
