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
            config.pid_path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
            with mock.patch.object(service, "_pid_running", return_value=True):
                with mock.patch.object(service, "_project_server_pids", return_value=[]):
                    with mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate:
                        result = service.stop_project365_servers(config)

            self.assertFalse(config.pid_path.exists())

        self.assertEqual(result["status"], "stopped")
        terminate.assert_called_once_with(12345)

    def test_stop_removes_all_pid_records_and_stops_all_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            config.runtime_dir.mkdir(exist_ok=True)
            config.pid_path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
            config.video_pid_path.write_text(json.dumps({"pid": 12346}), encoding="utf-8")
            config.swarm_backfill_pid_path.write_text(json.dumps({"pid": 12347}), encoding="utf-8")
            with (
                mock.patch.object(service, "_pid_running", return_value=True),
                mock.patch.object(service, "_project_server_pids", return_value=[]),
                mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
            ):
                result = service.stop_project365_servers(config)

            self.assertFalse(config.pid_path.exists())
            self.assertFalse(config.video_pid_path.exists())
            self.assertFalse(config.swarm_backfill_pid_path.exists())

        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            terminate.call_args_list,
            [mock.call(12345), mock.call(12346), mock.call(12347)],
        )

    def test_stop_detects_unmanaged_video_server_on_video_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = service.ServiceConfig(runtime_dir=Path(temp_dir))
            with (
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [12346], []]) as detect,
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
                mock.patch.object(service, "_project_server_pids", side_effect=[[], [], [12347]]) as detect,
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

    def test_start_project365_services_starts_control_video_and_swarm_backfill(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(
                service,
                "start_control_panel",
                return_value={"status": "started", "running": True, "pid": 1},
            ) as start_control,
            mock.patch.object(
                service,
                "start_video_memory_review",
                return_value={"status": "started", "running": True, "pid": 2},
            ) as start_video,
            mock.patch.object(
                service,
                "start_swarm_backfill_review",
                return_value={"status": "started", "running": True, "pid": 3},
            ) as start_swarm,
        ):
            result = service.start_project365_services(config)

        self.assertTrue(result["running"])
        self.assertEqual(result["status"], "started")
        start_control.assert_called_once_with(config)
        start_video.assert_called_once_with(config)
        start_swarm.assert_called_once_with(config)

    def test_start_project365_services_rolls_back_new_control_when_video_fails(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(
                service,
                "start_control_panel",
                return_value={"status": "started", "running": True, "pid": 12345},
            ),
            mock.patch.object(service, "start_video_memory_review", side_effect=RuntimeError("video failed")),
            mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
            mock.patch.object(service, "_remove_pid_record") as remove_record,
        ):
            with self.assertRaisesRegex(RuntimeError, "video failed"):
                service.start_project365_services(config)

        terminate.assert_called_once_with(12345)
        remove_record.assert_called_once_with(config.pid_path)

    def test_start_project365_services_rolls_back_new_services_when_swarm_fails(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(
                service,
                "start_control_panel",
                return_value={"status": "started", "running": True, "pid": 12345},
            ),
            mock.patch.object(
                service,
                "start_video_memory_review",
                return_value={"status": "started", "running": True, "pid": 12346},
            ),
            mock.patch.object(
                service,
                "start_swarm_backfill_review",
                side_effect=RuntimeError("swarm failed"),
            ),
            mock.patch.object(service, "_terminate_process_group", return_value=True) as terminate,
            mock.patch.object(service, "_remove_pid_record") as remove_record,
        ):
            with self.assertRaisesRegex(RuntimeError, "swarm failed"):
                service.start_project365_services(config)

        self.assertEqual(terminate.call_args_list, [mock.call(12346), mock.call(12345)])
        self.assertEqual(
            remove_record.call_args_list,
            [mock.call(config.video_pid_path), mock.call(config.pid_path)],
        )

    def test_project365_services_status_is_partial_when_video_is_stopped(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(service, "service_status", return_value={"running": True}),
            mock.patch.object(service, "video_service_status", return_value={"running": False}),
            mock.patch.object(service, "swarm_backfill_service_status", return_value={"running": True}),
        ):
            result = service.project365_services_status(config)

        self.assertFalse(result["running"])
        self.assertEqual(result["status"], "partial")

    def test_project365_services_status_is_partial_when_swarm_backfill_is_stopped(self) -> None:
        config = service.ServiceConfig()
        with (
            mock.patch.object(service, "service_status", return_value={"running": True}),
            mock.patch.object(service, "video_service_status", return_value={"running": True}),
            mock.patch.object(service, "swarm_backfill_service_status", return_value={"running": False}),
        ):
            result = service.project365_services_status(config)

        self.assertFalse(result["running"])
        self.assertEqual(result["status"], "partial")

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
