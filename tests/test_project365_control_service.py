from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project365_control_service as service


class Project365ControlServiceTests(unittest.TestCase):
    def test_control_panel_command_starts_local_control_app(self) -> None:
        config = service.ServiceConfig(host="127.0.0.1", port=9876)

        command = service._control_panel_command(config)

        self.assertIn("project365_control_app.py", command[1])
        self.assertIn("--host", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--port", command)
        self.assertIn("9876", command)

    def test_video_memory_review_command_starts_local_video_server(self) -> None:
        config = service.ServiceConfig(host="127.0.0.1", video_port=9877)

        command = service._video_memory_review_command(config)

        self.assertIn("project365_video_memory_review.py", command[1])
        self.assertIn("serve", command)
        self.assertIn("--host", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--port", command)
        self.assertIn("9877", command)

    def test_swarm_backfill_command_starts_local_review_server(self) -> None:
        config = service.ServiceConfig(host="127.0.0.1", swarm_backfill_port=9878)

        command = service._swarm_backfill_review_command(config)

        self.assertIn("swarm_photo_backfill_review.py", command[1])
        self.assertIn("--source", command)
        self.assertIn("--state-dir", command)
        self.assertIn("--port", command)
        self.assertIn("9878", command)

    def test_video_server_detection_excludes_prepare_batch_process(self) -> None:
        serve_command = (
            "python project365_video_memory_review.py --canonical-root "
            "Project365Canonical serve --host 127.0.0.1 --port 8767"
        )
        batch_command = (
            "python project365_video_memory_review.py --canonical-root "
            "Project365Canonical prepare-batch --all-missing"
        )

        self.assertTrue(
            service._is_project365_server_command(
                serve_command,
                server_scripts=(service.VIDEO_SERVER_SCRIPT,),
            )
        )
        self.assertFalse(
            service._is_project365_server_command(
                batch_command,
                server_scripts=(service.VIDEO_SERVER_SCRIPT,),
            )
        )

    def test_start_adopts_existing_project365_server_on_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(port=9876, runtime_dir=Path(temp_dir))
            with mock.patch.object(service, "_project_server_pids", return_value=[12345]):
                with mock.patch.object(service, "_pid_running", return_value=True):
                    result = service.start_control_panel(config)

            record = json.loads(config.pid_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["pid"], 12345)
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["port"], 9876)

    def test_start_does_not_inherit_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(port=9876, runtime_dir=Path(temp_dir))
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(service, "_wait_for_control_panel"),
            ):
                service.start_control_panel(config)

        self.assertEqual(popen.call_args.kwargs["stdin"], service.subprocess.DEVNULL)

    def test_video_start_does_not_inherit_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(video_port=9877, runtime_dir=Path(temp_dir))
            process = mock.Mock(pid=12346)
            process.poll.return_value = None
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(service, "_wait_for_video_memory_review"),
            ):
                service.start_video_memory_review(config)

        self.assertEqual(popen.call_args.kwargs["stdin"], service.subprocess.DEVNULL)

    def test_swarm_backfill_start_does_not_inherit_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(swarm_backfill_port=9878, runtime_dir=Path(temp_dir))
            process = mock.Mock(pid=12347)
            process.poll.return_value = None
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service.subprocess, "Popen", return_value=process) as popen,
                mock.patch.object(service, "_wait_for_swarm_backfill_review"),
            ):
                service.start_swarm_backfill_review(config)

        self.assertEqual(popen.call_args.kwargs["stdin"], service.subprocess.DEVNULL)

    def test_swarm_backfill_start_adopts_existing_server_on_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(swarm_backfill_port=9878, runtime_dir=Path(temp_dir))
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[12347]),
                mock.patch.object(service, "_pid_running", return_value=True),
            ):
                result = service.start_swarm_backfill_review(config)

            record = json.loads(config.swarm_backfill_pid_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["pid"], 12347)
        self.assertEqual(record["pid"], 12347)
        self.assertEqual(record["port"], 9878)

    def test_wait_for_control_panel_checks_page_not_full_status(self) -> None:
        config = service.ServiceConfig(host="127.0.0.1", port=9876)
        process = mock.Mock()
        process.poll.return_value = None
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(service.urllib.request, "urlopen", return_value=response) as urlopen:
            service._wait_for_control_panel(config, process, timeout_seconds=0.1)

        urlopen.assert_called_once_with(config.url, timeout=20)

    def test_port_probe_checks_page_not_full_status(self) -> None:
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"<title>Project365 Control</title>"

        with mock.patch.object(service.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertTrue(service._port_has_project365_status("127.0.0.1", 9876))

        urlopen.assert_called_once_with("http://127.0.0.1:9876", timeout=20)

    def test_stop_removes_pid_record_and_stops_tracked_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            config.runtime_dir.mkdir(exist_ok=True)
            service._write_service_pid_record(
                config.pid_path, 12345, config.host, config.port,
                config.url, config.log_path,
            )
            with (
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_project_server_pids", side_effect=[[12345], [], [], [], []]),
                mock.patch.object(service.os, "getpgid", return_value=12345),
                mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)

            self.assertFalse(config.pid_path.exists())

        self.assertEqual(result["status"], "stopped")
        terminate.assert_called_once_with(12345)

    def test_stop_removes_all_pid_records_and_stops_all_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            config.runtime_dir.mkdir(exist_ok=True)
            for path, pid, port, url, log_path in (
                (config.pid_path, 12345, config.port, config.url, config.log_path),
                (config.video_pid_path, 12346, config.video_port, config.video_url, config.video_log_path),
                (config.swarm_backfill_pid_path, 12347, config.swarm_backfill_port, config.swarm_backfill_url, config.swarm_backfill_log_path),
                (config.swarm_media_pid_path, 12348, config.swarm_media_port, f"http://{config.host}:{config.swarm_media_port}", config.swarm_media_pid_path.with_suffix(".log")),
                (config.twitter_pid_path, 12349, config.twitter_port, f"http://{config.host}:{config.twitter_port}", config.twitter_pid_path.with_suffix(".log")),
            ):
                service._write_service_pid_record(path, pid, config.host, port, url, log_path)
            with (
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_project_server_pids", side_effect=[[12345], [12346], [12347], [12348], [12349]]),
                mock.patch.object(service.os, "getpgid", side_effect=[12345, 12346, 12347, 12348, 12349]),
                mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)

            self.assertFalse(config.pid_path.exists())
            self.assertFalse(config.video_pid_path.exists())
            self.assertFalse(config.swarm_backfill_pid_path.exists())
            self.assertFalse(config.swarm_media_pid_path.exists())
            self.assertFalse(config.twitter_pid_path.exists())

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            terminate.call_args_list,
            [mock.call(12345), mock.call(12346), mock.call(12347), mock.call(12348), mock.call(12349)],
        )

    def test_stop_detects_unmanaged_video_server_on_video_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [12346], [], [], []]) as detect,
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_descendant_pids", return_value=[]),
                mock.patch.object(service, "_terminate_pids", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(detect.call_args_list[1].kwargs["port"], config.video_port)
        self.assertEqual(detect.call_args_list[1].kwargs["page_marker"], "Project365 Video Memory Review")
        terminate.assert_called_once_with([12346])

    def test_stop_detects_unmanaged_swarm_backfill_server_on_its_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [], [12347], [], []]) as detect,
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_descendant_pids", return_value=[]),
                mock.patch.object(service, "_terminate_pids", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(detect.call_args_list[2].kwargs["port"], config.swarm_backfill_port)
        self.assertEqual(
            detect.call_args_list[2].kwargs["page_marker"],
            "Swarm representative-photo review",
        )
        terminate.assert_called_once_with([12347])

    def test_start_project365_services_starts_only_the_control_panel(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(
                service,
                "start_control_panel",
                return_value={"status": "started", "running": True, "pid": 1},
            ) as start_control,
            mock.patch.object(service, "start_video_memory_review") as start_video,
            mock.patch.object(service, "start_swarm_backfill_review") as start_swarm,
            mock.patch.object(service, "start_review_service") as start_review,
            mock.patch.object(service, "stop_project365_servers") as stop_all,
        ):
            result = service.start_project365_services(config)

        self.assertTrue(result["running"])
        self.assertEqual(result["status"], "started")
        start_control.assert_called_once_with(config)
        self.assertEqual(result["services"], {"control": {"status": "started", "running": True, "pid": 1}})
        start_video.assert_not_called()
        start_swarm.assert_not_called()
        start_review.assert_not_called()
        stop_all.assert_not_called()

    def test_control_running_is_healthy_with_optional_services_stopped(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(service, "service_status", return_value={"running": True, "status": "running"}),
            mock.patch.object(service, "video_service_status", return_value={"running": False, "status": "stopped"}),
            mock.patch.object(service, "swarm_backfill_service_status", return_value={"running": False, "status": "stopped"}),
            mock.patch.object(service, "review_service_status", return_value={"running": False, "status": "stopped"}),
        ):
            result = service.project365_services_status(config)

        self.assertTrue(result["running"])
        self.assertEqual(result["status"], "running")
        status_text = service._human_status(result)
        self.assertIn("Video: stopped (starts on demand)", status_text)
        self.assertIn("Swarm photo order and crops: stopped (starts on demand)", status_text)
        self.assertIn("Twitter diary review: stopped (starts on demand)", status_text)
        with (
            mock.patch.object(service.sys, "argv", ["project365_control_service.py", "status"]),
            mock.patch.object(service, "project365_services_status", return_value=result),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(service.main(), 0)

    def test_control_down_is_unhealthy_even_when_optional_services_run(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(service, "service_status", return_value={"running": False, "status": "stopped"}),
            mock.patch.object(service, "video_service_status", return_value={"running": True}),
            mock.patch.object(service, "swarm_backfill_service_status", return_value={"running": True}),
            mock.patch.object(service, "review_service_status", return_value={"running": True, "status": "running"}),
        ):
            result = service.project365_services_status(config)

        self.assertFalse(result["running"])
        with (
            mock.patch.object(service.sys, "argv", ["project365_control_service.py", "status"]),
            mock.patch.object(service, "project365_services_status", return_value=result),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(service.main(), 1)

    def test_review_commands_include_expected_sources_outputs_and_ports(self) -> None:
        config = service.ServiceConfig(swarm_media_port=9879, twitter_port=9880)
        swarm = service._review_service_spec(config, "swarm_media")
        twitter = service._review_service_spec(config, "twitter")
        self.assertIn("swarm_media_workflow.py", swarm["command"][1])
        self.assertIn("--database", swarm["command"])
        self.assertIn("9879", swarm["command"])
        self.assertIn("twitter_dayone_workflow.py", twitter["command"][1])
        self.assertIn("serve", twitter["command"])
        self.assertIn("--output-dir", twitter["command"])
        self.assertIn("9880", twitter["command"])

    def test_review_start_adopts_running_server_and_records_pid(self) -> None:
        for key in ("swarm_media", "twitter"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                config = service.ServiceConfig(runtime_dir=Path(temporary))
                with mock.patch.object(service, "_project_server_pids", return_value=[12348]), mock.patch.object(service.subprocess, "Popen") as popen:
                    result = service.start_review_service(config, key)
                spec = service._review_service_spec(config, key)
                self.assertEqual(result["status"], "running")
                self.assertEqual(json.loads(spec["pid_path"].read_text())["pid"], 12348)
                popen.assert_not_called()

    def test_review_start_detaches_stdin_and_cleans_failed_start(self) -> None:
        for key in ("swarm_media", "twitter"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                config = service.ServiceConfig(runtime_dir=Path(temporary))
                process = mock.Mock(pid=12348)
                with (
                    mock.patch.object(service, "_project_server_pids", return_value=[]),
                    mock.patch.object(service.subprocess, "Popen", return_value=process) as popen,
                    mock.patch.object(service, "_wait_for_review_service", side_effect=RuntimeError("not ready")),
                    mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
                ):
                    with self.assertRaisesRegex(RuntimeError, "not ready"):
                        service.start_review_service(config, key)
                self.assertEqual(popen.call_args.kwargs["stdin"], service.subprocess.DEVNULL)
                terminate.assert_called_once_with(12348)
                self.assertFalse(service._review_service_spec(config, key)["pid_path"].exists())

    def test_review_health_rejects_unrelated_json(self) -> None:
        config = service.ServiceConfig()
        process = mock.Mock()
        process.poll.return_value = None
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"unrelated": true}'
        for key in ("swarm_media", "twitter"):
            with self.subTest(key=key), mock.patch.object(service.urllib.request, "urlopen", return_value=response), mock.patch.object(service.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "did not respond"):
                    service._wait_for_review_service(config, key, process, timeout_seconds=0.01)

    def test_status_stays_healthy_when_either_new_optional_server_is_stopped(self) -> None:
        for stopped_key in ("swarm_media", "twitter"):
            with (
                self.subTest(key=stopped_key),
                mock.patch.object(service, "service_status", return_value={"running": True}),
                mock.patch.object(service, "video_service_status", return_value={"running": True}),
                mock.patch.object(service, "swarm_backfill_service_status", return_value={"running": True}),
                mock.patch.object(
                    service,
                    "review_service_status",
                    side_effect=lambda config, key: {
                        "running": key != stopped_key,
                        "status": "running" if key != stopped_key else "stopped",
                    },
                ),
            ):
                result = service.project365_services_status(service.ServiceConfig())
            self.assertEqual(result["status"], "running")
            self.assertTrue(result["running"])

    def test_stop_detects_unmanaged_new_review_servers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = service.ServiceConfig(runtime_dir=Path(temporary))
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [], [], [12348], [12349]]),
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_descendant_pids", side_effect=[[], []]),
                mock.patch.object(service, "_terminate_pids", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)
            self.assertEqual(result["stopped_pids"], [12348, 12349])
            self.assertEqual(terminate.call_args_list, [mock.call([12348]), mock.call([12349])])

    def test_stop_failure_preserves_pid_record_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = service.ServiceConfig(runtime_dir=Path(temporary))
            service._write_service_pid_record(
                config.twitter_pid_path, 12349, config.host, config.twitter_port,
                f"http://{config.host}:{config.twitter_port}", config.runtime_dir / "twitter.log",
            )
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [], [], [], [12349]]),
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service.os, "getpgid", return_value=12349),
                mock.patch.object(service, "_terminate_process_group", return_value=False),
            ):
                result = service.stop_project365_servers(config)
            self.assertEqual(result["status"], "partial")
            self.assertTrue(result["running"])
            self.assertTrue(config.twitter_pid_path.exists())

    def test_discovery_ignores_global_matching_script_on_wrong_port(self) -> None:
        with (
            mock.patch.object(service, "_project_server_pids_from_ps", return_value=[12345]),
            mock.patch.object(service, "_port_has_project365_page", return_value=False),
            mock.patch.object(service, "_listening_pids", return_value=[]),
            mock.patch.object(service, "_pid_running", return_value=True),
        ):
            detected = service._project_server_pids(
                port=9879,
                include_any_project_server=True,
                server_scripts=(service.SWARM_MEDIA_SERVER_SCRIPT,),
                page_marker="Swarm photo order",
            )

        self.assertEqual(detected, [])

    def test_discovery_validates_listener_script_and_uses_marker_only_without_command(self) -> None:
        def detect(command: str) -> list[int]:
            with (
                mock.patch.object(service, "_project_server_pids_from_ps", return_value=[]),
                mock.patch.object(service, "_listening_pids", return_value=[12345]),
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_process_command", return_value=command),
                mock.patch.object(service, "_port_has_project365_page", return_value=True),
            ):
                return service._project_server_pids(
                    port=9879,
                    include_any_project_server=True,
                    server_scripts=(service.SWARM_MEDIA_SERVER_SCRIPT,),
                    page_marker="Swarm photo order",
                )

        self.assertEqual(detect("python unrelated.py"), [])
        self.assertEqual(detect(""), [12345])

    def test_stop_does_not_signal_live_stale_managed_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = service.ServiceConfig(runtime_dir=Path(temporary))
            service._write_service_pid_record(
                config.twitter_pid_path, 12349, config.host, config.twitter_port,
                f"http://{config.host}:{config.twitter_port}", config.runtime_dir / "twitter.log",
            )
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_terminate_process_group") as terminate_group,
                mock.patch.object(service, "_terminate_pids") as terminate_pids,
            ):
                result = service.stop_project365_servers(config)
            self.assertEqual(result["failed_pids"], [])
            terminate_group.assert_not_called()
            terminate_pids.assert_not_called()
            self.assertFalse(config.twitter_pid_path.exists())

    def test_stop_adopted_service_in_shared_group_only_terminates_it_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = service.ServiceConfig(runtime_dir=Path(temporary))
            service._write_service_pid_record(
                config.twitter_pid_path, 12349, config.host, config.twitter_port,
                f"http://{config.host}:{config.twitter_port}", config.runtime_dir / "twitter.log",
            )
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [], [], [], [12349]]),
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service.os, "getpgid", return_value=500),
                mock.patch.object(service, "_descendant_pids", return_value=[12350]),
                mock.patch.object(service, "_terminate_process_group") as terminate_group,
                mock.patch.object(service, "_terminate_pids", return_value=True) as terminate_pids,
            ):
                result = service.stop_project365_servers(config)
            self.assertEqual(result["status"], "stopped")
            terminate_group.assert_not_called()
            terminate_pids.assert_called_once()
            self.assertEqual(set(terminate_pids.call_args.args[0]), {12349, 12350})

    def test_failed_readiness_cleanup_retains_record_and_reports_cleanup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = service.ServiceConfig(runtime_dir=Path(temporary))
            process = mock.Mock(pid=12348)
            with (
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service.subprocess, "Popen", return_value=process),
                mock.patch.object(service, "_wait_for_review_service", side_effect=RuntimeError("not ready")),
                mock.patch.object(service, "_terminate_process_group", return_value=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    service.start_review_service(config, "twitter")
            record = json.loads(config.twitter_pid_path.read_text())
            self.assertEqual(record["pid"], 12348)

    def test_command_detection_excludes_review_export_and_metadata_jobs(self) -> None:
        self.assertTrue(service._is_project365_server_command("python3 twitter_dayone_workflow.py --source example serve"))
        self.assertFalse(service._is_project365_server_command("python3 twitter_dayone_workflow.py --source example export-test"))
        self.assertTrue(service._is_project365_server_command("python3 swarm_media_workflow.py --source example"))
        self.assertFalse(service._is_project365_server_command("python3 swarm_media_workflow.py --source example --prepare-digikam"))
        self.assertFalse(service._is_project365_server_command("python3 swarm_media_workflow.py --source example --import-digikam-people"))

    def test_status_detects_unmanaged_project365_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            with mock.patch.object(service, "_project_server_pids", return_value=[222]):
                result = service.service_status(config)

        self.assertTrue(result["running"])
        self.assertEqual(result["detected_pids"], [222])

    def test_project_server_command_matching_is_limited_to_python_servers(self) -> None:
        self.assertTrue(service._is_project365_server_command("python3 project365_control_app.py --port 8766"))
        self.assertTrue(service._is_project365_server_command("python3 project365_original_picker.py --port 8765"))
        self.assertFalse(service._is_project365_server_command("rg project365_control_app.py"))
        self.assertFalse(service._is_project365_server_command("python3 unrelated.py"))


if __name__ == "__main__":
    unittest.main()
