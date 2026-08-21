import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.executable_paths import (
    default_gps_sim_executable,
    resolve_hackrf_executable,
)


class ExecutablePathTests(unittest.TestCase):
    def test_windows_prefers_bundled_hackrf_tool(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = (
                root / "third_party" / "hackrf-tools-windows" / "hackrf_info.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()

            resolved = resolve_hackrf_executable(
                "hackrf_info",
                system_name="Windows",
                project_root=root,
            )

            self.assertEqual(resolved, str(executable.resolve()))

    def test_windows_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "utils.executable_paths.shutil.which",
                side_effect=[None, r"C:\tools\hackrf_info.exe"],
            ):
                resolved = resolve_hackrf_executable(
                    "hackrf_info",
                    system_name="Windows",
                    project_root=Path(temp_dir),
                )

            self.assertEqual(resolved, r"C:\tools\hackrf_info.exe")

    def test_gps_sim_uses_exe_suffix_on_windows(self):
        path = default_gps_sim_executable(
            system_name="Windows",
            project_root=Path("project"),
        )

        self.assertTrue(path.endswith("gps-sdr-sim.exe"))


if __name__ == "__main__":
    unittest.main()
