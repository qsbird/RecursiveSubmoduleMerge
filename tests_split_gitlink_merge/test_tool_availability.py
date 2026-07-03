from __future__ import annotations

import unittest
from unittest.mock import patch

from manual_merge_split.preflight import ToolAvailabilityResult, check_tool_availability


class CheckToolAvailabilityTests(unittest.TestCase):
    def test_available_tool_returns_available(self) -> None:
        result = check_tool_availability("git")
        self.assertIsInstance(result, ToolAvailabilityResult)
        # git should be available on any system running these tests
        self.assertTrue(result.available)
        self.assertEqual(result.tool_name, "git")

    def test_unavailable_tool_returns_not_available(self) -> None:
        result = check_tool_availability("nonexistent_tool_xyz_12345")
        self.assertFalse(result.available)
        self.assertEqual(result.tool_name, "nonexistent_tool_xyz_12345")
        self.assertIn("nonexistent_tool_xyz_12345", result.error_message)

    @patch("shutil.which", return_value=None)
    def test_which_returns_none_means_not_available(self, _mock_which: object) -> None:
        result = check_tool_availability("code")
        self.assertFalse(result.available)

    @patch("shutil.which", return_value="/usr/bin/code")
    def test_which_returns_path_means_available(self, _mock_which: object) -> None:
        result = check_tool_availability("code")
        self.assertTrue(result.available)

    def test_result_is_frozen_dataclass(self) -> None:
        result = ToolAvailabilityResult(tool_name="test", available=True, error_message="", resolved_path="/usr/bin/test")
        with self.assertRaises(AttributeError):
            result.available = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()