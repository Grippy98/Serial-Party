"""Render and manage the Linux host; nothing opens a UART until explicit activation."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time

from .config import DEFAULT_CONFIG, ConfigError, get_board, load_config, validate_config

BIN_DIR = "/opt/serial-party/venv/bin"
STATE_DIR = Path("/var/lib/serial-party/backups")
SERVICE_USER = "serialparty"
RULE = "/etc/udev/rules.d/99-serial-party.rules"
IDENTITY_KEYS = ("ID_SERIAL", "ID_USB_INTERFACE_NUM", "ID_VENDOR_ID", "ID_MODEL_ID")
SAFE_PATH = re.compile(r"/[A-Za-z0-9_./+-]+\Z")


class HostError(RuntimeError):
    """A host operation was refused or did not pass verification."""


def require(condition, message):
    if not condition:
        raise HostError(message)


def run(*args, check=True, input=None, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, input=input, timeout=timeout)
    if check and result.returncode:
        # Do not include argv or captured output: a subprocess may contain credentials.
        raise HostError(f"{Path(args[0]).name} failed with exit status {result.returncode}")
    return result


def _path(value):
    require(isinstance(value, str) and SAFE_PATH.fullmatch(value)
            and ".." not in Path(value).parts and str(Path(value)) == value,
            "Executable paths must be absolute and contain only safe path characters")
    return value


def unit_names(config):
    return ["serial-party-conserver.service"] + [
        f"serial-party-{board['id']}-{mode}.service"
        for board in config["boards"] for mode in ("monitor", "control")]


def render_files(config, *, bin_dir=BIN_DIR, ttyd="/usr/bin/ttyd"):
    """Return destination -> content, without secrets or any host-side action."""
    config = validate_config(config)
    bin_dir, ttyd = _path(bin_dir), _path(ttyd)
    labels = ",".join(config["writers"])
    broker = [f"""config * {{
    primaryport {config['broker_port']};
    secondaryport {config['broker_secondary_port']};
    defaultaccess rejected;
    autocomplete no;
}}
access * {{
    trusted 127.0.0.1;
    limited {labels},monitor;
    admin "";
}}
"""]
    for board in config["boards"]:
        broker.append(f"""console {board['id']} {{
    master 127.0.0.1;
    type device;
    device {board['device']};
    baud {board['baud']};
    parity none;
    options !ixon,!ixany,!ixoff,!crtscts,!cstopb,!hupcl,autoreinit;
    rw {labels};
    ro monitor;
    tasklist "";
    logfile /var/log/serial-party/{board['id']}.log;
    timestamp 1lab;
}}
""")
    files = {
        DEFAULT_CONFIG: json.dumps(config, indent=2) + "\n",
        "/etc/serial-party/conserver.cf": "\n".join(broker),
        "/etc/systemd/system/serial-party-conserver.service": f"""[Unit]
Description=Serial Party console broker
After=network.target

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
RuntimeDirectory=serial-party
RuntimeDirectoryMode=0750
ExecStart=/usr/sbin/conserver -E -M 127.0.0.1 -m 16 -p {config['broker_port']} -b {config['broker_secondary_port']} -C /etc/serial-party/conserver.cf
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/serial-party /run/serial-party
UMask=0027

