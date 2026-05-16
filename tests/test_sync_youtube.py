import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import sync_youtube


class SyncYoutubeTests(unittest.TestCase):
    def test_discover_videos_sorts_by_mtime_and_filters_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            newer = root / "GH010002.MP4"
            older = root / "GH010001.MP4"
            ignored = root / "GOPR0001.JPG"
            nested = root / "nested"
            nested.mkdir()
            nested_video = nested / "GX010003.MP4"

            for path in (newer, older, ignored, nested_video):
                path.write_bytes(b"x")
            os.utime(older, (1_700_000_000, 1_700_000_000))
            os.utime(newer, (1_700_000_010, 1_700_000_010))
            os.utime(nested_video, (1_700_000_020, 1_700_000_020))

            discovered = sync_youtube.discover_videos(
                [str(root)], {".mp4"}, recursive=True
            )

            self.assertEqual([path.name for path in discovered], [
                "GH010001.MP4",
                "GH010002.MP4",
                "GX010003.MP4",
            ])

    def test_format_title_uses_file_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "GH010084.MP4"
            video.write_bytes(b"x")

            title = sync_youtube.format_title("{date} {stem} {size_mb}", video)

            self.assertIn("GH010084", title)
            self.assertTrue(title.endswith("0.0"))

    def test_list_account_videos_by_title_handles_pages(self):
        youtube = FakeYoutube(
            pages=[
                {
                    "items": [
                        {
                            "snippet": {
                                "title": "GH010001",
                                "publishedAt": "2026-05-01T00:00:00Z",
                                "resourceId": {"videoId": "video-1"},
                            }
                        }
                    ],
                    "nextPageToken": "next",
                },
                {
                    "items": [
                        {
                            "snippet": {
                                "title": "GH010002",
                                "publishedAt": "2026-05-02T00:00:00Z",
                                "resourceId": {"videoId": "video-2"},
                            }
                        }
                    ]
                },
            ]
        )

        videos = sync_youtube.list_account_videos_by_title(youtube)

        self.assertEqual(videos["GH010001"][0]["video_id"], "video-1")
        self.assertEqual(videos["GH010002"][0]["video_id"], "video-2")

    def test_main_skips_account_matches_and_uploads_remaining_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            existing = root / "GH010001.MP4"
            missing = root / "GH010002.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            for path in (existing, missing, secrets_path):
                path.write_bytes(b"x")

            argv = [
                "sync_youtube.py",
                "--check-existing-account",
                "--state",
                str(state_path),
                "--secrets",
                str(secrets_path),
                "--token",
                str(token_path),
                str(root),
            ]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "list_account_videos_by_title",
                return_value={
                    "GH010001": [
                        {
                            "video_id": "existing-video",
                            "published_at": "2026-05-01T00:00:00Z",
                        }
                    ]
                },
            ), mock.patch.object(
                sync_youtube, "upload_video", return_value="new-video"
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["uploaded"][sync_youtube.file_key(existing)]["matched_on"],
                "title",
            )
            self.assertEqual(
                state["uploaded"][sync_youtube.file_key(missing)]["video_id"],
                "new-video",
            )

    def test_main_stops_cleanly_on_upload_limit_without_marking_failed_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            for path in (video, secrets_path):
                path.write_bytes(b"x")

            argv = [
                "sync_youtube.py",
                "--state",
                str(state_path),
                "--secrets",
                str(secrets_path),
                "--token",
                str(token_path),
                str(root),
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ), mock.patch.object(sys, "argv", argv), mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "upload_video",
                side_effect=sync_youtube.UploadLimitExceeded("upload limit hit"),
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn(sync_youtube.file_key(video), state["uploaded"])
            self.assertIn("upload_limit", state)

    def test_upload_limit_status_reports_remaining_wait(self):
        last_upload_at = sync_youtube.dt.datetime(
            2026, 5, 16, 12, 0, tzinfo=sync_youtube.dt.timezone.utc
        )
        state = sync_youtube.empty_state()
        sync_youtube.record_upload_limit(
            state,
            now=last_upload_at + sync_youtube.dt.timedelta(hours=1),
            last_account_upload_at=last_upload_at,
        )

        active_limit = sync_youtube.get_active_upload_limit(
            state,
            now=last_upload_at + sync_youtube.dt.timedelta(hours=2, minutes=30),
        )

        self.assertIsNotNone(active_limit)
        self.assertEqual(active_limit["last_account_upload_at"], last_upload_at)
        self.assertEqual(
            sync_youtube.format_duration(active_limit["remaining_seconds"]),
            "21h 30m",
        )

    def test_upload_limit_status_expires_after_24_hours(self):
        last_upload_at = sync_youtube.dt.datetime(
            2026, 5, 16, 12, 0, tzinfo=sync_youtube.dt.timezone.utc
        )
        state = sync_youtube.empty_state()
        sync_youtube.record_upload_limit(
            state,
            now=last_upload_at + sync_youtube.dt.timedelta(hours=1),
            last_account_upload_at=last_upload_at,
        )

        active_limit = sync_youtube.get_active_upload_limit(
            state,
            now=last_upload_at + sync_youtube.dt.timedelta(hours=24),
        )

        self.assertIsNone(active_limit)

    def test_refresh_upload_limit_uses_latest_account_upload(self):
        state = sync_youtube.empty_state()
        now = sync_youtube.dt.datetime(
            2026, 5, 16, 20, 0, tzinfo=sync_youtube.dt.timezone.utc
        )
        account_videos = {
            "GH010001": [{"video_id": "old", "published_at": "2026-05-15T10:00:00Z"}],
            "GH010002": [{"video_id": "new", "published_at": "2026-05-16T08:30:00Z"}],
        }

        upload_limit = sync_youtube.refresh_upload_limit_from_account(
            state, account_videos, now=now
        )

        self.assertIsNotNone(upload_limit)
        self.assertEqual(
            sync_youtube.parse_datetime(upload_limit["retry_after"]),
            sync_youtube.dt.datetime(
                2026, 5, 17, 8, 30, tzinfo=sync_youtube.dt.timezone.utc
            ),
        )

    def test_refresh_upload_limit_clears_expired_wait(self):
        state = {
            "uploaded": {},
            "upload_limit": {"retry_after": "2026-05-16T00:00:00+00:00"},
        }
        account_videos = {
            "GH010001": [{"video_id": "old", "published_at": "2026-05-15T08:00:00Z"}],
        }
        now = sync_youtube.dt.datetime(
            2026, 5, 16, 8, 1, tzinfo=sync_youtube.dt.timezone.utc
        )

        upload_limit = sync_youtube.refresh_upload_limit_from_account(
            state, account_videos, now=now
        )

        self.assertIsNone(upload_limit)
        self.assertNotIn("upload_limit", state)

    def test_active_upload_limit_non_tty_exits_with_concise_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            video.write_bytes(b"x")
            secrets_path.write_bytes(b"x")
            state = sync_youtube.empty_state()
            sync_youtube.record_upload_limit(
                state,
                now=sync_youtube.dt.datetime(
                    2998, 12, 31, 23, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
                last_account_upload_at=sync_youtube.dt.datetime(
                    2999, 1, 1, 0, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
            )
            sync_youtube.save_state(state_path, state)

            argv = [
                "sync_youtube.py",
                "--state",
                str(state_path),
                "--secrets",
                str(secrets_path),
                "--token",
                str(token_path),
                str(root),
            ]
            stdout = io.StringIO()
            stdin = mock.Mock()
            stdin.isatty.return_value = False
            with contextlib.redirect_stdout(stdout), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(sys, "stdin", stdin), mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "list_account_videos_by_title",
                return_value={
                    "latest": [
                        {
                            "video_id": "latest-video",
                            "published_at": "2999-01-01T00:00:00Z",
                        }
                    ]
                },
            ), mock.patch.object(sync_youtube, "upload_video") as upload_video:
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 2)
            upload_video.assert_not_called()
            output = stdout.getvalue()
            self.assertIn("Upload limit active:", output)
            self.assertIn("--ignore-upload-limit-wait", output)
            self.assertNotIn("latest account upload:", output)
            self.assertNotIn("still inside the 24-hour wait", output)

    def test_active_upload_limit_tty_yes_waits_then_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            video.write_bytes(b"x")
            secrets_path.write_bytes(b"x")
            state = sync_youtube.empty_state()
            sync_youtube.record_upload_limit(
                state,
                now=sync_youtube.dt.datetime(
                    2998, 12, 31, 23, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
                last_account_upload_at=sync_youtube.dt.datetime(
                    2999, 1, 1, 0, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
            )
            sync_youtube.save_state(state_path, state)

            argv = [
                "sync_youtube.py",
                "--state",
                str(state_path),
                "--secrets",
                str(secrets_path),
                "--token",
                str(token_path),
                str(root),
            ]
            stdout = io.StringIO()
            stdin = mock.Mock()
            stdin.isatty.return_value = True
            with contextlib.redirect_stdout(stdout), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(sys, "stdin", stdin), mock.patch(
                "builtins.input", return_value="yes"
            ), mock.patch.object(
                sync_youtube, "time"
            ) as time_module, mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "list_account_videos_by_title",
                return_value={
                    "latest": [
                        {
                            "video_id": "latest-video",
                            "published_at": "2999-01-01T00:00:00Z",
                        }
                    ]
                },
            ), mock.patch.object(
                sync_youtube, "upload_video", return_value="new-video"
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 0)
            time_module.sleep.assert_called_once()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("upload_limit", state)
            self.assertEqual(
                state["uploaded"][sync_youtube.file_key(video)]["video_id"],
                "new-video",
            )

    def test_active_upload_limit_flag_waits_in_non_tty_then_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            video.write_bytes(b"x")
            secrets_path.write_bytes(b"x")
            state = sync_youtube.empty_state()
            sync_youtube.record_upload_limit(
                state,
                now=sync_youtube.dt.datetime(
                    2998, 12, 31, 23, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
                last_account_upload_at=sync_youtube.dt.datetime(
                    2999, 1, 1, 0, 0, tzinfo=sync_youtube.dt.timezone.utc
                ),
            )
            sync_youtube.save_state(state_path, state)

            argv = [
                "sync_youtube.py",
                "--wait-until-upload-limit-reset",
                "--state",
                str(state_path),
                "--secrets",
                str(secrets_path),
                "--token",
                str(token_path),
                str(root),
            ]
            stdout = io.StringIO()
            stdin = mock.Mock()
            stdin.isatty.return_value = False
            with contextlib.redirect_stdout(stdout), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(sys, "stdin", stdin), mock.patch(
                "builtins.input"
            ) as input_mock, mock.patch.object(
                sync_youtube, "time"
            ) as time_module, mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "list_account_videos_by_title",
                return_value={
                    "latest": [
                        {
                            "video_id": "latest-video",
                            "published_at": "2999-01-01T00:00:00Z",
                        }
                    ]
                },
            ), mock.patch.object(
                sync_youtube, "upload_video", return_value="new-video"
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 0)
            input_mock.assert_not_called()
            time_module.sleep.assert_called_once()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("upload_limit", state)
            self.assertEqual(
                state["uploaded"][sync_youtube.file_key(video)]["video_id"],
                "new-video",
            )


class FakeYoutube:
    def __init__(self, pages):
        self.playlist_items = FakePlaylistItems(pages)

    def channels(self):
        return FakeChannels()

    def playlistItems(self):
        return self.playlist_items


class FakeChannels:
    def list(self, **kwargs):
        return FakeExecute(
            {
                "items": [
                    {
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": "uploads-playlist"}
                        }
                    }
                ]
            }
        )


class FakePlaylistItems:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def list(self, **kwargs):
        page = self.pages[self.calls]
        self.calls += 1
        return FakeExecute(page)


class FakeExecute:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


if __name__ == "__main__":
    unittest.main()
