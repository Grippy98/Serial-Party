import base64
import copy
import io
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from serial_party import host
from serial_party.config import validate_config


def example():
    return validate_config({"version": 1, "listen_host": "192.0.2.10", "boards": [
        {"id": "board-a", "name": "Board A", "device": "/dev/serial/by-id/usb-Example_A-if00", "monitor_port": 7681, "control_port": 7682},
        {"id": "board-b", "name": "Board B", "device": "/dev/serial/by-id/usb-Example_B-if00", "monitor_port": 7683, "control_port": 7684}]})


class HostTests(unittest.TestCase):
    def test_renderer_separates_broker_terminal_and_physical_access(self):
        files = host.render_files(example())
        broker = files["/etc/systemd/system/serial-party-conserver.service"]
        self.assertIn("-M 127.0.0.1 -m 16", broker)
        self.assertIn("User=serialparty", broker)
        self.assertNotIn("192.0.2.10", files["/etc/serial-party/conserver.cf"])
        self.assertIn("ro monitor;", files["/etc/serial-party/conserver.cf"])
        self.assertEqual(len(host.unit_names(example())), 5)
        for mode in ("monitor", "control"):
            terminal = files[f"/etc/systemd/system/serial-party-board-a-{mode}.service"]
            self.assertIn("User=www-data", terminal)
            self.assertIn("PrivateDevices=true", terminal)
            self.assertNotIn("/dev/serial", terminal)
        self.assertNotIn("/etc/serial-party/web.env", files)

    def test_udev_targets_each_exact_stable_usb_link(self):
        rule = host.render_files(example())[host.RULE]
        self.assertEqual(rule.count('SUBSYSTEM=="tty"'), 2)
        self.assertIn('SYMLINK=="serial/by-id/usb-Example_A-if00"', rule)
        self.assertIn('MODE="0600", TAG-="uaccess"', rule)
        self.assertNotIn('KERNEL=="tty*"', rule)

    def test_unsafe_executable_paths_rejected(self):
        for option in ("bin_dir", "ttyd"):
            for value in ("relative", "/usr/bin/ttyd --writable", "/usr/bin/%i", "/usr/bin/../ttyd", "/usr/bin/ttyd\nExecStart=sh"):
                with self.assertRaises(host.HostError):
                    host.render_files(example(), **{option: value})

    def test_monitor_is_readonly_at_ttyd_and_broker(self):
        config = example()
        argv = host.terminal_argv(config, config["boards"][0], "monitor", "user:" + "x" * 32)
        self.assertNotIn("-W", argv)
        self.assertNotIn("-f", argv)
        self.assertIn("-S", argv)
        self.assertEqual(argv[argv.index("-l") + 1], "monitor")
        self.assertEqual(argv[argv.index("-M") + 1], "127.0.0.1")
        self.assertIn("rendererType=dom", argv)
        self.assertEqual(argv[argv.index("-d") + 1], "1")
        self.assertEqual(argv[argv.index("-b") + 1], "/monitor")

    def test_control_uses_claim_wrapper_and_single_browser_client(self):
        config = example()
        argv = host.terminal_argv(config, config["boards"][1], "control", "user:" + "x" * 32)
        self.assertIn("-W", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "1")
        self.assertIn(host.BIN_DIR + "/serial-party-console", argv)
        self.assertEqual(argv[-4:], ["board-b", "control", "--label", "webcontrol"])
        self.assertNotIn("-f", argv)

    def test_credential_is_required_and_validated_without_echoing_it(self):
        for credential in ("", "user:short", "user:secret\nINJECT=value"):
            with self.assertRaises(host.HostError) as error:
                host.terminal_argv(example(), example()["boards"][0], "monitor", credential)
            if credential:
                self.assertNotIn(credential, str(error.exception))

    def test_render_is_passive_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(host, "run") as runner:
            output = Path(directory) / "stage"
            result = host.render(example(), output)
            self.assertEqual(result["result"], "rendered")
            self.assertTrue((output / "etc/serial-party/config.json").is_file())
            self.assertFalse((output / "etc/serial-party/web.env").exists())
            runner.assert_not_called()
            with self.assertRaises(host.HostError):
                host.render(example(), output)

    def test_atomic_write_has_restricted_permissions_and_no_temporary_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            host.atomic_write(path, "test", 0o600)
            self.assertEqual(path.read_text(), "test")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_config_directory_stays_traversable_under_private_deployment_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serial-party"
            previous = os.umask(0o077)
            try:
                host.ensure_root_directory(path, 0o755)
                host.atomic_write(path / "config.json", "{}\n")
                host.atomic_write(path / "web.env", "private credential\n", 0o600)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((path / "config.json").stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((path / "web.env").stat().st_mode), 0o600)

    def test_install_needs_two_explicit_flags_before_any_action(self):
        for kwargs in ({}, {"activate": True}, {"confirm_idle": True}):
            with patch.object(host, "run") as runner, patch.object(host, "render_files") as renderer:
                with self.assertRaises(host.HostError):
                    host.install(example(), **kwargs)
                runner.assert_not_called()
                renderer.assert_not_called()

    def test_busy_device_guard_never_kills_or_opens_the_uart(self):
        raw = MagicMock(spec=Path)
        with patch.object(host, "identify_device", return_value=(raw, {})), patch.object(host, "holders", return_value=[123]), patch.object(host, "run") as runner:
            with self.assertRaisesRegex(host.HostError, "already open"):
                host.snapshot_device(example()["boards"][0])
            raw.open.assert_not_called()
            raw.stat.assert_not_called()
            runner.assert_not_called()

    def test_device_replacement_aborts_before_permission_changes(self):
        with patch.object(host, "identify_device", return_value=(Path("/dev/example"), {"ID_SERIAL": "replacement"})), patch.object(host.os, "chown") as chown:
            with self.assertRaisesRegex(host.HostError, "identity changed"):
                host.same_device(example()["boards"][0], {"identity": {"ID_SERIAL": "original"}})
            chown.assert_not_called()

    def test_permission_rollback_restores_acl_owner_mode(self):
        raw = MagicMock(spec=Path)
        raw.__str__.return_value = "/dev/fake"
        raw.stat.return_value = type("Info", (), {"st_uid": 11, "st_gid": 22, "st_mode": stat.S_IFCHR | 0o660})()
        previous = {"uid": 11, "gid": 22, "mode": 0o660, "acl": "user::rw-\ngroup::rw-\nother::---\n"}
        with patch.object(host, "holders", return_value=[]), patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, previous["acl"], "")) as runner, patch.object(host.os, "chown") as chown, patch.object(host.os, "chmod") as chmod:
            host.restore_device(raw, previous)
            chown.assert_called_once_with(raw, 11, 22)
            chmod.assert_called_once_with(raw, 0o660)
            self.assertIn(unittest.mock.call("setfacl", "--set-file=-", "/dev/fake", input=previous["acl"]), runner.call_args_list)

    def test_permission_rollback_refuses_new_holder(self):
        with patch.object(host, "holders", return_value=[123]), patch.object(host.os, "chown") as chown, patch.object(host, "run") as runner:
            with self.assertRaises(host.HostError):
                host.restore_device(Path("/dev/fake"), {})
            chown.assert_not_called()
            runner.assert_not_called()

    def test_rollback_status_guard_distinguishes_observer_from_writer(self):
        config = example()
        fields = ["board-a", "localhost,1,2", "/", "/dev/fake,115200n,3", "", "up", "rw", "log", "1", "autoup", "", "", "", "", ""]
        for user in ("", "r@monitor@localhost@0@ro", "s@operator@localhost@0@ro"):
            fields[4] = user
            with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, ":".join(fields), "")):
                host.assert_no_writers(config, config["boards"][0])
        for user in ("w@hermes@localhost@0", "r@codex@localhost@0@rw", "unexpected"):
            fields[4] = user
            with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, ":".join(fields), "")):
                with self.assertRaises(host.HostError):
                    host.assert_no_writers(config, config["boards"][0])

    def test_rollback_rejects_missing_acknowledgement_before_accessing_backup(self):
        with patch.object(host, "run") as runner:
            with self.assertRaises(host.HostError):
                host.rollback("/does/not/exist")
            runner.assert_not_called()

    def test_rollback_filesystem_guard_preserves_local_edits_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed.conf"
            path.write_text("installed")
            files = {str(path): host.sha(path)}
            host.validate_managed_files(files, [str(path)])
            path.write_text("operator changed this")
            with self.assertRaisesRegex(host.HostError, "local edits"):
                host.validate_managed_files(files, [str(path)])
            self.assertEqual(path.read_text(), "operator changed this")
            path.unlink()
            host.validate_managed_files(files, [str(path)])
            other = Path(directory) / "other.conf"
            other.write_text("installed")
            path.symlink_to(other)
            with self.assertRaises(host.HostError):
                host.validate_managed_files(files, [str(path)])
            self.assertEqual(other.read_text(), "installed")

    def test_rollback_filesystem_guard_rejects_unmanaged_destinations(self):
        with self.assertRaisesRegex(host.HostError, "unmanaged"):
            host.validate_managed_files({"/etc/passwd": "a" * 64}, [host.RULE])

    def test_fuser_errors_fail_closed(self):
        for result in (subprocess.CompletedProcess([], 2, "", "error"), subprocess.CompletedProcess([], 0, "surprise", "")):
            with patch.object(host, "run", return_value=result):
                with self.assertRaises(host.HostError):
                    host.holders("/dev/fake")

    def test_ttyd_old_release_is_rejected_before_activation(self):
        with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, "--readonly --base-path --max-clients", "")):
            with self.assertRaisesRegex(host.HostError, "required capabilities"):
                host.check_ttyd("/usr/bin/ttyd")

    def test_ttyd_help_on_stderr_is_supported(self):
        with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, "", "--writable --base-path --max-clients --debug")):
            host.check_ttyd("/usr/bin/ttyd")

    @unittest.skipUnless(shutil.which("ttyd"), "Real ttyd startup-log regression needs the ttyd executable")
    def test_real_ttyd_startup_does_not_log_credentials(self):
        executable = str(Path(shutil.which("ttyd")).resolve())
        host.check_ttyd(executable)
        config = example()
        config["listen_host"] = "127.0.0.1"
        config["boards"][0]["monitor_port"] = 0  # Isolated test lets the OS choose an unused port.
        secret = "synthetic-test:" + "x" * 32
        argv = host.terminal_argv(config, config["boards"][0], "monitor", secret, ttyd=executable)
        argv = argv[:argv.index("/usr/bin/console")] + ["/bin/cat"]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(.25)
            self.assertIsNone(process.poll(), "ttyd exited before its logging behavior could be verified")
        finally:
            process.terminate()
            output = process.communicate(timeout=5)[0]
        self.assertNotIn(secret, output)
        self.assertNotIn(base64.b64encode(secret.encode()).decode(), output)

    def test_validate_cli_reads_environment_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(example()))
            with patch.dict(os.environ, {"SERIAL_PARTY_CONFIG": str(path)}), patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(host.main(["validate"]), 0)
                self.assertEqual(json.loads(stdout.getvalue())["boards"], ["board-a", "board-b"])


if __name__ == "__main__":
    unittest.main()
