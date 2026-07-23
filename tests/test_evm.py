from __future__ import annotations

import unittest

from smart_money_radar.ingestion.evm import (
    decode_abi_address,
    decode_abi_string,
    decode_abi_uint,
)


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

    def test_decodes_uint(self) -> None:
        encoded = (18).to_bytes(32, "big")

        self.assertEqual(decode_abi_uint("0x" + encoded.hex()), 18)

    def test_decodes_address(self) -> None:
        encoded = bytes.fromhex("00" * 12 + "12" * 20)

        self.assertEqual(decode_abi_address("0x" + encoded.hex()), "0x" + "12" * 20)


if __name__ == "__main__":
    unittest.main()
