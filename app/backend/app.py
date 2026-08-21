from __future__ import annotations

import contextlib
import datetime
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = PROJECT_ROOT / "app" / "frontend"
GPS_BIN_DIR = PROJECT_ROOT / "data" / "fake_signal" / "gps"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fake_gps import FakeGPS
from hackrf_wrapper import HackRFCLI
from utils.config_manager import ConfigManager
from utils.executable_paths import is_windows, resolve_hackrf_executable
from utils.get_latest_brdc import fetch_latest_ephemeris

GPS_L1_FREQUENCY_HZ = 1_575_420_000
DISK_RESERVE_BYTES = 256 * 1024 * 1024
MAX_SIGNAL_DURATION_S = 86_400
NOISE_SIZE_BYTES = 4 * 1024 * 1024


def project_metadata():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        configuration = tomllib.load(project_file)
    project = configuration["project"]
    return {
        "name": configuration["tool"]["gps-spoofing-tools"]["display-name"],
        "version": project["version"],
        "version_label": f"版本 {project['version']}",
    }


PROJECT_METADATA = project_metadata()


def inspect_hackrf():
    try:
        result = subprocess.run(
            [resolve_hackrf_executable("hackrf_info")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return {
            "connected": False,
            "installed": False,
            "mode": "unavailable",
            "output": "",
            "details": {},
            "error": "hackrf_tools_missing",
        }
    except subprocess.TimeoutExpired:
        return {
            "connected": False,
            "installed": True,
            "mode": "unavailable",
            "output": "",
            "details": {},
            "error": "hardware_info_timeout",
        }

    output = (result.stdout + result.stderr).strip()

    def match(pattern):
        found = re.search(pattern, output, flags=re.MULTILINE)
        return found.group(1).strip() if found else None

    firmware = match(r"^Firmware Version:\s*(.*?)\s*(?:\(API:|$)")
    api_version = match(r"^Firmware Version:.*\(API:\s*([^)]+)\)")
    manufacturer = (
        "Great Scott Gadgets"
        if "manufactured by Great Scott Gadgets" in output
        else None
    )
    details = {
        "tool_version": match(r"^hackrf_info version:\s*(.+)$"),
        "library_version": match(r"^libhackrf version:\s*(.+)$"),
        "index": match(r"^Index:\s*(.+)$"),
        "serial_number": match(r"^Serial number:\s*(.+)$"),
        "board_id": match(r"^Board ID Number:\s*(.+)$"),
        "firmware_version": firmware,
        "api_version": api_version,
        "part_id": match(r"^Part ID Number:\s*(.+)$"),
        "hardware_revision": match(r"^Hardware Revision:\s*(.+)$"),
        "manufacturer": manufacturer,
        "supported_platform": match(
            r"^Hardware supported by installed firmware:\s*\n\s*(.+)$"
        ),
    }
    connected = result.returncode == 0 and (
        "Found HackRF" in output or details["serial_number"] is not None
    )
    dfu_mode = False
    if not connected:
        try:
            usb_command = (
                ["pnputil", "/enum-devices", "/connected", "/deviceids"]
                if is_windows()
                else ["lsusb"]
            )
            usb_result = subprocess.run(
                usb_command,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            usb_output = (usb_result.stdout + usb_result.stderr).lower()
            dfu_mode = (
                "1fc9:000c" in usb_output
                or "vid_1fc9&pid_000c" in usb_output
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    mode = "normal" if connected else "dfu" if dfu_mode else "unavailable"
    error = None if connected else (
        "hackrf_dfu_mode" if dfu_mode else "hackrf_not_connected"
    )
    return {
        "connected": connected,
        "installed": True,
        "mode": mode,
        "output": output,
        "details": details,
        "error": error,
    }


class CoordinateModel(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt: float

    @field_validator("lat", "lon", "alt")
    @classmethod
    def finite_coordinates(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("coordinate_not_finite")
        return value


class StaticGenerationRequest(CoordinateModel):
    duration: int = Field(default=60, ge=1, le=MAX_SIGNAL_DURATION_S)
    time_mode: Literal["ephemeris", "shifted-now"] = "ephemeris"


class TractionGenerationRequest(CoordinateModel):
    direction_lat: float = Field(ge=-90, le=90)
    direction_lon: float = Field(ge=-180, le=180)
    speed: float = Field(default=0.5, gt=0)
    ramp: float = Field(default=20.0, gt=0)
    duration: float = Field(default=120.0, gt=0, le=MAX_SIGNAL_DURATION_S)
    hold: float = Field(default=10.0, ge=0)
    final_hold: float = Field(default=5.0, ge=0)
    time_mode: Literal["ephemeris", "shifted-now"] = "ephemeris"

    @model_validator(mode="after")
    def validate_profile_duration(self):
        active = self.duration - self.hold - self.final_hold
        if active < 2 * self.ramp:
            raise ValueError("traction_duration_too_short")
        return self


class PresetCreateRequest(CoordinateModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("preset_name_empty")
        return value


class PresetUpdateRequest(CoordinateModel):
    new_name: str | None = Field(default=None, max_length=80)


class SettingsRequest(BaseModel):
    default_freq: int = Field(ge=1_000_000, le=6_000_000_000)
    sample_rate: int = Field(ge=1_000_000, le=20_000_000)
    tx_gain: int = Field(ge=0, le=47)
    lna_gain: int = Field(ge=0, le=40)
    vga_gain: int = Field(ge=0, le=62)
    default_speed_mps: float = Field(ge=0.01, le=100)
    default_height: float = Field(ge=-1_000, le=100_000)
    static_duration_s: int = Field(ge=1, le=MAX_SIGNAL_DURATION_S)
    traction_duration_s: int = Field(ge=55, le=MAX_SIGNAL_DURATION_S)
    update_rate_hz: float
    drift_heading_deg: float = Field(ge=0, le=360)
    drift_alt_jitter_m: float = Field(ge=0, le=1_000)
    ephemeris_save_dir: str = Field(min_length=1, max_length=240)
    ephemeris_max_files: int = Field(ge=1, le=100)

    @field_validator("lna_gain")
    @classmethod
    def valid_lna_step(cls, value: int) -> int:
        if value % 8 != 0:
            raise ValueError("invalid_lna_step")
        return value

    @field_validator("vga_gain")
    @classmethod
    def valid_vga_step(cls, value: int) -> int:
        if value % 2 != 0:
            raise ValueError("invalid_vga_step")
        return value

    @field_validator("update_rate_hz")
    @classmethod
    def supported_update_rate(cls, value: float) -> float:
        if not math.isclose(value, FakeGPS.MOTION_RATE_HZ):
            raise ValueError("unsupported_update_rate")
        return value


class BinDeleteRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=1_000)


class LogBuffer(io.TextIOBase):
    def __init__(self, callback):
        self.callback = callback
        self.pending = ""

    def write(self, value):
        if not value:
            return 0
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.callback(line)
        return len(value)

    def flush(self):
        if self.pending.strip():
            self.callback(self.pending)
        self.pending = ""


class RuntimeManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = ConfigManager()
        self.hackrf = HackRFCLI()
        self.task = self._idle_task()
        self.rf_mode: str | None = None
        self.rf_state = "stopped"
        self.rf_started_at: str | None = None
        self.rf_started_monotonic: float | None = None
        self.rf_finished_at: str | None = None
        self.rf_expected_duration: float | None = None
        self.generated_file: str | None = None
        self.noise_file: str | None = None
        self.task_cancel_event = threading.Event()
        self.task_process: subprocess.Popen | None = None
        self.task_output: Path | None = None

    @staticmethod
    def _idle_task():
        return {
            "id": None,
            "kind": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "logs": [],
            "progress": 0,
            "progress_phase": None,
            "result": None,
            "error": None,
        }

    def add_log(self, message: str):
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self.lock:
            self.task["logs"].append({"time": timestamp, "message": message})

    def set_progress(self, progress: float, phase: str):
        with self.lock:
            self.task["progress"] = max(0, min(100, round(progress, 1)))
            self.task["progress_phase"] = phase

    def monitor_output(self, path: Path, expected_size: int, stop: threading.Event):
        while not stop.wait(0.25):
            if path.exists() and expected_size > 0:
                ratio = path.stat().st_size / expected_size
                self.set_progress(10 + min(ratio, 1.0) * 85, "generating")

    @staticmethod
    def ensure_disk_capacity(directory: Path, expected_size: int):
        free_space = shutil.disk_usage(directory).free
        usable_space = max(0, free_space - DISK_RESERVE_BYTES)
        if expected_size > usable_space:
            raise RuntimeError("insufficient_disk_space")

    @staticmethod
    def validate_generated_duration(path: Path, requested_duration: float, sample_rate: int):
        actual_duration = path.stat().st_size / (sample_rate * 2)
        if actual_duration + 0.2 < requested_duration:
            RuntimeManager.remove_file(path)
            raise RuntimeError("generated_duration_mismatch")

    @staticmethod
    def remove_file(path: Path, attempts: int = 10):
        """Remove a file, tolerating short-lived Windows sharing races."""
        for attempt in range(attempts):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05)

    def ensure_idle(self):
        if self.task["status"] in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="task_busy")
        if self.hackrf.is_running():
            raise HTTPException(status_code=409, detail="rf_busy")

    def set_task_process(self, process):
        with self.lock:
            self.task_process = process
            should_cancel = process is not None and self.task_cancel_event.is_set()
        if should_cancel and process.poll() is None:
            process.terminate()

    @staticmethod
    def require_hackrf():
        hardware = inspect_hackrf()
        if not hardware["installed"]:
            raise HTTPException(status_code=503, detail="hackrf_tools_missing")
        if not hardware["connected"]:
            detail = hardware.get("error") or "hackrf_not_connected"
            raise HTTPException(status_code=503, detail=detail)
        return hardware

    def start_task(self, kind: str, target, *args):
        with self.lock:
            self.ensure_idle()
            if self.rf_state == "completed":
                self.rf_mode = None
                self.rf_state = "stopped"
                self.rf_started_at = None
                self.rf_started_monotonic = None
                self.rf_finished_at = None
                self.rf_expected_duration = None
            self.task = {
                **self._idle_task(),
                "id": str(uuid.uuid4()),
                "kind": kind,
                "status": "queued",
                "started_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            }
            self.task_cancel_event = threading.Event()
            self.task_process = None
            self.task_output = None
            thread = threading.Thread(
                target=self._run_task,
                args=(target, args),
                daemon=True,
            )
            thread.start()
            return self.task["id"]

    def _run_task(self, target, args):
        with self.lock:
            self.task["status"] = "running"
        try:
            result = target(*args)
            with self.lock:
                if self.task_cancel_event.is_set():
                    self.task["status"] = "cancelled"
                    self.task["progress_phase"] = "cancelled"
                else:
                    self.task["status"] = "completed"
                    self.task["result"] = result
                    self.task["progress"] = 100
                    self.task["progress_phase"] = "completed"
        except Exception as exc:
            with self.lock:
                cancelled = self.task_cancel_event.is_set()
            if cancelled:
                with self.lock:
                    self.task["status"] = "cancelled"
                    self.task["progress_phase"] = "cancelled"
            else:
                self.add_log(f"{type(exc).__name__}: {exc}")
                with self.lock:
                    self.task["status"] = "failed"
                    self.task["error"] = str(exc)
                    self.task["progress_phase"] = "failed"
        finally:
            with self.lock:
                output = (
                    self.task_output
                    if self.task["status"] != "completed"
                    else None
                )
                self.task_process = None
                self.task_output = None
                self.task["finished_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
            if output:
                self.remove_file(output)

    def cancel_task(self):
        with self.lock:
            if self.task["status"] not in {"queued", "running"}:
                raise HTTPException(status_code=409, detail="task_not_running")
            if self.task["kind"] not in {"static_generation", "traction_generation"}:
                raise HTTPException(status_code=409, detail="task_not_cancellable")
            self.task_cancel_event.set()
            process = self.task_process
            output = self.task_output
            self.task["progress_phase"] = "cancelling"
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if output:
            self.remove_file(output)

    def simulator(self):
        return FakeGPS(
            target_speed_mps=self.config.get("gps_sim.default_speed_mps", 5.0),
            default_height=self.config.get("gps_sim.default_height", 100.0),
            update_rate_hz=self.config.get("gps_sim.update_rate_hz", 10.0),
        )

    def update_ephemeris(self):
        self.set_progress(5, "preparing")
        self.add_log("ephemeris_update_started")
        capture = LogBuffer(self.add_log)
        with contextlib.redirect_stdout(capture):
            path = fetch_latest_ephemeris(
                save_dir=str(
                    PROJECT_ROOT
                    / self.config.get("ephemeris.save_dir", "data/ephemeris")
                ),
                force=True,
                max_files=self.config.get("ephemeris.max_files", 5),
            )
        capture.flush()
        if not path:
            raise RuntimeError("ephemeris_update_failed")
        bounds = FakeGPS._get_ephemeris_time_bounds(path)
        result = {"path": path, "earliest": None, "latest": None}
        if bounds:
            result["earliest"] = bounds[0].isoformat()
            result["latest"] = bounds[1].isoformat()
        self.add_log("ephemeris_update_completed")
        self.set_progress(100, "completed")
        return result

    def current_ephemeris(self):
        directory = PROJECT_ROOT / self.config.get(
            "ephemeris.save_dir", "data/ephemeris"
        )
        candidates = [
            path
            for path in directory.glob("brdc*.??n")
            if path.is_file() and not path.name.endswith(".download")
        ]
        if not candidates:
            return {"path": None, "earliest": None, "latest": None}
        path = max(candidates, key=lambda item: item.stat().st_mtime)
        bounds = FakeGPS._get_ephemeris_time_bounds(str(path))
        return {
            "path": str(path),
            "earliest": bounds[0].isoformat() if bounds else None,
            "latest": bounds[1].isoformat() if bounds else None,
        }

    def ephemeris_path(self):
        self.set_progress(5, "checkingEphemeris")
        self.add_log("ephemeris_check_started")
        path = fetch_latest_ephemeris(
            save_dir=str(
                PROJECT_ROOT
                / self.config.get("ephemeris.save_dir", "data/ephemeris")
            ),
            max_files=self.config.get("ephemeris.max_files", 5),
        )
        if not path:
            self.add_log("ephemeris_check_failed")
            raise RuntimeError("ephemeris_update_failed")
        self.add_log(f"ephemeris_ready: {path}")
        self.set_progress(7, "preparing")
        return path

    def generate_static(self, request: StaticGenerationRequest):
        output_dir = PROJECT_ROOT / "data" / "fake_signal" / "gps"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = (
            f"web_static_{request.lat:.5f}_{request.lon:.5f}_{request.duration}s"
        )
        # gps-sdr-sim uses a 100-byte C buffer for the output path, so the
        # temporary basename must remain short even when the project path is long.
        output = output_dir / f".tmp_{uuid.uuid4().hex[:8]}.bin.part"
        actual_output = Path(str(output).replace(".bin", "_static.bin"))
        final_output = output_dir / f"{stem}_static.bin"
        actual_output.unlink(missing_ok=True)
        with self.lock:
            self.task_output = actual_output
        self.add_log("static_generation_started")
        self.set_progress(5, "preparing")
        stop_monitor = threading.Event()
        sample_rate = self.config.get("hackrf.sample_rate", 2_600_000)
        expected_size = int(request.duration * sample_rate * 2)
        self.ensure_disk_capacity(output_dir, expected_size)
        monitor = threading.Thread(
            target=self.monitor_output,
            args=(actual_output, expected_size, stop_monitor),
            daemon=True,
        )
        monitor.start()
        capture = LogBuffer(self.add_log)
        try:
            with contextlib.redirect_stdout(capture):
                result = self.simulator().generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=self.ephemeris_path(),
                    static_mode=True,
                    manual_coords=(request.lat, request.lon, request.alt),
                    drift_duration_s=request.duration,
                    time_mode=request.time_mode,
                    sample_rate=sample_rate,
                    process_callback=self.set_task_process,
                )
        finally:
            stop_monitor.set()
            monitor.join(timeout=1)
        capture.flush()
        if not result or not Path(result).exists():
            raise RuntimeError("static_generation_failed")
        self.validate_generated_duration(Path(result), request.duration, sample_rate)
        Path(result).replace(final_output)
        self.generated_file = str(final_output)
        info = self._file_info(final_output)
        self.add_log("static_generation_completed")
        return info

    @staticmethod
    def bearing(start_lat, start_lon, end_lat, end_lon):
        lat1 = math.radians(start_lat)
        lat2 = math.radians(end_lat)
        delta_lon = math.radians(end_lon - start_lon)
        y = math.sin(delta_lon) * math.cos(lat2)
        x = (
            math.cos(lat1) * math.sin(lat2)
            - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
        )
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def generate_traction(self, request: TractionGenerationRequest):
        output_dir = PROJECT_ROOT / "data" / "fake_signal" / "gps"
        csv_dir = PROJECT_ROOT / "data" / "fake_path"
        output_dir.mkdir(parents=True, exist_ok=True)
        csv_dir.mkdir(parents=True, exist_ok=True)
        stem = f"web_traction_{request.lat:.5f}_{request.lon:.5f}"
        # Keep this path below gps-sdr-sim's 100-byte output-path buffer.
        output = output_dir / f".tmp_{uuid.uuid4().hex[:8]}.bin.part"
        final_output = output_dir / f"{stem}.bin"
        csv_file = csv_dir / f"{stem}.csv"
        heading = self.bearing(
            request.lat,
            request.lon,
            request.direction_lat,
            request.direction_lon,
        )
        simulator = self.simulator()
        self.add_log("traction_profile_started")
        self.set_progress(5, "preparing")
        output.unlink(missing_ok=True)
        with self.lock:
            self.task_output = output
        sample_rate = self.config.get("hackrf.sample_rate", 2_600_000)
        expected_size = int(request.duration * sample_rate * 2)
        self.ensure_disk_capacity(output_dir, expected_size)
        capture = LogBuffer(self.add_log)
        with contextlib.redirect_stdout(capture):
            ok = simulator._generate_traction_csv(
                csv_file=str(csv_file),
                start_lat=request.lat,
                start_lon=request.lon,
                start_alt=request.alt,
                heading_deg=heading,
                target_speed_mps=request.speed,
                ramp_duration_s=request.ramp,
                total_duration_s=request.duration,
                hold_duration_s=request.hold,
                final_hold_duration_s=request.final_hold,
            )
            if not ok:
                raise RuntimeError("traction_profile_failed")
        stop_monitor = threading.Event()
        monitor = threading.Thread(
            target=self.monitor_output,
            args=(output, expected_size, stop_monitor),
            daemon=True,
        )
        monitor.start()
        try:
            with contextlib.redirect_stdout(capture):
                result = simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=self.ephemeris_path(),
                    static_mode=False,
                    csv_file=str(csv_file),
                    time_mode=request.time_mode,
                    sample_rate=sample_rate,
                    process_callback=self.set_task_process,
                )
        finally:
            stop_monitor.set()
            monitor.join(timeout=1)
        capture.flush()
        if not result or not Path(result).exists():
            raise RuntimeError("traction_generation_failed")
        self.validate_generated_duration(Path(result), request.duration, sample_rate)
        Path(result).replace(final_output)
        self.generated_file = str(final_output)
        info = self._file_info(final_output)
        info.update({"heading": heading, "csv_file": str(csv_file)})
        self.add_log("traction_generation_completed")
        return info

    @staticmethod
    def _file_info(path):
        file_path = Path(path)
        return {
            "path": str(file_path),
            "name": file_path.name,
            "size": file_path.stat().st_size,
        }

    def list_bin_files(self):
        GPS_BIN_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock:
            generation_active = (
                self.task["status"] in {"queued", "running"}
                and self.task["kind"]
                in {"static_generation", "traction_generation"}
            )
            active_output = (
                self.task_output.resolve()
                if generation_active and self.task_output
                else None
            )
        files = []
        for path in GPS_BIN_DIR.iterdir():
            if path.suffix.lower() != ".bin" or not path.is_file() or path.is_symlink():
                continue
            if active_output and path.resolve() == active_output:
                continue
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=datetime.timezone.utc,
                    ).isoformat(),
                    "current": bool(
                        self.generated_file
                        and Path(self.generated_file).resolve() == path.resolve()
                    ),
                }
            )
        return sorted(files, key=lambda item: item["modified_at"], reverse=True)

    def delete_bin_files(self, names: list[str]):
        with self.lock:
            if self.task["status"] in {"queued", "running"}:
                raise HTTPException(status_code=409, detail="task_busy")
            directory = GPS_BIN_DIR.resolve()
            targets = []
            for name in dict.fromkeys(names):
                if Path(name).name != name or Path(name).suffix.lower() != ".bin":
                    raise HTTPException(status_code=400, detail="invalid_bin_filename")
                target = GPS_BIN_DIR / name
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or target.resolve().parent != directory
                ):
                    raise HTTPException(status_code=404, detail="bin_file_missing")
                targets.append(target)

            active_file = (
                Path(self.generated_file).resolve()
                if self.rf_mode == "transmit"
                and self.hackrf.is_running()
                and self.generated_file
                else None
            )
            if active_file and any(target.resolve() == active_file for target in targets):
                raise HTTPException(status_code=409, detail="bin_file_in_use")

            deleted_paths = {target.resolve() for target in targets}
            for target in targets:
                target.unlink()
            if (
                self.generated_file
                and Path(self.generated_file).resolve() in deleted_paths
            ):
                self.generated_file = None
            return [target.name for target in targets]

    def start_transmit(self):
        with self.lock:
            self.ensure_idle()
            self.require_hackrf()
            if not self.generated_file or not Path(self.generated_file).exists():
                raise HTTPException(status_code=404, detail="generated_file_missing")
            ok = self.hackrf.start_tx(
                filename=self.generated_file,
                freq_hz=self.config.get("hackrf.default_freq", GPS_L1_FREQUENCY_HZ),
                sample_rate_hz=self.config.get("hackrf.sample_rate", 2_600_000),
                tx_gain=self.config.get("hackrf.tx_gain", 30),
                repeat=False,
            )
            if not ok:
                raise HTTPException(status_code=500, detail="transmit_start_failed")
            self.rf_mode = "transmit"
            self.rf_state = "running"
            self.rf_started_at = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self.rf_started_monotonic = time.monotonic()
            self.rf_finished_at = None
            sample_rate = self.config.get("hackrf.sample_rate", 2_600_000)
            self.rf_expected_duration = (
                Path(self.generated_file).stat().st_size / (sample_rate * 2)
            )

    def start_jam(self):
        with self.lock:
            self.ensure_idle()
            self.require_hackrf()
            noise_path = Path(tempfile.gettempdir()) / "hackrf_web_jam_noise.bin"
            noise_path.write_bytes(os.urandom(NOISE_SIZE_BYTES))
            ok = self.hackrf.start_tx(
                filename=str(noise_path),
                freq_hz=GPS_L1_FREQUENCY_HZ,
                sample_rate_hz=self.config.get("hackrf.sample_rate", 2_600_000),
                tx_gain=self.config.get("hackrf.tx_gain", 30),
                repeat=True,
            )
            if not ok:
                noise_path.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail="jam_start_failed")
            self.noise_file = str(noise_path)
            self.rf_mode = "jam"
            self.rf_state = "running"
            self.rf_started_at = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self.rf_started_monotonic = time.monotonic()
            self.rf_finished_at = None
            self.rf_expected_duration = None

    def stop_rf(self):
        with self.lock:
            self.hackrf.stop()
            if self.noise_file:
                Path(self.noise_file).unlink(missing_ok=True)
            self.noise_file = None
            self.rf_mode = None
            self.rf_state = "stopped"
            self.rf_started_at = None
            self.rf_started_monotonic = None
            self.rf_finished_at = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self.rf_expected_duration = None

    def status(self):
        with self.lock:
            rf_running = self.hackrf.is_running()
            elapsed = 0.0
            remaining = None
            progress = None
            if self.rf_started_monotonic is not None:
                elapsed = max(0.0, time.monotonic() - self.rf_started_monotonic)
            if self.rf_mode == "transmit" and self.rf_expected_duration is not None:
                duration = self.rf_expected_duration
                if not rf_running and self.rf_state == "running":
                    self.rf_state = "completed"
                    self.rf_finished_at = datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat()
                if self.rf_state == "completed":
                    elapsed = duration
                else:
                    elapsed = min(elapsed, duration)
                remaining = max(0.0, duration - elapsed)
                progress = 100.0 if duration <= 0 else min(
                    100.0, elapsed / duration * 100
                )
            elif self.rf_mode == "jam" and not rf_running:
                self.rf_mode = None
                self.rf_state = "stopped"
            return {
                "task": dict(self.task),
                "rf": {
                    "running": rf_running,
                    "mode": self.rf_mode,
                    "status": self.rf_state,
                    "started_at": self.rf_started_at,
                    "finished_at": self.rf_finished_at,
                    "duration": self.rf_expected_duration,
                    "elapsed": elapsed,
                    "remaining": remaining,
                    "progress": progress,
                },
                "generated_file": (
                    self._file_info(self.generated_file)
                    if self.generated_file and Path(self.generated_file).exists()
                    else None
                ),
            }


