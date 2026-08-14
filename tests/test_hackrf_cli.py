import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import hackrf


class TransmitTests(unittest.TestCase):
    def test_single_playback_does_not_enable_hackrf_repeat(self):
        with (
            patch.object(hackrf.hackrf, "start_tx", return_value=True) as start_tx,
            patch.object(hackrf.hackrf, "is_running", return_value=False),
        ):
            hackrf._transmit("trajectory.bin", loop=False)

        self.assertFalse(start_tx.call_args.kwargs["repeat"])

    def test_static_duration_controls_generation_and_single_playback(self):
        args = SimpleNamespace(
            lat=25.0,
            lon=121.0,
            alt=10.0,
            preset=None,
            duration=300,
            repeat=None,
            drift=False,
            drift_rate=None,
            drift_seed=None,
            time_mode="ephemeris",
        )
        simulator = Mock()
        simulator.generate_bin.return_value = "/tmp/static_300s_static.bin"

        with (
            patch("hackrf._make_simulator", return_value=simulator),
            patch("hackrf.shutil.disk_usage", return_value=SimpleNamespace(free=10**12)),
            patch("hackrf._transmit") as transmit,
        ):
            hackrf.cmd_gps_static(args)

        self.assertEqual(
            simulator.generate_bin.call_args.kwargs["drift_duration_s"],
            300,
        )
        self.assertFalse(transmit.call_args.kwargs["loop"])

    def test_static_duration_can_exceed_five_minutes(self):
        args = SimpleNamespace(
            lat=25.0,
            lon=121.0,
            alt=10.0,
            preset=None,
            duration=301,
            repeat=None,
            drift=False,
            drift_rate=None,
            drift_seed=None,
            time_mode="ephemeris",
        )

        simulator = Mock()
        simulator.generate_bin.return_value = "/tmp/static_301s_static.bin"
        with (
            patch("hackrf._make_simulator", return_value=simulator),
            patch("hackrf.shutil.disk_usage", return_value=SimpleNamespace(free=10**12)),
            patch("hackrf._transmit"),
        ):
            hackrf.cmd_gps_static(args)

        self.assertEqual(
            simulator.generate_bin.call_args.kwargs["drift_duration_s"],
            301,
        )


if __name__ == "__main__":
    unittest.main()