[Install]
WantedBy=multi-user.target
""",
        "/etc/logrotate.d/serial-party": """/var/log/serial-party/*.log {
    daily
    rotate 7
    maxsize 10M
    compress
    delaycompress
    missingok
    notifempty
    create 0640 serialparty serialparty
    sharedscripts
    postrotate
        /bin/systemctl kill --kill-who=all --signal=USR2 serial-party-conserver.service >/dev/null 2>&1 || true
    endscript
}
""",
    }
    rules = ["# Only these exact stable USB links belong to Serial Party."]
    for board in config["boards"]:
        if board["device"].startswith("/dev/serial/by-id/"):
            rules.append(f'SUBSYSTEM=="tty", SYMLINK=="{board["device"][5:]}", OWNER="{SERVICE_USER}", GROUP="{SERVICE_USER}", MODE="0600", TAG-="uaccess", ENV{{ID_MM_DEVICE_IGNORE}}="1"')
        else:
            rules.append(f"# {board['id']}: no persistent rule; managed installation requires /dev/serial/by-id.")
        for mode in ("monitor", "control"):
            files[f"/etc/systemd/system/serial-party-{board['id']}-{mode}.service"] = f"""[Unit]
Description=Serial Party {board['id']} {mode} terminal
After=serial-party-conserver.service
Requires=serial-party-conserver.service

[Service]
Type=simple
User=www-data
Group=www-data
EnvironmentFile=/etc/serial-party/web.env
ExecStart={bin_dir}/serial-party-host launch-terminal --config {DEFAULT_CONFIG} --board {board['id']} --mode {mode} --ttyd {ttyd} --bin-dir {bin_dir}
Restart=on-failure
RestartSec=5
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
UMask=0077

[Install]
WantedBy=multi-user.target
"""
    files[RULE] = "\n".join(rules) + "\n"
    return files


def terminal_argv(config, board, mode, credential, *, config_path=DEFAULT_CONFIG,
                  ttyd="/usr/bin/ttyd", bin_dir=BIN_DIR):
    """Build exec arguments. Callers must never log the returned credential argument."""
    require(mode in ("monitor", "control"), "Unknown terminal mode")
    require(isinstance(credential, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}:[A-Za-z0-9_-]{24,128}", credential),
            "Set SERIAL_PARTY_WEB_CREDENTIAL to a 1–32 character username and 24–128 character URL-safe password separated by :")
    command = [_path(ttyd), "-i", config["listen_host"], "-p", str(board[mode + "_port"]),
               # ttyd's default NOTICE logs disclose the reversible Base64 credential.
               "-d", "1", "-c", credential, "-b", "/" + mode,
               "-t", "disableReconnect=true", "-t", "disableLeaveAlert=true",
               "-t", "rendererType=dom", "-t", "fontSize=14", "-t", "scrollback=5000",
               "-t", 'theme={"background":"#1b1f25","foreground":"#f1f5f9","cursor":"#f1f5f9"}',
               "-t", "titleFixed=Serial Party — " + board["name"]]
    if mode == "monitor":
        command += ["/usr/bin/console", "-n", "-C", "/dev/null", "-E", "-M", "127.0.0.1",
                    "-p", str(config["broker_port"]), "-l", "monitor", "-S", board["id"]]
    else:
        command += ["-W", "-m", "1", _path(bin_dir) + "/serial-party-console",
                    "--config", str(config_path), "--board", board["id"], "control", "--label", "webcontrol"]
    return command


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(6))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render(config, directory, **options):
    directory = Path(directory)
    require(not directory.exists() or not any(directory.iterdir()), "Render directory must be absent or empty")
    files = render_files(config, **options)
    for destination, content in files.items():
        atomic_write(directory / destination.lstrip("/"), content)
    return {"result": "rendered", "directory": str(directory), "files": list(files),
            "note": "Preview only; credentials are created privately during installation."}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_ttyd(ttyd):
    result = run(_path(ttyd), "--help")
    help_text = result.stdout + result.stderr
    require(all(flag in help_text for flag in ("--writable", "--base-path", "--max-clients", "--debug")),
            "ttyd lacks required capabilities; install ttyd 1.7.7 or later (see host/README.md)")


def holders(raw):
    result = run("fuser", str(raw), check=False)
    require(result.returncode in (0, 1), "Cannot establish serial-port ownership")
    require(all(value.isdecimal() for value in result.stdout.split()), "Unexpected fuser output")
    return [int(value) for value in result.stdout.split()]


def identify_device(board):
    link = Path(board["device"])
    require(link.parent == Path("/dev/serial/by-id") and link.is_symlink(),
            f"{board['id']}: managed installation needs a present /dev/serial/by-id link")
    raw = link.resolve(strict=True)
    info = raw.stat()
    require(stat.S_ISCHR(info.st_mode), "Configured device is not a character device")
    properties = dict(line.split("=", 1) for line in run("udevadm", "info", "--query=property", "--name=" + str(raw)).stdout.splitlines() if "=" in line)
    require(properties.get("ID_SERIAL") and properties.get("ID_VENDOR_ID") and properties.get("ID_MODEL_ID"),
            "Stable USB identity is unavailable; refusing to change device ownership")
    return raw, {key: properties.get(key, "") for key in IDENTITY_KEYS}


def snapshot_device(board):
    raw, identity = identify_device(board)
    require(not holders(raw), f"{board['id']}: UART is already open; release all existing readers/writers first")
    info = raw.stat()
    return {"identity": identity, "rdev": info.st_rdev, "uid": info.st_uid, "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode), "acl": run("getfacl", "-cp", str(raw)).stdout}


def same_device(board, baseline):
    raw, identity = identify_device(board)
    require(identity == baseline["identity"], f"{board['id']}: USB identity changed; device was not modified")
    return raw


def protect_device(raw, uid, gid):
    run("setfacl", "-b", str(raw))
    os.chown(raw, uid, gid)
    os.chmod(raw, 0o600)


def restore_device(raw, baseline):
    require(not holders(raw), "UART acquired a new owner; refusing permission rollback")
    run("setfacl", "-b", str(raw))
    os.chown(raw, baseline["uid"], baseline["gid"])
    os.chmod(raw, baseline["mode"])
    run("setfacl", "--set-file=-", str(raw), input=baseline["acl"])
    current = raw.stat()
    require((current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)) ==
            (baseline["uid"], baseline["gid"], baseline["mode"]), "Permission rollback did not match baseline")
    require(run("getfacl", "-cp", str(raw)).stdout == baseline["acl"], "ACL rollback did not match baseline")


def service_state(unit):
    return run("systemctl", "show", unit, "--property=ActiveState", "--value").stdout.strip()


def port_free(address, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((address, port))
    except OSError as error:
        raise HostError(f"Required listening address {address}:{port} is unavailable") from error


def wait_listener(port, timeout=15, address="127.0.0.1"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=.3):
                return
        except OSError:
            time.sleep(.2)
    raise HostError("Broker did not become ready")


def assert_no_writers(config, board):
    """Conserver's documented 15-field status distinguishes writers and spies."""
    status = run("/usr/bin/console", "-n", "-C", "/dev/null", "-E", "-M", "127.0.0.1",
                 "-p", str(config["broker_port"]), "-l", "monitor", "-i", board["id"]).stdout.strip()
    fields = status.split(":")
    require(len(fields) == 15 and fields[0] == board["id"],
            "Cannot safely establish current writer ownership; rollback was not started")
    for user in filter(None, fields[4].split(",")):
        parts = user.split("@")
        require(len(parts) == 5 and parts[0] in ("r", "s") and parts[-1] == "ro",
                f"{board['id']}: a writer or pending writer is connected; release it before rollback")


def verify(config):
    import pwd
    uid = pwd.getpwnam(SERVICE_USER).pw_uid
    for unit in unit_names(config):
        require(service_state(unit) == "active", f"{unit} is not active")
    for board in config["boards"]:
        raw, _ = identify_device(board)
        info = raw.stat()
        require(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o600,
                "Raw device permissions do not restrict access to the broker")
        require(not any(line.startswith("user:") and not line.startswith("user::")
                        for line in run("getfacl", "-cp", str(raw)).stdout.splitlines()),
                "Named ACL grants raw-device access")
        pids = holders(raw)
        require(pids and all(Path(f"/proc/{pid}/comm").read_text().strip() == "conserver" for pid in pids),
                "Unexpected or missing UART owner")
    listeners = run("ss", "-ltnH").stdout.splitlines()
    for port in (config["broker_port"], config["broker_secondary_port"]):
        endpoints = [line.split()[3] for line in listeners if len(line.split()) >= 4 and line.split()[3].endswith(":" + str(port))]
        require(endpoints == ["127.0.0.1:" + str(port)], "Broker listener escaped IPv4 loopback or moved to another port")
    for board in config["boards"]:
        for mode in ("monitor", "control"):
            require(any(len(line.split()) >= 4 and line.split()[3] == f"{config['listen_host']}:{board[mode + '_port']}" for line in listeners),
                    f"{board['id']} {mode} terminal is not listening at the configured address")
    return {"result": "verified", "boards": [b["id"] for b in config["boards"]], "broker": "127.0.0.1"}


def _save_manifest(backup, manifest):
    atomic_write(backup / "baseline.json", json.dumps(manifest, indent=2) + "\n", 0o600)


def ensure_root_directory(path, mode):
    path = Path(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == mode,
                f"{path} must be a root-owned directory with mode {mode:04o}")
    else:
        path.mkdir(mode=mode, parents=True)
        os.chmod(path, mode)


def validate_managed_files(files, allowed):
    """Refuse removal of paths outside our manifest or files edited since install."""
    require(isinstance(files, dict) and set(files) <= set(allowed), "Baseline contains unmanaged file paths")
    for path, digest in files.items():
        require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest), "Invalid baseline file digest")
        require(not Path(path).is_symlink() and (not Path(path).exists() or sha(path) == digest),
                f"{path} changed after installation; refusing to remove local edits")


def install(config, *, activate=False, confirm_idle=False, bin_dir=BIN_DIR, ttyd="/usr/bin/ttyd"):
    """First installation only. Existing files/services are never overwritten."""
    import grp
    import pwd
    require(activate and confirm_idle, "Installation requires --activate --confirm-idle after all UART users release ownership")
    config = validate_config(config)
    files = render_files(config, bin_dir=bin_dir, ttyd=ttyd)
    files["/etc/serial-party/web.env"] = "SERIAL_PARTY_WEB_CREDENTIAL=serialparty:" + secrets.token_urlsafe(32) + "\n"
    require(all(not Path(path).exists() and not Path(path).is_symlink() for path in files),
            "Managed files already exist; inspect them and use the recorded rollback before a fresh installation")
    for program in ("/usr/sbin/conserver", "/usr/bin/console", "udevadm", "systemctl", "fuser", "getfacl", "setfacl", "ss", "useradd"):
        require(shutil.which(program) is not None, f"Missing prerequisite: {program}")
    for entry in ("serial-party-host", "serial-party-console"):
        require(os.access(Path(bin_dir) / entry, os.X_OK), f"Install the Python package in {bin_dir} first")
    check_ttyd(ttyd)
    pwd.getpwnam("www-data")
    require(service_state("conserver-server.service") in ("inactive", "failed"), "The distro conserver-server service is active; release its consoles manually first")
    require(run("systemctl", "is-enabled", "conserver-server.service", check=False).stdout.strip()
            in ("disabled", "masked", "not-found"),
            "The distro conserver-server service can start automatically; coordinate disabling it before activation")
    for unit in unit_names(config):
        require(service_state(unit) in ("inactive", "failed"), "A Serial Party service is already active")
        require(not run("systemctl", "show", unit, "--property=FragmentPath", "--value").stdout.strip(),
                "A service with a Serial Party unit name already exists")
    devices = {board["id"]: snapshot_device(board) for board in config["boards"]}
    require(len({item["rdev"] for item in devices.values()}) == len(devices), "Multiple configured links resolve to the same UART")
    port_free("127.0.0.1", config["broker_port"])
    port_free("127.0.0.1", config["broker_secondary_port"])
    for board in config["boards"]:
        for mode in ("monitor", "control"):
            port_free(config["listen_host"], board[mode + "_port"])
    ensure_root_directory(STATE_DIR, 0o700)
    backup = STATE_DIR / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup.mkdir(mode=0o700)
    manifest = {"version": 1, "config": config, "devices": devices, "files": {}, "created_units": unit_names(config), "complete": False}
    _save_manifest(backup, manifest)
    try:
        # The deployment lock uses umask 077; public config must still be readable
        # by the two unprivileged services. Only web.env itself remains 0600.
        ensure_root_directory("/etc/serial-party", 0o755)
        try:
            account = pwd.getpwnam(SERVICE_USER)
            require(account.pw_shell in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false"), "Existing serialparty account is not a restricted service account")
        except KeyError:
            run("useradd", "--system", "--user-group", "--home-dir", "/nonexistent", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE_USER)
            account = pwd.getpwnam(SERVICE_USER)
        group = grp.getgrnam(SERVICE_USER)
        require(account.pw_uid != 0 and account.pw_gid == group.gr_gid and group.gr_gid != 0,
                "serialparty must have its own unprivileged primary user/group")
        log_dir = Path("/var/log/serial-party")
        if log_dir.exists():
            info = log_dir.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == account.pw_uid
                    and info.st_gid == group.gr_gid and stat.S_IMODE(info.st_mode) == 0o750,
                    "An existing log directory has different ownership or permissions")
        else:
            log_dir.mkdir(mode=0o750)
            os.chown(log_dir, account.pw_uid, group.gr_gid)
            os.chmod(log_dir, 0o750)
        for destination, content in files.items():
            require(not Path(destination).exists() and not Path(destination).is_symlink(), "A destination appeared during installation")
            atomic_write(destination, content, 0o600 if destination.endswith("web.env") else 0o644)
            manifest["files"][destination] = sha(destination)
            _save_manifest(backup, manifest)
        run("/usr/sbin/conserver", "-S", "-C", "/etc/serial-party/conserver.cf")
        for board in config["boards"]:
            raw = same_device(board, devices[board["id"]])
            require(not holders(raw), "UART acquired an owner; installation will not stop it")
            protect_device(raw, account.pw_uid, group.gr_gid)
        run("udevadm", "control", "--reload-rules")
        for board in config["boards"]:
            raw = same_device(board, devices[board["id"]])
            run("udevadm", "trigger", "--action=change", "--subsystem-match=tty", "--sysname-match=" + raw.name)
        run("udevadm", "settle", "--timeout=10")
        for board in config["boards"]:
            raw = same_device(board, devices[board["id"]])
            protect_device(raw, account.pw_uid, group.gr_gid)
            require(not holders(raw), "UART acquired an owner before broker activation")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "serial-party-conserver.service")
        wait_listener(config["broker_port"])
        run("systemctl", "enable", "--now", *unit_names(config)[1:])
        for board in config["boards"]:
            for mode in ("monitor", "control"):
                wait_listener(board[mode + "_port"], address="127.0.0.1" if config["listen_host"] == "0.0.0.0" else config["listen_host"])
        details = verify(config)
        manifest["complete"] = True
        _save_manifest(backup, manifest)
        return {**details, "result": "installed", "backup": str(backup), "credential_file": "/etc/serial-party/web.env"}
    except Exception as error:
        try:
            rollback(backup, confirm_idle=True, automatic=True)
        except Exception as rollback_error:
            raise HostError(f"Installation failed; rollback needs attention: {rollback_error}. Baseline: {backup}") from error
        raise HostError(f"Installation failed and was rolled back: {error}. Baseline: {backup}") from error


def rollback(backup, *, confirm_idle=False, automatic=False):
    require(confirm_idle, "Rollback requires --confirm-idle after users release their consoles")
    backup = Path(backup).resolve(strict=True)
    require(backup.parent == STATE_DIR.resolve(), "Rollback must use a managed backup directory")
    info = (backup / "baseline.json").stat()
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600, "Backup manifest must be root-owned mode 0600")
    manifest = json.loads((backup / "baseline.json").read_text())
    require(manifest.get("version") == 1 and not manifest.get("rolled_back"), "Unsupported or already restored baseline")
    config = validate_config(manifest["config"])
    allowed = set(render_files(config)) | {"/etc/serial-party/web.env"}
    validate_managed_files(manifest["files"], allowed)
    if service_state("serial-party-conserver.service") == "active":
        for board in config["boards"]:
            assert_no_writers(config, board)
    # Validate all identities before stopping anything. Never stop unknown UART holders.
    for board in config["boards"]:
        raw = same_device(board, manifest["devices"][board["id"]])
        for pid in holders(raw):
            require(Path(f"/proc/{pid}/comm").read_text().strip() == "conserver", "Unknown UART owner prevents rollback")
    for unit in reversed(unit_names(config)):
        # A failed early install may not have written every unit yet.
        if f"/etc/systemd/system/{unit}" in manifest["files"]:
            run("systemctl", "stop", unit)
            require(service_state(unit) in ("inactive", "failed"), "Could not stop a managed service")
            run("systemctl", "disable", unit)
    for path in manifest["files"]:
        Path(path).unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    run("udevadm", "control", "--reload-rules")
    for board in config["boards"]:
        raw = same_device(board, manifest["devices"][board["id"]])
        require(not holders(raw), "A new UART owner appeared; permissions were not changed")
        run("udevadm", "trigger", "--action=change", "--subsystem-match=tty", "--sysname-match=" + raw.name)
    run("udevadm", "settle", "--timeout=10")
    for board in config["boards"]:
        restore_device(same_device(board, manifest["devices"][board["id"]]), manifest["devices"][board["id"]])
    manifest["rolled_back"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_manifest(backup, manifest)
    return {"result": "rolled_back", "backup": str(backup), "automatic": automatic,
            "retained": "Service account, logs, Python environment and backup are retained."}


@contextmanager
def host_lock():
    import fcntl
    require(sys.platform.startswith("linux") and os.geteuid() == 0, "Host management requires root on Linux")
    os.umask(0o077)
    fd = os.open("/run/lock/serial-party.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "render", "launch-terminal", "install", "verify", "rollback"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", default=argparse.SUPPRESS)
        if name in ("render", "install", "launch-terminal"):
            sub.add_argument("--bin-dir", default=BIN_DIR)
            sub.add_argument("--ttyd", default="/usr/bin/ttyd")
        if name == "render":
            sub.add_argument("--output", required=True, type=Path)
        elif name == "install":
            sub.add_argument("--activate", action="store_true")
            sub.add_argument("--confirm-idle", action="store_true")
        elif name == "launch-terminal":
            sub.add_argument("--board", required=True)
            sub.add_argument("--mode", required=True, choices=("monitor", "control"))
        elif name == "rollback":
            sub.add_argument("--backup", required=True, type=Path)
            sub.add_argument("--confirm-idle", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "rollback":
            with host_lock():
                result = rollback(args.backup, confirm_idle=args.confirm_idle)
        else:
            config = load_config(args.config)
            if args.command == "validate":
                result = {"result": "valid", "boards": [board["id"] for board in config["boards"]]}
            elif args.command == "render":
                result = render(config, args.output, bin_dir=args.bin_dir, ttyd=args.ttyd)
            elif args.command == "launch-terminal":
                credential = os.environ.pop("SERIAL_PARTY_WEB_CREDENTIAL", "")
                command = terminal_argv(config, get_board(config, args.board), args.mode, credential,
                                        config_path=args.config or os.environ.get("SERIAL_PARTY_CONFIG", DEFAULT_CONFIG),
                                        bin_dir=args.bin_dir, ttyd=args.ttyd)
                os.execv(command[0], command)
            else:
                with host_lock():
                    result = install(config, activate=args.activate, confirm_idle=args.confirm_idle,
                                     bin_dir=args.bin_dir, ttyd=args.ttyd) if args.command == "install" else verify(config)
        print(json.dumps(result, indent=2))
        return 0
    except (ConfigError, HostError, OSError, KeyError, subprocess.SubprocessError, ValueError) as error:
        print(f"Serial Party: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
