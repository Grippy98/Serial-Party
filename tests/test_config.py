import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from serial_party.config import ConfigError, get_board, load_config, validate_config


def example():
    return {"version": 1, "boards": [{"id": "board-a", "name": "Board A",
            "device": "/dev/serial/by-id/usb-Example_A-if00", "baud": 115200,
            "monitor_port": 7681, "control_port": 7682}]}


class ConfigTests(unittest.TestCase):
    def test_normalizes_defaults_without_mutating_input(self):
        data = example()
        before = copy.deepcopy(data)
        result = validate_config(data)
        self.assertEqual(data, before)
        self.assertEqual(result["broker_port"], 7820)
        self.assertEqual(result["broker_secondary_port"], 7821)
        self.assertEqual(result["listen_host"], "127.0.0.1")
        self.assertIn("webcontrol", result["writers"])

    def test_by_id_by_device_and_single_board_default(self):
        config = validate_config(example())
        board = config["boards"][0]
        self.assertIs(get_board(config), board)
        self.assertIs(get_board(config, "board-a"), board)
        self.assertIs(get_board(config, board["device"]), board)
        for selector in ("board", "../board-a", "", "/dev/ttyUSB0"):
            with self.assertRaises(ConfigError):
                get_board(config, selector)

    def test_multiple_boards_need_explicit_selection(self):
        config = example()
        config["boards"].append({"id": "board-b", "device": "/dev/pts/2", "monitor_port": 7683, "control_port": 7684})
        result = validate_config(config)
        with self.assertRaises(ConfigError):
            get_board(result)
        self.assertEqual(get_board(result, "board-b")["baud"], 115200)

    def test_rejects_config_and_device_injection(self):
        for field, values in {
            "id": ["A", "x;rw all", "x\nconsole y", "../x", "x" * 33, "x%y"],
            "device": ["/dev/ttyUSB0;rw all", "/dev/ttyUSB0\n", "/dev/../etc/passwd", "/dev//ttyUSB0", "/dev/tty*", "/dev/tty[0]", "/dev/tty|foo", "/tmp/tty"],
            "name": ["", "   ", "x\nnew", "x\x00y", "x\u0085y", "x\u2028y", "x" * 81],
            "baud": [True, 12345, "115200", -1],
            "monitor_port": [True, 0, 22, 65536, "7681"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = example()
                    data["boards"][0][field] = value
                    with self.assertRaises(ConfigError):
                        validate_config(data)

    def test_readonly_label_and_invalid_writer_list_are_rejected(self):
        for writers in ([], ["monitor", "webcontrol"], ["operator"], ["webcontrol", "webcontrol"], ["webcontrol", "x,root"], "operator"):
            data = example()
            data["writers"] = writers
            with self.assertRaises(ConfigError):
                validate_config(data)

    def test_all_port_collisions_rejected(self):
        for edit in ({"broker_port": 7681}, {"broker_secondary_port": 7820}):
            data = example()
            data.update(edit)
            with self.assertRaises(ConfigError):
                validate_config(data)
        data = example()
        data["boards"][0]["control_port"] = 7681
        with self.assertRaises(ConfigError):
            validate_config(data)

    def test_duplicate_devices_and_ids_rejected(self):
        for field in ("id", "device", "monitor_port", "control_port"):
            data = example()
            other = {"id": "board-b", "device": "/dev/pts/2", "monitor_port": 7683, "control_port": 7684}
            other[field] = data["boards"][0][field]
            data["boards"].append(other)
            with self.assertRaises(ConfigError):
                validate_config(data)

    def test_maximum_conserver_group_size(self):
        data = example()
        data["boards"] = [{"id": f"board-{i}", "device": f"/dev/pts/{i}", "monitor_port": 8000 + i * 2, "control_port": 8001 + i * 2} for i in range(16)]
        self.assertEqual(len(validate_config(data)["boards"]), 16)
        data["boards"].append({"id": "extra"})
        with self.assertRaises(ConfigError):
            validate_config(data)

    def test_terminal_bind_address_is_ipv4_only(self):
        for address in ("127.0.0.1", "0.0.0.0", "192.0.2.10"):
            self.assertEqual(validate_config({**example(), "listen_host": address})["listen_host"], address)
        for address in ("example.com", "::", "224.0.0.1", "255.255.255.255", 1, True, "127.0.0.1; x"):
            with self.assertRaises(ConfigError):
                validate_config({**example(), "listen_host": address})

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ConfigError):
            validate_config({**example(), "password": "do-not-store-secrets-here"})
        data = example()
        data["boards"][0]["shell"] = "sh"
        with self.assertRaises(ConfigError):
            validate_config(data)

    def test_load_path_and_environment_without_exposing_invalid_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(example()))
            with patch.dict(os.environ, {"SERIAL_PARTY_CONFIG": str(path)}):
                self.assertEqual(load_config(), load_config(path))
            path.write_text("secret-invalid-json")
            with self.assertRaises(ConfigError) as error:
                load_config(path)
            self.assertNotIn("secret-invalid-json", str(error.exception))


if __name__ == "__main__":
    unittest.main()
