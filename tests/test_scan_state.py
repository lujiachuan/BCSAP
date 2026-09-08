"""扫谱状态规则验证。"""

import unittest

from packages.domain import InvalidScanTransition, ScanState, transition_scan


class ScanStateTests(unittest.TestCase):
    def test_normal_scan_path(self) -> None:
        state = ScanState.DRAFT
        for target in (
            ScanState.VALIDATING,
            ScanState.PREPARING,
            ScanState.RUNNING,
            ScanState.COMPLETING,
            ScanState.COMPLETED,
        ):
            state = transition_scan(state, target)
        self.assertEqual(state, ScanState.COMPLETED)

    def test_completed_scan_cannot_restart(self) -> None:
        with self.assertRaises(InvalidScanTransition):
            transition_scan(ScanState.COMPLETED, ScanState.RUNNING)


if __name__ == "__main__":
    unittest.main()

