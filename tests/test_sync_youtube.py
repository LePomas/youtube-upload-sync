import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import types
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

    def test_load_state_rejects_invalid_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "state.json"
            state_path.write_text(json.dumps({"uploaded": []}), encoding="utf-8")

            with self.assertRaises(ValueError):
                sync_youtube.load_state(state_path)

    def test_discover_videos_handles_file_nonrecursive_and_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010001.MP4"
            nested = root / "nested"
            nested.mkdir()
            nested_video = nested / "GX010002.MP4"
            for path in (video, nested_video):
                path.write_bytes(b"x")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                direct = sync_youtube.discover_videos(
                    [str(video), str(root / "missing")], {".mp4"}, recursive=False
                )
                nonrecursive = sync_youtube.discover_videos(
                    [str(root)], {".mp4"}, recursive=False
                )

            self.assertEqual(direct, [video])
            self.assertEqual(nonrecursive, [video])
            self.assertIn("warning: skipping missing path", stderr.getvalue())

    def test_parse_datetime_and_format_duration_edges(self):
        self.assertEqual(
            sync_youtube.parse_datetime("2026-05-16T12:00:00Z"),
            sync_youtube.dt.datetime(
                2026, 5, 16, 12, 0, tzinfo=sync_youtube.dt.timezone.utc
            ),
        )
        self.assertEqual(
            sync_youtube.parse_datetime("2026-05-16T12:00:00").tzinfo,
            sync_youtube.dt.timezone.utc,
        )
        self.assertEqual(sync_youtube.format_duration(3600), "1h")
        self.assertEqual(sync_youtube.format_duration(-1), "0m")

    def test_is_upload_limit_error_detects_reason_text(self):
        self.assertTrue(sync_youtube.is_upload_limit_error(Exception("uploadLimitExceeded")))
        self.assertTrue(sync_youtube.is_upload_limit_error(Exception("quotaExceeded")))
        self.assertFalse(sync_youtube.is_upload_limit_error(Exception("dailyLimitExceeded")))

    def test_resolve_progress_mode_uses_tty_for_auto(self):
        self.assertEqual(
            sync_youtube.resolve_progress_mode("auto", FakeStream(is_tty=True)),
            "rich",
        )
        self.assertEqual(
            sync_youtube.resolve_progress_mode("auto", FakeStream(is_tty=False)),
            "plain",
        )
        self.assertEqual(
            sync_youtube.resolve_progress_mode("plain", FakeStream(is_tty=True)),
            "plain",
        )

    def test_rich_upload_progress_uses_rich_renderer(self):
        console_module = types.ModuleType("rich.console")
        progress_module = types.ModuleType("rich.progress")
        fake_progress = FakeRichProgress
        console_module.Console = FakeRichConsole
        progress_module.Progress = fake_progress
        progress_module.BarColumn = FakeRichColumn
        progress_module.SpinnerColumn = FakeRichColumn
        progress_module.TaskProgressColumn = FakeRichColumn
        progress_module.TextColumn = FakeRichColumn
        progress_module.TimeElapsedColumn = FakeRichColumn

        with mock.patch.dict(
            sys.modules,
            {
                "rich": types.ModuleType("rich"),
                "rich.console": console_module,
                "rich.progress": progress_module,
            },
        ):
            progress = sync_youtube.make_upload_progress("rich")
            with progress as active:
                with active.status("checking..."):
                    pass
                active.start_upload(1, 2, pathlib.Path("/tmp/GH010001.MP4"))
                active.update_upload(0.5)
                active.start_upload(2, 2, pathlib.Path("/tmp/GH010002.MP4"))
                active.finish_upload("video-2")

        self.assertTrue(progress.progress.started)
        self.assertTrue(progress.progress.stopped)
        self.assertEqual(progress.progress.removed, [1, 2])
        self.assertEqual(
            progress.progress.updates,
            [(2, {"completed": 50.0}), (3, {"completed": 100})],
        )
        self.assertEqual(progress.console.lines, ["  uploaded: https://youtu.be/video-2"])

    def test_plain_upload_progress_writes_deterministic_lines(self):
        progress = sync_youtube.PlainUploadProgress()
        stdout = io.StringIO()
        path = pathlib.Path("/tmp/GH010084.MP4")

        with contextlib.redirect_stdout(stdout):
            with progress as active:
                with active.status("checking account..."):
                    pass
                active.start_upload(1, 3, path)
                active.update_upload(0.375)
                active.finish_upload("video-1")

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "checking account...",
                "[1/3] uploading /tmp/GH010084.MP4",
                "  progress: 37.5%",
                "  uploaded: https://youtu.be/video-1",
            ],
        )

    def test_rich_progress_status_removes_task_when_body_raises(self):
        progress = FakeRichProgress(console=FakeRichConsole())
        status = sync_youtube.RichProgressStatus(progress, "checking...")

        with self.assertRaises(RuntimeError):
            with status:
                raise RuntimeError("boom")

        self.assertEqual(progress.tasks, [(1, "checking...", None)])
        self.assertEqual(progress.removed, [1])

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

    def test_main_forced_rich_uses_progress_reporter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            for path in (video, secrets_path):
                path.write_bytes(b"x")

            progress = CapturingProgress()

            def fake_upload_video(**kwargs):
                kwargs["progress_callback"](0.5)
                return "new-video"

            argv = [
                "sync_youtube.py",
                "--progress",
                "rich",
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
                sync_youtube, "make_upload_progress", return_value=progress
            ), mock.patch.object(
                sync_youtube, "upload_video", side_effect=fake_upload_video
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue(progress.entered)
            self.assertEqual(progress.uploads, [(1, 1, video)])
            self.assertEqual(progress.updates, [0.5])
            self.assertEqual(progress.finished, ["new-video"])
            self.assertNotIn("[1/1] uploading", stdout.getvalue())

    def test_main_auto_progress_uses_plain_reporter_for_non_tty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            video = root / "GH010084.MP4"
            state_path = root / "state.json"
            secrets_path = root / "secrets.json"
            token_path = root / "token.json"
            for path in (video, secrets_path):
                path.write_bytes(b"x")
            progress = CapturingProgress()

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
            with contextlib.redirect_stdout(stdout), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube, "make_upload_progress", return_value=progress
            ) as make_upload_progress, mock.patch.object(
                sync_youtube, "upload_video", return_value="new-video"
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 0)
            make_upload_progress.assert_called_once_with("plain")
            self.assertTrue(progress.entered)
            self.assertEqual(progress.uploads, [(1, 1, video)])
            self.assertEqual(progress.finished, ["new-video"])

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

    def test_upload_limit_timestamp_only_when_latest_upload_is_old(self):
        hit_at = sync_youtube.dt.datetime(
            2026, 5, 16, 12, 0, tzinfo=sync_youtube.dt.timezone.utc
        )
        last_upload_at = hit_at - sync_youtube.dt.timedelta(hours=25)
        state = sync_youtube.empty_state()

        upload_limit = sync_youtube.record_upload_limit(
            state,
            now=hit_at,
            last_account_upload_at=last_upload_at,
        )

        self.assertNotIn("retry_after", upload_limit)
        self.assertIsNone(sync_youtube.get_active_upload_limit(state, now=hit_at))

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

    def test_refresh_upload_limit_returns_none_without_account_uploads(self):
        state = sync_youtube.empty_state()

        upload_limit = sync_youtube.refresh_upload_limit_from_account(state, {})

        self.assertIsNone(upload_limit)
        self.assertEqual(state, {"uploaded": {}})

    def test_print_upload_limit_status_branches(self):
        inactive_stdout = io.StringIO()
        with contextlib.redirect_stdout(inactive_stdout):
            inactive = sync_youtube.print_upload_limit_status(sync_youtube.empty_state())

        state = sync_youtube.empty_state()
        last_upload_at = sync_youtube.dt.datetime(
            2999, 1, 1, 0, 0, tzinfo=sync_youtube.dt.timezone.utc
        )
        sync_youtube.record_upload_limit(
            state,
            now=last_upload_at,
            last_account_upload_at=last_upload_at,
        )
        active_stdout = io.StringIO()
        with contextlib.redirect_stdout(active_stdout):
            active = sync_youtube.print_upload_limit_status(state)

        self.assertFalse(inactive)
        self.assertIn("no active upload-limit wait saved", inactive_stdout.getvalue())
        self.assertTrue(active)
        self.assertIn("latest account upload:", active_stdout.getvalue())

    def test_refresh_and_print_upload_limit_status_without_uploads(self):
        youtube = FakeYoutube(pages=[{"items": []}])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            active = sync_youtube.refresh_and_print_upload_limit_status(
                youtube, sync_youtube.empty_state()
            )

        self.assertFalse(active)
        self.assertIn("no account uploads found", stdout.getvalue())

    def test_refresh_and_print_upload_limit_status_keeps_timestamp_only_state(self):
        state = sync_youtube.empty_state()
        sync_youtube.record_upload_limit(
            state,
            now=sync_youtube.dt.datetime(
                2026, 5, 16, 12, 0, tzinfo=sync_youtube.dt.timezone.utc
            ),
            last_account_upload_at=sync_youtube.dt.datetime(
                2026, 5, 15, 10, 0, tzinfo=sync_youtube.dt.timezone.utc
            ),
        )
        youtube = FakeYoutube(
            pages=[
                {
                    "items": [
                        {
                            "snippet": {
                                "title": "old",
                                "publishedAt": "2026-05-15T10:00:00Z",
                                "resourceId": {"videoId": "old-video"},
                            }
                        }
                    ]
                }
            ]
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            active = sync_youtube.refresh_and_print_upload_limit_status(youtube, state)

        self.assertFalse(active)
        self.assertIn("upload_limit", state)
        self.assertNotIn("retry_after", state["upload_limit"])
        self.assertIn("last upload-limit error:", stdout.getvalue())

    def test_add_to_playlist_inserts_playlist_item(self):
        youtube = FakePlaylistInsertYoutube()

        sync_youtube.add_to_playlist(youtube, "playlist-1", "video-1")

        self.assertEqual(youtube.insert_kwargs["part"], "snippet")
        self.assertEqual(
            youtube.insert_kwargs["body"]["snippet"]["playlistId"], "playlist-1"
        )
        self.assertEqual(
            youtube.insert_kwargs["body"]["snippet"]["resourceId"]["videoId"],
            "video-1",
        )

    def test_upload_video_success_without_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "GH010084.MP4"
            video.write_bytes(b"x")
            youtube = FakeVideoInsertYoutube(FakeUploadRequest([(None, {"id": "abc123"})]))

            video_id = sync_youtube.upload_video(
                youtube,
                video,
                title="GH010084",
                description="desc",
                tags=["gopro"],
                category_id="22",
                privacy="private",
            )

        self.assertEqual(video_id, "abc123")
        self.assertEqual(youtube.insert_kwargs["part"], "snippet,status")
        self.assertEqual(youtube.insert_kwargs["body"]["snippet"]["title"], "GH010084")

    def test_upload_video_reports_progress_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "GH010084.MP4"
            video.write_bytes(b"x")
            youtube = FakeVideoInsertYoutube(
                FakeUploadRequest(
                    [
                        (FakeUploadStatus(0.25), None),
                        (FakeUploadStatus(0.75), None),
                        (None, {"id": "abc123"}),
                    ]
                )
            )
            progress_updates = []

            video_id = sync_youtube.upload_video(
                youtube,
                video,
                title="GH010084",
                description="desc",
                tags=["gopro"],
                category_id="22",
                privacy="private",
                progress_callback=progress_updates.append,
            )

        self.assertEqual(video_id, "abc123")
        self.assertEqual(progress_updates, [0.25, 0.75])

    def test_upload_video_raises_when_response_has_no_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "GH010084.MP4"
            video.write_bytes(b"x")
            youtube = FakeVideoInsertYoutube(FakeUploadRequest([(None, {})]))

            with self.assertRaises(RuntimeError):
                sync_youtube.upload_video(
                    youtube,
                    video,
                    title="GH010084",
                    description="desc",
                    tags=[],
                    category_id="22",
                    privacy="private",
                )

    def test_upload_video_handles_resumable_quota_error(self):
        from googleapiclient.errors import ResumableUploadError

        class FakeResponse:
            status = 403
            reason = "Forbidden"

        content = json.dumps(
            {
                "error": {
                    "message": "quota exceeded",
                    "errors": [{"reason": "quotaExceeded"}],
                }
            }
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            video = pathlib.Path(tmp) / "GH010084.MP4"
            video.write_bytes(b"x")
            youtube = FakeVideoInsertYoutube(
                FakeUploadRequest([ResumableUploadError(FakeResponse(), content)])
            )

            with self.assertRaises(sync_youtube.UploadLimitExceeded):
                sync_youtube.upload_video(
                    youtube,
                    video,
                    title="GH010084",
                    description="desc",
                    tags=[],
                    category_id="22",
                    privacy="private",
                )

    def test_main_records_timestamp_only_for_old_latest_upload_after_quota(self):
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
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(
                sync_youtube, "get_youtube_client", return_value=object()
            ), mock.patch.object(
                sync_youtube,
                "list_account_videos_by_title",
                return_value={
                    "old": [
                        {
                            "video_id": "old-video",
                            "published_at": "2000-01-01T00:00:00Z",
                        }
                    ]
                },
            ), mock.patch.object(
                sync_youtube,
                "upload_video",
                side_effect=sync_youtube.UploadLimitExceeded("quota hit"),
            ):
                exit_code = sync_youtube.main()

            self.assertEqual(exit_code, 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn(sync_youtube.file_key(video), state["uploaded"])
            self.assertIn("upload_limit", state)
            self.assertNotIn("retry_after", state["upload_limit"])
            self.assertIn("no active wait estimate saved", stderr.getvalue())

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


class FakePlaylistInsertYoutube:
    def __init__(self):
        self.insert_kwargs = None

    def playlistItems(self):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return FakeExecute({})


class FakeVideoInsertYoutube:
    def __init__(self, request):
        self.request = request
        self.insert_kwargs = None

    def videos(self):
        return self

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return self.request


class FakeUploadRequest:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def next_chunk(self):
        if not self.chunks:
            raise AssertionError("next_chunk called too many times")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


class FakeUploadStatus:
    def __init__(self, progress):
        self._progress = progress

    def progress(self):
        return self._progress


class FakeStream:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class CapturingProgress:
    def __init__(self):
        self.entered = False
        self.statuses = []
        self.uploads = []
        self.updates = []
        self.finished = []

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def status(self, message):
        self.statuses.append(message)
        return contextlib.nullcontext()

    def start_upload(self, index, total, path):
        self.uploads.append((index, total, path))

    def update_upload(self, progress):
        self.updates.append(progress)

    def finish_upload(self, video_id):
        self.finished.append(video_id)


class FakeRichColumn:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class FakeRichConsole:
    def __init__(self):
        self.statuses = []
        self.lines = []

    def status(self, message):
        self.statuses.append(message)
        return contextlib.nullcontext()

    def print(self, message):
        self.lines.append(message)


class FakeRichProgress:
    def __init__(self, *columns, console):
        self.columns = columns
        self.console = console
        self.started = False
        self.stopped = False
        self.tasks = []
        self.removed = []
        self.updates = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def add_task(self, description, total):
        task_id = len(self.tasks) + 1
        self.tasks.append((task_id, description, total))
        return task_id

    def remove_task(self, task_id):
        self.removed.append(task_id)

    def update(self, task_id, **kwargs):
        self.updates.append((task_id, kwargs))


if __name__ == "__main__":
    unittest.main()
