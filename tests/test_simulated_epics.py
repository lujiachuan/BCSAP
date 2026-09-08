"""模拟 EPICS 网关验证。"""

import unittest
from uuid import uuid4

from packages.epics_adapter import SimulatedEpicsGateway


class SimulatedEpicsGatewayTests(unittest.TestCase):
    def test_write_read_and_snapshot(self) -> None:
        gateway = SimulatedEpicsGateway({"magnet.current": (1.0, "A")})

        written = gateway.write("magnet.current", 2.5, uuid4())
        read = gateway.read("magnet.current")
        snapshot = gateway.snapshot(["magnet.current"])

        self.assertTrue(gateway.connect())
        self.assertEqual(written.value, 2.5)
        self.assertEqual(read.unit, "A")
        self.assertEqual(snapshot["magnet.current"].value, 2.5)

    def test_unknown_signal_is_rejected(self) -> None:
        gateway = SimulatedEpicsGateway()
        with self.assertRaises(KeyError):
            gateway.read("unknown.signal")
        with self.assertRaises(KeyError):
            gateway.write("unknown.signal", 1.0, uuid4())


if __name__ == "__main__":
    unittest.main()
