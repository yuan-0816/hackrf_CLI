import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fake_gps import FakeGPS


class FakeGPSTests(unittest.TestCase):
    def test_explicit_ephemeris_is_used_without_fetching(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ephemeris = root / "provided.nav"
            motion = root / "motion.csv"
            output = root / "signal.bin"
            executable = root / "gps-sdr-sim"
            ephemeris.touch()
            motion.write_text("0.0,25.0,121.0,10.0\n", encoding="utf-8")
            executable.touch()

            simulator = FakeGPS(gps_sim_exe_path=str(executable))
            with (
                patch("fake_gps.fetch_latest_ephemeris") as fetch,
                patch("fake_gps.subprocess.Popen") as popen,
            ):
                popen.return_value.wait.return_value = 0
                result = simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=str(ephemeris),
                    csv_file=str(motion),
                )

            self.assertEqual(result, str(output))
            fetch.assert_not_called()
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-e") + 1], str(ephemeris))
            self.assertEqual(command[command.index("-x") + 1], str(motion))
            self.assertNotIn("-u", command)

    def test_non_ten_hz_motion_rate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "10 Hz"):
            FakeGPS(update_rate_hz=5.0)

    def test_static_mode_uses_requested_scenario_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ephemeris = root / "provided.nav"
            output = root / "signal.bin"
            executable = root / "gps-sdr-sim"
            ephemeris.touch()
            executable.touch()

            simulator = FakeGPS(gps_sim_exe_path=str(executable))
            with patch("fake_gps.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
                simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=str(ephemeris),
                    static_mode=True,
                    manual_coords=(25.0, 121.0, 10.0),
                    scenario_start_time="2026/08/11,07:30:00",
                )

            command = popen.call_args.args[0]
            self.assertEqual(
                command[command.index("-t") + 1],
                "2026/08/11,07:30:00",
            )

    def test_static_shifted_now_mode_uses_uppercase_t(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ephemeris = root / "provided.nav"
            output = root / "signal.bin"
            executable = root / "gps-sdr-sim"
            ephemeris.touch()
            executable.touch()

            simulator = FakeGPS(gps_sim_exe_path=str(executable))
            with patch("fake_gps.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
                simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=str(ephemeris),
                    static_mode=True,
                    manual_coords=(25.0, 121.0, 10.0),
                    time_mode="shifted-now",
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-T") + 1], "now")
            self.assertNotIn("-t", command)

    def test_static_mode_defaults_to_current_utc_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ephemeris = root / "provided.nav"
            output = root / "signal.bin"
            executable = root / "gps-sdr-sim"
            ephemeris.touch()
            executable.touch()

            simulator = FakeGPS(gps_sim_exe_path=str(executable))
            with patch("fake_gps.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
                simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=str(ephemeris),
                    static_mode=True,
                    manual_coords=(25.0, 121.0, 10.0),
                )

            command = popen.call_args.args[0]
            start_time = command[command.index("-t") + 1]
            parsed = datetime.datetime.strptime(start_time, "%Y/%m/%d,%H:%M:%S")
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            self.assertLess(abs((now - parsed).total_seconds()), 5)

    def test_default_time_is_clamped_to_latest_ephemeris_epoch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ephemeris = Path(temp_dir) / "provided.nav"
            ephemeris.write_text(
                "     2              NAVIGATION DATA     RINEX VERSION / TYPE\n"
                "                                                            END OF HEADER\n"
                " 1 26  8 11  0  0  0.0 0.0 0.0 0.0\n"
                " 1 26  8 11  4  0  0.0 0.0 0.0 0.0\n",
                encoding="ascii",
            )
            simulator = FakeGPS()

            selected = simulator._nearest_ephemeris_time_to_now(str(ephemeris))

            self.assertEqual(selected, "2026/08/11,04:00:00")

    def test_traction_can_exceed_five_minutes(self):
        simulator = FakeGPS()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = simulator._generate_traction_csv(
                csv_file=str(Path(temp_dir) / "motion.csv"),
                start_lat=25.0,
                start_lon=121.0,
                start_alt=10.0,
                heading_deg=0.0,
                target_speed_mps=0.5,
                ramp_duration_s=20.0,
                total_duration_s=301.0,
            )

        self.assertTrue(result)

    def test_traction_profile_decelerates_and_finishes_stationary(self):
        simulator = FakeGPS()
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_file = Path(temp_dir) / "motion.csv"
            result = simulator._generate_traction_csv(
                csv_file=str(csv_file),
                start_lat=25.0,
                start_lon=121.0,
                start_alt=10.0,
                heading_deg=0.0,
                target_speed_mps=0.5,
                ramp_duration_s=2.0,
                total_duration_s=10.0,
                hold_duration_s=1.0,
                final_hold_duration_s=1.0,
            )

            rows = [line.split(",") for line in csv_file.read_text().splitlines()]

        self.assertTrue(result)
        self.assertEqual(float(rows[-1][0]), 10.0)
        self.assertEqual(rows[-1][1:], rows[-2][1:])

    def test_dynamic_shifted_now_mode_uses_uppercase_t(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ephemeris = root / "provided.nav"
            motion = root / "motion.csv"
            output = root / "signal.bin"
            executable = root / "gps-sdr-sim"
            ephemeris.touch()
            motion.write_text("0.0,25.0,121.0,10.0\n", encoding="utf-8")
            executable.touch()

            simulator = FakeGPS(gps_sim_exe_path=str(executable))
            with patch("fake_gps.subprocess.Popen") as popen:
                popen.return_value.wait.return_value = 0
                simulator.generate_bin(
                    output_bin=str(output),
                    ephemeris_file_path=str(ephemeris),
                    csv_file=str(motion),
                    time_mode="shifted-now",
                )

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("-T") + 1], "now")
            self.assertIn("-x", command)


if __name__ == "__main__":
    unittest.main()
