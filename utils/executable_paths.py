import platform
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HACKRF_WINDOWS_DIR = PROJECT_ROOT / "third_party" / "hackrf-tools-windows"
GPS_SIM_DIR = PROJECT_ROOT / "third_party" / "gps-sdr-sim"


def is_windows(system_name: str | None = None) -> bool:
    """Return whether executable lookup should use Windows conventions."""
    return (system_name or platform.system()).casefold() == "windows"


def resolve_hackrf_executable(
    name: str,
    *,
    system_name: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Resolve a HackRF tool, preferring the bundled Windows distribution."""
    windows = is_windows(system_name)
    root = Path(project_root) if project_root is not None else PROJECT_ROOT

    if windows:
        executable_name = name if name.casefold().endswith(".exe") else f"{name}.exe"
        bundled = root / "third_party" / "hackrf-tools-windows" / executable_name
        if bundled.is_file():
            return str(bundled.resolve())

    path_names = [name]
    if windows and not name.casefold().endswith(".exe"):
        path_names.append(f"{name}.exe")
    for path_name in path_names:
        located = shutil.which(path_name)
        if located:
            return located

    # Keep the traditional command name so subprocess raises FileNotFoundError.
    # This also allows callers and tests to inject their own process runner.
    return name


def default_gps_sim_executable(
    *,
    system_name: str | None = None,
    project_root: Path | None = None,
) -> str:
    """Return the platform-specific in-project gps-sdr-sim executable path."""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    filename = "gps-sdr-sim.exe" if is_windows(system_name) else "gps-sdr-sim"
    return str((root / "third_party" / "gps-sdr-sim" / filename).resolve())
