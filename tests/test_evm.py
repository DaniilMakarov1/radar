from __future__ import annotations

import unittest

from smart_money_radar.ingestion.evm import decode_abi_string


class EvmAbiTest(unittest.TestCase):
    def test_decodes_dynamic_string(self) -> None:
        symbol = b"TOKEN"
        encoded = (
            (32).to_bytes(32, "big")
            + len(symbol).to_bytes(32, "big")
            + symbol.ljust(32, b"\x00")
        )

        self.assertEqual(decode_abi_string("0x" + encoded.hex()), "TOKEN")

    def test_decodes_bytes32_string(self) -> None:
        encoded = b"BASE".ljust(32, b"\x00")

        self.assertEqual(decode_abi_string("0x" + encoded.hex()), "BASE")


if __name__ == "__main__":
    unittest.main()