runtime = RuntimeManager()
app = FastAPI(
    title=PROJECT_METADATA["name"],
    version=PROJECT_METADATA["version"],
    docs_url="/api/docs",
)
app.mount("/assets", StaticFiles(directory=FRONTEND_ROOT), name="assets")


@app.middleware("http")
async def disable_frontend_cache(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/")
def index():
    return FileResponse(FRONTEND_ROOT / "index.html")


@app.get("/api/status")
def get_status():
    return runtime.status()


@app.get("/api/software")
def software_info():
    return PROJECT_METADATA


@app.get("/api/hardware")
def hardware_info():
    return inspect_hackrf()


@app.post("/api/ephemeris/update", status_code=202)
def update_ephemeris():
    return {"task_id": runtime.start_task("ephemeris", runtime.update_ephemeris)}


@app.get("/api/ephemeris")
def get_ephemeris():
    return runtime.current_ephemeris()


@app.get("/api/settings")
def get_settings():
    return {
        "default_freq": runtime.config.get("hackrf.default_freq", GPS_L1_FREQUENCY_HZ),
        "sample_rate": runtime.config.get("hackrf.sample_rate", 2_600_000),
        "tx_gain": runtime.config.get("hackrf.tx_gain", 30),
        "lna_gain": runtime.config.get("hackrf.lna_gain", 16),
        "vga_gain": runtime.config.get("hackrf.vga_gain", 20),
        "default_speed_mps": runtime.config.get("gps_sim.default_speed_mps", 5.0),
        "default_height": runtime.config.get("gps_sim.default_height", 100.0),
        "static_duration_s": runtime.config.get("gps_sim.static_duration_s", 60),
        "traction_duration_s": runtime.config.get("gps_sim.traction_duration_s", 120),
        "update_rate_hz": runtime.config.get("gps_sim.update_rate_hz", 10.0),
        "drift_heading_deg": runtime.config.get("gps_sim.drift_heading_deg", 0.0),
        "drift_alt_jitter_m": runtime.config.get("gps_sim.drift_alt_jitter_m", 0.1),
        "ephemeris_save_dir": runtime.config.get("ephemeris.save_dir", "data/ephemeris"),
        "ephemeris_max_files": runtime.config.get("ephemeris.max_files", 5),
    }


@app.put("/api/settings")
def update_settings(request: SettingsRequest):
    runtime.config.set("hackrf.default_freq", request.default_freq)
    runtime.config.set("hackrf.sample_rate", request.sample_rate)
    runtime.config.set("hackrf.tx_gain", request.tx_gain)
    runtime.config.set("hackrf.lna_gain", request.lna_gain)
    runtime.config.set("hackrf.vga_gain", request.vga_gain)
    runtime.config.set("gps_sim.default_speed_mps", request.default_speed_mps)
    runtime.config.set("gps_sim.default_height", request.default_height)
    runtime.config.set("gps_sim.static_duration_s", request.static_duration_s)
    runtime.config.set("gps_sim.traction_duration_s", request.traction_duration_s)
    runtime.config.set("gps_sim.update_rate_hz", request.update_rate_hz)
    runtime.config.set("gps_sim.drift_heading_deg", request.drift_heading_deg)
    runtime.config.set("gps_sim.drift_alt_jitter_m", request.drift_alt_jitter_m)
    runtime.config.set("ephemeris.save_dir", request.ephemeris_save_dir)
    runtime.config.set("ephemeris.max_files", request.ephemeris_max_files)
    return get_settings()


@app.get("/api/presets")
def list_presets():
    return runtime.config.preset_list()


@app.post("/api/presets", status_code=201)
def create_preset(request: PresetCreateRequest):
    if runtime.config.preset_get(request.name):
        raise HTTPException(status_code=409, detail="preset_exists")
    runtime.config.preset_add(request.name, request.lat, request.lon, request.alt)
    return {"name": request.name, **runtime.config.preset_get(request.name)}


@app.put("/api/presets/{name}")
def update_preset(name: str, request: PresetUpdateRequest):
    try:
        updated = runtime.config.preset_update(
            name,
            new_name=request.new_name,
            lat=request.lat,
            lon=request.lon,
            alt=request.alt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="preset_missing")
    target = request.new_name or name
    return {"name": target, **runtime.config.preset_get(target)}


@app.delete("/api/presets/{name}", status_code=204)
def delete_preset(name: str):
    if not runtime.config.preset_delete(name):
        raise HTTPException(status_code=404, detail="preset_missing")


@app.get("/api/files")
def list_bin_files():
    return {"files": runtime.list_bin_files()}


@app.delete("/api/files")
def delete_bin_files(request: BinDeleteRequest):
    deleted = runtime.delete_bin_files(request.names)
    return {"deleted": deleted}


@app.post("/api/generate/static", status_code=202)
def generate_static(request: StaticGenerationRequest):
    return {
        "task_id": runtime.start_task(
            "static_generation", runtime.generate_static, request
        )
    }


@app.post("/api/generate/traction", status_code=202)
def generate_traction(request: TractionGenerationRequest):
    return {
        "task_id": runtime.start_task(
            "traction_generation", runtime.generate_traction, request
        )
    }


@app.post("/api/tasks/cancel", status_code=202)
def cancel_task():
    runtime.cancel_task()
    return {"status": "cancelling"}


@app.post("/api/rf/transmit", status_code=202)
def start_transmit():
    runtime.start_transmit()
    return {"status": "started"}


@app.post("/api/rf/jam", status_code=202)
def start_jam():
    runtime.start_jam()
    return {"status": "started"}


@app.post("/api/rf/stop")
def stop_rf():
    runtime.stop_rf()
    return {"status": "stopped"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
