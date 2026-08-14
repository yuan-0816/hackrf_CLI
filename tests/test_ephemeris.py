import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.get_latest_brdc import fetch_latest_ephemeris


class EphemerisUpdateTests(unittest.TestCase):
    def test_existing_daily_file_is_reused_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            with patch(
                "utils.get_latest_brdc.get_brdc_url",
                return_value=("https://example.test/file.gz", "today.gz"),
            ):
                expected = save_dir / "today"
                expected.touch()
                with patch("utils.get_latest_brdc.download_file") as download:
                    result = fetch_latest_ephemeris(save_dir=str(save_dir))

            self.assertEqual(result, str(expected))
            download.assert_not_called()

    def test_force_refresh_downloads_even_when_daily_file_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = Path(temp_dir)
            existing = save_dir / "today"
            existing.touch()
            archive = save_dir / "today.gz"

            with (
                patch(
                    "utils.get_latest_brdc.get_brdc_url",
                    return_value=("https://example.test/file.gz", "today.gz"),
                ),
                patch(
                    "utils.get_latest_brdc._load_credentials",
                    return_value=("user", "password"),
                ),
                patch(
                    "utils.get_latest_brdc.download_file",
                    return_value=str(archive),
                ) as download,
                patch(
                    "utils.get_latest_brdc.uncompress_file",
                    return_value=str(existing),
                ),
            ):
                result = fetch_latest_ephemeris(
                    save_dir=str(save_dir),
                    cleanup=False,
                    force=True,
                )

            self.assertEqual(result, str(existing))
            self.assertTrue(download.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
