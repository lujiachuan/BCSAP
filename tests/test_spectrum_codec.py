"""谱图编码边界验证。"""

import unittest

import numpy as np

from packages.spectrum import (
    SpectrumFormatError,
    decode_spectrum,
    encode_spectrum,
    spectrum_checksum,
)


class SpectrumCodecTests(unittest.TestCase):
    def test_round_trip_preserves_values(self) -> None:
        x = np.linspace(1.0, 5.0, 150_000)
        y = np.sin(x)

        payload = encode_spectrum(x, y)
        decoded_x, decoded_y = decode_spectrum(payload)

        np.testing.assert_array_equal(decoded_x, x)
        np.testing.assert_array_equal(decoded_y, y)
        self.assertEqual(len(spectrum_checksum(payload)), 64)

    def test_mismatched_arrays_are_rejected(self) -> None:
        with self.assertRaises(SpectrumFormatError):
            encode_spectrum([1.0, 2.0], [3.0])

    def test_invalid_payload_is_rejected(self) -> None:
        with self.assertRaises(SpectrumFormatError):
            decode_spectrum(b"not-a-spectrum")


if __name__ == "__main__":
    unittest.main()

