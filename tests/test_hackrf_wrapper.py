import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hackrf_wrapper import HackRFCLI

CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)


class HackRFWrapperTests(unittest.TestCase):
    def test_windows_process_uses_a_new_process_group(self):
        process = Mock()
        process.poll.return_value = 0

        with (
            patch("hackrf_wrapper.os.name", "nt"),
            patch("hackrf_wrapper.WINDOWS_PROCESS_GROUP", 512),
            patch("hackrf_wrapper.subprocess.Popen", return_value=process) as popen,
        ):
            hackrf = HackRFCLI()
            self.assertTrue(hackrf._start_process(["hackrf_transfer.exe"]))

        self.assertEqual(popen.call_args.kwargs["creationflags"], 512)

    def test_windows_stop_sends_ctrl_break_before_terminating(self):
        process = Mock()
        process.poll.side_effect = [None, 0]
        hackrf = HackRFCLI()
        hackrf.process = process

        with (
            patch("hackrf_wrapper.os.name", "nt"),
            patch("hackrf_wrapper.WINDOWS_CTRL_BREAK_EVENT", CTRL_BREAK_EVENT),
        ):
            hackrf.stop()

        process.send_signal.assert_called_once_with(CTRL_BREAK_EVENT)
        process.wait.assert_called_once_with(timeout=5)
        process.terminate.assert_not_called()
        self.assertIsNone(hackrf.process)

    def test_windows_stop_forces_termination_after_timeout(self):
        process = Mock()
        process.poll.side_effect = [None, 0]
        process.wait.side_effect = [subprocess.TimeoutExpired("hackrf_transfer", 5), 0]
        hackrf = HackRFCLI()
        hackrf.process = process

        with (
            patch("hackrf_wrapper.os.name", "nt"),
            patch("hackrf_wrapper.WINDOWS_CTRL_BREAK_EVENT", CTRL_BREAK_EVENT),
        ):
            hackrf.stop()

        process.send_signal.assert_called_once_with(CTRL_BREAK_EVENT)
        process.terminate.assert_called_once()
        self.assertEqual(process.wait.call_args_list[1].kwargs["timeout"], 2)

    def test_start_tx_still_passes_the_signal_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            signal_file = Path(temp_dir) / "signal.bin"
            signal_file.touch()
            hackrf = HackRFCLI()

            with patch.object(hackrf, "_start_process", return_value=True) as start:
                result = hackrf.start_tx(
                    filename=str(signal_file),
                    freq_hz=1_575_420_000,
                    repeat=False,
                )

        self.assertTrue(result)
        self.assertEqual(start.call_args.args[0][0], hackrf.transfer_exec)


if __name__ == "__main__":
    unittest.main()
