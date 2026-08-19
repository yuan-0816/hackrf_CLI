import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.backend.app import app, inspect_hackrf, runtime
from utils.config_manager import ConfigManager


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = ConfigManager(str(Path(self.temp_dir.name) / "config.json"))
        self.config.preset_add("測試點", 23.5, 121.0, 8.0)
        self.original_config = runtime.config
        runtime.config = self.config
        self.client = TestClient(app)

    def tearDown(self):
        runtime.config = self.original_config
        self.temp_dir.cleanup()

    def test_frontend_and_locale_are_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-i18n="app.title"', response.text)
        self.assertIn('id="software-version"', response.text)
        self.assertIn('id="system-software-version"', response.text)
        self.assertIn('data-i18n="systemInfo.title"', response.text)
        self.assertNotIn('id="hardware-serial"', response.text)
        self.assertNotIn('id="hardware-api"', response.text)
        self.assertNotIn('id="hardware-part-id"', response.text)
        self.assertNotIn('id="hardware-library"', response.text)
        self.assertNotIn('id="hardware-tool-version"', response.text)
        self.assertNotIn('id="hardware-manufacturer"', response.text)
        self.assertNotIn('id="hardware-supported-platform"', response.text)
        self.assertNotIn('data-i18n="app.subtitle"', response.text)
        self.assertIn('id="static-focus-position"', response.text)
        self.assertIn('id="traction-focus-start"', response.text)
        self.assertIn('id="traction-heading"', response.text)
        self.assertNotIn('id="traction-duration-hint"', response.text)
        self.assertIn('id="preset-focus-position"', response.text)
        self.assertIn('data-page="files"', response.text)
        self.assertIn('data-page="ephemeris"', response.text)
        self.assertIn('data-page-panel="ephemeris"', response.text)
        self.assertEqual(response.text.count('id="update-ephemeris"'), 1)
        self.assertIn('id="select-all-files"', response.text)
        self.assertIn('id="delete-files"', response.text)
        self.assertIn('class="task-output compact"', response.text)
        self.assertIn('data-i18n="task.fullLog"', response.text)
        self.assertIn('class="header-progress-area"', response.text)
        self.assertLess(response.text.index('class="header-progress-area"'), response.text.index('id="hardware-pill"'))
        self.assertIn('data-i18n-aria-label="actions.cancelGeneration"', response.text)
        self.assertNotIn('id="cancel-task-row"', response.text)
        self.assertIn('class="map-action"', response.text)
        frontend_script = self.client.get("/assets/app.js").text
        self.assertIn("function normalizeMapPoint", frontend_script)
        self.assertIn("function syncTractionDirectionFromHeading", frontend_script)
        self.assertIn("worldCopyJump: false", frontend_script)
        self.assertNotIn("noWrap: true", frontend_script)
        self.assertIn("async function deleteSelectedFiles", frontend_script)
        self.assertIn("payload.rf.status === 'running'", frontend_script)
        self.assertNotIn("['running', 'completed'].includes(payload.rf.status)", frontend_script)
        self.assertIn("&& !payload.rf.running", frontend_script)
        self.assertEqual(
            response.headers["cache-control"],
            "no-cache, no-store, must-revalidate",
        )
        locale = self.client.get("/assets/locales/zh-TW.json")
        self.assertEqual(locale.status_code, 200)
        self.assertEqual(locale.json()["app"]["name"], "GPS Spoofing Tools")
        self.assertEqual(locale.json()["nav"]["ephemeris"], "星曆更新")
        self.assertEqual(locale.json()["progress"]["ephemerisCompleted"], "星曆更新完成")
        self.assertEqual(
            locale.headers["cache-control"],
            "no-cache, no-store, must-revalidate",
        )

    def test_task_log_keeps_complete_history(self):
        original_task = runtime.task
        runtime.task = runtime._idle_task()
        try:
            for index in range(501):
                runtime.add_log(f"log-{index}")
            self.assertEqual(len(runtime.task["logs"]), 501)
            self.assertEqual(runtime.task["logs"][0]["message"], "log-0")
        finally:
            runtime.task = original_task

    def test_bin_files_can_be_listed_and_permanently_deleted(self):
        bin_dir = Path(self.temp_dir.name) / "gps"
        bin_dir.mkdir()
        first = bin_dir / "first.bin"
        second = bin_dir / "second.BIN"
        partial = bin_dir / ".generating.bin.part"
        ignored = bin_dir / "notes.txt"
        first.write_bytes(b"1234")
        second.write_bytes(b"123456")
        partial.write_bytes(b"unfinished")
        ignored.write_text("keep", encoding="utf-8")
        original_generated_file = runtime.generated_file
        original_task = runtime.task
        runtime.generated_file = str(first)
        runtime.task = runtime._idle_task()
        try:
            with patch("app.backend.app.GPS_BIN_DIR", bin_dir):
                response = self.client.get("/api/files")
                self.assertEqual(response.status_code, 200)
                files = {item["name"]: item for item in response.json()["files"]}
                self.assertEqual(set(files), {"first.bin", "second.BIN"})
                self.assertEqual(files["first.bin"]["size"], 4)
                self.assertTrue(files["first.bin"]["current"])

                response = self.client.request(
                    "DELETE",
                    "/api/files",
                    json={"names": ["first.bin", "second.BIN"]},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    set(response.json()["deleted"]),
                    {"first.bin", "second.BIN"},
                )
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(ignored.exists())
            self.assertTrue(partial.exists())
            self.assertIsNone(runtime.generated_file)
        finally:
            runtime.generated_file = original_generated_file
            runtime.task = original_task

    def test_bin_delete_rejects_path_traversal(self):
        bin_dir = Path(self.temp_dir.name) / "gps"
        bin_dir.mkdir()
        outside = Path(self.temp_dir.name) / "outside.bin"
        outside.write_bytes(b"keep")
        original_task = runtime.task
        runtime.task = runtime._idle_task()
        try:
            with patch("app.backend.app.GPS_BIN_DIR", bin_dir):
                response = self.client.request(
                    "DELETE",
                    "/api/files",
                    json={"names": ["../outside.bin"]},
                )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "invalid_bin_filename")
            self.assertTrue(outside.exists())
        finally:
            runtime.task = original_task

    def test_bin_list_hides_file_while_generation_is_writing_it(self):
        bin_dir = Path(self.temp_dir.name) / "gps"
        bin_dir.mkdir()
        completed = bin_dir / "completed.bin"
        generating = bin_dir / "generating.bin"
        completed.write_bytes(b"done")
        generating.write_bytes(b"partial")
        original_task = runtime.task
        original_task_output = runtime.task_output
        runtime.task = {
            **runtime._idle_task(),
            "id": "generation-task",
            "kind": "static_generation",
            "status": "running",
        }
        runtime.task_output = generating
        try:
            with patch("app.backend.app.GPS_BIN_DIR", bin_dir):
                response = self.client.get("/api/files")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["name"] for item in response.json()["files"]],
                ["completed.bin"],
            )
        finally:
            runtime.task = original_task
            runtime.task_output = original_task_output

    def test_bin_delete_rejects_file_being_transmitted(self):
        bin_dir = Path(self.temp_dir.name) / "gps"
        bin_dir.mkdir()
        signal = bin_dir / "active.bin"
        signal.write_bytes(b"active")
        original_generated_file = runtime.generated_file
        original_task = runtime.task
        original_rf_mode = runtime.rf_mode
        runtime.generated_file = str(signal)
        runtime.task = runtime._idle_task()
        runtime.rf_mode = "transmit"
        try:
            with (
                patch("app.backend.app.GPS_BIN_DIR", bin_dir),
                patch.object(runtime.hackrf, "is_running", return_value=True),
            ):
                response = self.client.request(
                    "DELETE",
                    "/api/files",
                    json={"names": ["active.bin"]},
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "bin_file_in_use")
            self.assertTrue(signal.exists())
        finally:
            runtime.generated_file = original_generated_file
            runtime.task = original_task
            runtime.rf_mode = original_rf_mode

    def test_software_metadata_comes_from_project_configuration(self):
        response = self.client.get("/api/software")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "name": "GPS Spoofing Tools",
                "version": "2.0",
                "version_label": "版本 2.0",
            },
        )
        self.assertEqual(app.version, "2.0")

    def test_hardware_refresh_detects_device_from_combined_output(self):
        result = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Found HackRF\nSerial number: 1234\n",
        )
        with patch("app.backend.app.subprocess.run", return_value=result):
            response = self.client.get("/api/hardware")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["connected"])

    def test_hardware_info_parses_device_details(self):
        output = (
            "hackrf_info version: 2024.02.1\n"
            "libhackrf version: 2024.02.1 (0.9)\n"
            "Found HackRF\n"
            "Index: 0\n"
            "Serial number: abc123\n"
            "Board ID Number: 4 (HackRF One)\n"
            "Firmware Version: 2024.02.1 (API:1.08)\n"
            "Part ID Number: 0xa000cb3c 0x005d4761\n"
            "Hardware Revision: r9\n"
            "Hardware appears to have been manufactured by Great Scott Gadgets.\n"
            "Hardware supported by installed firmware:\n"
            "    HackRF One\n"
        )
        result = SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch("app.backend.app.subprocess.run", return_value=result):
            hardware = inspect_hackrf()

        self.assertTrue(hardware["connected"])
        self.assertEqual(hardware["details"]["serial_number"], "abc123")
        self.assertEqual(hardware["details"]["board_id"], "4 (HackRF One)")
        self.assertEqual(hardware["details"]["api_version"], "1.08")
        self.assertEqual(hardware["details"]["hardware_revision"], "r9")
        self.assertEqual(hardware["details"]["firmware_version"], "2024.02.1")
        self.assertEqual(
            hardware["details"]["manufacturer"], "Great Scott Gadgets"
        )
        self.assertEqual(hardware["details"]["supported_platform"], "HackRF One")

    def test_hardware_info_identifies_dfu_mode(self):
        info_result = SimpleNamespace(
            returncode=1,
            stdout="No HackRF boards found.\n",
            stderr="",
        )
        usb_result = SimpleNamespace(
            returncode=0,
            stdout=(
                "Bus 003 Device 011: ID 1fc9:000c NXP Semiconductors "
                "LPC4330FET180 (device firmware upgrade mode)\n"
            ),
            stderr="",
        )
        with patch(
            "app.backend.app.subprocess.run",
            side_effect=[info_result, usb_result],
        ):
            hardware = inspect_hackrf()

        self.assertFalse(hardware["connected"])
        self.assertEqual(hardware["mode"], "dfu")
        self.assertEqual(hardware["error"], "hackrf_dfu_mode")

    def test_transmit_is_rejected_when_hackrf_is_not_connected(self):
        hardware = {
            "connected": False,
            "installed": True,
            "output": "No HackRF boards found.",
            "details": {},
            "error": "hackrf_not_connected",
        }
        with (
            patch("app.backend.app.inspect_hackrf", return_value=hardware),
            patch.object(runtime.hackrf, "is_running", return_value=False),
            patch.object(runtime.hackrf, "start_tx") as start_tx,
        ):
            response = self.client.post("/api/rf/transmit")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "hackrf_not_connected")
        start_tx.assert_not_called()

    def test_jamming_is_rejected_when_hackrf_is_not_connected(self):
        hardware = {
            "connected": False,
            "installed": True,
            "output": "No HackRF boards found.",
            "details": {},
            "error": "hackrf_not_connected",
        }
        with (
            patch("app.backend.app.inspect_hackrf", return_value=hardware),
            patch.object(runtime.hackrf, "is_running", return_value=False),
            patch.object(runtime.hackrf, "start_tx") as start_tx,
        ):
            response = self.client.post("/api/rf/jam")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "hackrf_not_connected")
        start_tx.assert_not_called()

    def test_transmit_duration_uses_iq_file_size_and_sample_rate(self):
        signal = Path(self.temp_dir.name) / "signal.bin"
        signal.write_bytes(b"0" * 200)
        original_file = runtime.generated_file
        original_values = (
            runtime.rf_mode,
            runtime.rf_state,
            runtime.rf_started_at,
            runtime.rf_started_monotonic,
            runtime.rf_finished_at,
            runtime.rf_expected_duration,
        )
        runtime.generated_file = str(signal)
        self.config.set("hackrf.sample_rate", 10)
        try:
            with (
                patch.object(runtime, "require_hackrf"),
                patch.object(runtime.hackrf, "is_running", return_value=False),
                patch.object(runtime.hackrf, "start_tx", return_value=True),
                patch("app.backend.app.time.monotonic", return_value=100.0),
            ):
                runtime.start_transmit()

            self.assertEqual(runtime.rf_expected_duration, 10.0)
            self.assertEqual(runtime.rf_state, "running")
        finally:
            runtime.generated_file = original_file
            (
                runtime.rf_mode,
                runtime.rf_state,
                runtime.rf_started_at,
                runtime.rf_started_monotonic,
                runtime.rf_finished_at,
                runtime.rf_expected_duration,
            ) = original_values

    def test_transmit_status_reports_progress_and_remaining_time(self):
        original_values = (
            runtime.rf_mode,
            runtime.rf_state,
            runtime.rf_started_at,
            runtime.rf_started_monotonic,
            runtime.rf_finished_at,
            runtime.rf_expected_duration,
        )
        runtime.rf_mode = "transmit"
        runtime.rf_state = "running"
        runtime.rf_started_at = "2026-08-12T00:00:00+00:00"
        runtime.rf_started_monotonic = 100.0
        runtime.rf_finished_at = None
        runtime.rf_expected_duration = 60.0
        try:
            with (
                patch.object(runtime.hackrf, "is_running", return_value=True),
                patch("app.backend.app.time.monotonic", return_value=115.0),
            ):
                rf = runtime.status()["rf"]

            self.assertEqual(rf["status"], "running")
            self.assertEqual(rf["duration"], 60.0)
            self.assertEqual(rf["elapsed"], 15.0)
            self.assertEqual(rf["remaining"], 45.0)
            self.assertEqual(rf["progress"], 25.0)
        finally:
            (
                runtime.rf_mode,
                runtime.rf_state,
                runtime.rf_started_at,
                runtime.rf_started_monotonic,
                runtime.rf_finished_at,
                runtime.rf_expected_duration,
            ) = original_values

    def test_transmit_status_becomes_completed_when_process_exits(self):
        original_values = (
            runtime.rf_mode,
            runtime.rf_state,
            runtime.rf_started_at,
            runtime.rf_started_monotonic,
            runtime.rf_finished_at,
            runtime.rf_expected_duration,
        )
        runtime.rf_mode = "transmit"
        runtime.rf_state = "running"
        runtime.rf_started_at = "2026-08-12T00:00:00+00:00"
        runtime.rf_started_monotonic = 100.0
        runtime.rf_finished_at = None
        runtime.rf_expected_duration = 60.0
        try:
            with (
                patch.object(runtime.hackrf, "is_running", return_value=False),
                patch("app.backend.app.time.monotonic", return_value=130.0),
            ):
                rf = runtime.status()["rf"]

            self.assertEqual(rf["status"], "completed")
            self.assertEqual(rf["remaining"], 0.0)
            self.assertEqual(rf["progress"], 100.0)
        finally:
            (
                runtime.rf_mode,
                runtime.rf_state,
                runtime.rf_started_at,
                runtime.rf_started_monotonic,
                runtime.rf_finished_at,
                runtime.rf_expected_duration,
            ) = original_values

    def test_new_generation_task_clears_completed_transmit_progress(self):
        original_values = (
            runtime.task,
            runtime.rf_mode,
            runtime.rf_state,
            runtime.rf_started_at,
            runtime.rf_started_monotonic,
            runtime.rf_finished_at,
            runtime.rf_expected_duration,
        )
        runtime.task = runtime._idle_task()
        runtime.rf_mode = "transmit"
        runtime.rf_state = "completed"
        runtime.rf_started_at = "2026-08-12T00:00:00+00:00"
        runtime.rf_started_monotonic = 100.0
        runtime.rf_finished_at = "2026-08-12T00:01:00+00:00"
        runtime.rf_expected_duration = 60.0
        finished = threading.Event()

        def target():
            finished.set()
            return {"ok": True}

        try:
            with patch.object(runtime.hackrf, "is_running", return_value=False):
                runtime.start_task("test_generation", target)
            self.assertTrue(finished.wait(timeout=1))
            deadline = time.monotonic() + 1
            while runtime.task["status"] != "completed" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(runtime.rf_mode)
            self.assertEqual(runtime.rf_state, "stopped")
            self.assertIsNone(runtime.rf_expected_duration)
        finally:
            (
                runtime.task,
                runtime.rf_mode,
                runtime.rf_state,
                runtime.rf_started_at,
                runtime.rf_started_monotonic,
                runtime.rf_finished_at,
                runtime.rf_expected_duration,
            ) = original_values

    def test_cancel_generation_stops_process_and_deletes_partial_file(self):
        original_values = (
            runtime.task,
            runtime.task_cancel_event,
            runtime.task_process,
            runtime.task_output,
        )
        partial_file = Path(self.temp_dir.name) / "partial.bin"
        started = threading.Event()
        released = threading.Event()
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        process.terminate.side_effect = released.set

        def target():
            partial_file.write_bytes(b"partial signal")
            with runtime.lock:
                runtime.task_output = partial_file
            runtime.set_task_process(process)
            started.set()
            released.wait(timeout=1)
            raise RuntimeError("generator_stopped")

        try:
            with patch.object(runtime.hackrf, "is_running", return_value=False):
                runtime.start_task("static_generation", target)
            self.assertTrue(started.wait(timeout=1))

            response = self.client.post("/api/tasks/cancel")

            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 1
            while runtime.task["status"] != "cancelled" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(runtime.task["status"], "cancelled")
            process.terminate.assert_called_once()
            self.assertFalse(partial_file.exists())
        finally:
            (
                runtime.task,
                runtime.task_cancel_event,
                runtime.task_process,
                runtime.task_output,
            ) = original_values

    def test_preset_can_be_edited_without_delete_and_recreate(self):
        response = self.client.put(
            "/api/presets/%E6%B8%AC%E8%A9%A6%E9%BB%9E",
            json={"new_name": "修改後", "lat": 24.0, "lon": 120.5, "alt": 12.0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.config.preset_get("測試點"))
        self.assertEqual(
            self.config.preset_get("修改後"),
            {"lat": 24.0, "lon": 120.5, "alt": 12.0},
        )

    def test_static_duration_can_exceed_five_minutes(self):
        with patch.object(runtime, "start_task", return_value="task-id") as start:
            response = self.client.post(
                "/api/generate/static",
                json={"lat": 23.5, "lon": 121.0, "alt": 8, "duration": 600},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(start.call_args.args[2].duration, 600)

    def test_generation_rejects_duration_that_exceeds_free_disk_space(self):
        disk_usage = SimpleNamespace(total=1000, used=900, free=100)
        with patch("app.backend.app.shutil.disk_usage", return_value=disk_usage):
            with self.assertRaisesRegex(RuntimeError, "insufficient_disk_space"):
                runtime.ensure_disk_capacity(Path(self.temp_dir.name), 101)

    def test_static_generation_uses_short_temporary_output_path(self):
        original_values = (
            runtime.task,
            runtime.task_output,
            runtime.generated_file,
        )
        runtime.task = runtime._idle_task()

        class FakeSimulator:
            def generate_bin(inner_self, **kwargs):
                actual = Path(
                    kwargs["output_bin"].replace(".bin", "_static.bin")
                )
                self.assertLess(len(str(actual).encode()), 100)
                self.assertEqual(actual.suffix, ".part")
                actual.write_bytes(b"complete")
                return str(actual)

        request = SimpleNamespace(
            lat=23.7951536,
            lon=121.3647061,
            alt=0.1,
            duration=800,
            time_mode="ephemeris",
        )
        try:
            with (
                patch("app.backend.app.PROJECT_ROOT", Path(self.temp_dir.name)),
                patch.object(runtime, "simulator", return_value=FakeSimulator()),
                patch.object(runtime, "ephemeris_path", return_value="test.nav"),
                patch.object(runtime, "ensure_disk_capacity"),
                patch.object(runtime, "validate_generated_duration"),
            ):
                result = runtime.generate_static(request)
            final_path = Path(result["path"])
            self.assertTrue(final_path.exists())
            self.assertEqual(
                final_path.name,
                "web_static_23.79515_121.36471_800s_static.bin",
            )
            self.assertFalse(any(final_path.parent.glob("*.part")))
        finally:
            (
                runtime.task,
                runtime.task_output,
                runtime.generated_file,
            ) = original_values

    def test_generation_always_checks_for_latest_ephemeris(self):
        project_root = Path(self.temp_dir.name)
        original_task = runtime.task
        runtime.task = runtime._idle_task()
        try:
            with (
                patch("app.backend.app.PROJECT_ROOT", project_root),
                patch(
                    "app.backend.app.fetch_latest_ephemeris",
                    return_value="today.nav",
                ) as fetch,
            ):
                result = runtime.ephemeris_path()

            self.assertEqual(result, "today.nav")
            fetch.assert_called_once_with(
                save_dir=str(project_root / "data" / "ephemeris"),
                max_files=5,
            )
        finally:
            runtime.task = original_task

    def test_truncated_generated_file_is_deleted(self):
        output = Path(self.temp_dir.name) / "truncated.bin"
        output.write_bytes(b"0" * 80)

        with self.assertRaisesRegex(RuntimeError, "generated_duration_mismatch"):
            runtime.validate_generated_duration(output, 5.0, 10)

        self.assertFalse(output.exists())

    def test_static_generation_request_starts_background_task(self):
        with patch.object(runtime, "start_task", return_value="task-id") as start:
            response = self.client.post(
                "/api/generate/static",
                json={
                    "lat": 23.5,
                    "lon": 121.0,
                    "alt": 8,
                    "duration": 300,
                    "time_mode": "shifted-now",
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"task_id": "task-id"})
        request = start.call_args.args[2]
        self.assertEqual(request.duration, 300)
        self.assertEqual(request.time_mode, "shifted-now")

    def test_traction_requires_enough_time_for_both_ramps(self):
        response = self.client.post(
            "/api/generate/traction",
            json={
                "lat": 23.5, "lon": 121.0, "alt": 8,
                "direction_lat": 24.0, "direction_lon": 121.0,
                "speed": 0.5, "ramp": 20, "duration": 50,
                "hold": 10, "final_hold": 5,
            },
        )
        self.assertEqual(response.status_code, 422)
        messages = " ".join(item["msg"] for item in response.json()["detail"])
        self.assertIn("traction_duration_too_short", messages)

    def test_bearing_for_north_is_zero(self):
        self.assertAlmostEqual(runtime.bearing(23.5, 121.0, 24.0, 121.0), 0.0)

    def test_settings_exposes_and_updates_all_existing_sections(self):
        payload = {
            "default_freq": 1575420000,
            "sample_rate": 2600000,
            "tx_gain": 30,
            "lna_gain": 24,
            "vga_gain": 22,
            "default_speed_mps": 0.8,
            "default_height": 15.0,
            "static_duration_s": 600,
            "traction_duration_s": 900,
            "update_rate_hz": 10.0,
            "drift_heading_deg": 45.0,
            "drift_alt_jitter_m": 0.2,
            "ephemeris_save_dir": "data/ephemeris",
            "ephemeris_max_files": 8,
        }
        response = self.client.put("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(self.config.get("hackrf.lna_gain"), 24)
        self.assertEqual(self.config.get("gps_sim.static_duration_s"), 600)
        self.assertEqual(self.config.get("gps_sim.traction_duration_s"), 900)
        self.assertEqual(self.config.get("ephemeris.max_files"), 8)

    def test_settings_rejects_values_outside_supported_limits(self):
        valid = {
            "default_freq": 1575420000,
            "sample_rate": 2600000,
            "tx_gain": 30,
            "lna_gain": 16,
            "vga_gain": 20,
            "default_speed_mps": 0.8,
            "default_height": 15.0,
            "static_duration_s": 90,
            "traction_duration_s": 180,
            "update_rate_hz": 10.0,
            "drift_heading_deg": 45.0,
            "drift_alt_jitter_m": 0.2,
            "ephemeris_save_dir": "data/ephemeris",
            "ephemeris_max_files": 8,
        }
        invalid_values = {
            "default_freq": [999999, 6000000001],
            "sample_rate": [999999, 20000001],
            "tx_gain": [-1, 48],
            "lna_gain": [-1, 10, 41],
            "vga_gain": [-1, 21, 63],
            "default_speed_mps": [0, 100.01],
            "default_height": [-1000.1, 100000.1],
            "static_duration_s": [0],
            "traction_duration_s": [54],
            "update_rate_hz": [9, 11],
            "drift_heading_deg": [-0.1, 360.1],
            "drift_alt_jitter_m": [-0.1, 1000.1],
            "ephemeris_max_files": [0, 101],
        }

        for field, values in invalid_values.items():
            for invalid in values:
                with self.subTest(field=field, invalid=invalid):
                    payload = {**valid, field: invalid}
                    response = self.client.put("/api/settings", json=payload)
                    self.assertEqual(response.status_code, 422)

    def test_task_status_contains_generation_progress(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("progress", response.json()["task"])
        self.assertIn("progress_phase", response.json()["task"])

    def test_generation_progress_uses_output_file_size(self):
        output = Path(self.temp_dir.name) / "signal.bin"
        output.write_bytes(b"0" * 50)
        original_task = runtime.task
        runtime.task = runtime._idle_task()
        stop = threading.Event()
        monitor = threading.Thread(
            target=runtime.monitor_output,
            args=(output, 100, stop),
        )
        monitor.start()
        time.sleep(0.3)
        stop.set()
        monitor.join(timeout=1)
        try:
            self.assertEqual(runtime.task["progress"], 52.5)
            self.assertEqual(runtime.task["progress_phase"], "generating")
        finally:
            runtime.task = original_task


if __name__ == "__main__":
    unittest.main()
