#!/usr/bin/env python3
"""Install one pinned upstream ttyd release after SHA-256 verification.

This helper never opens a serial device or starts a service. It refuses to
replace an existing file with different contents. No binary is bundled here.
"""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import tempfile
import urllib.request

VERSION = "1.7.7"
# Published in https://github.com/tsl0922/ttyd/releases/download/1.7.7/SHA256SUMS
RELEASES = {
    "aarch64": "b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165",
    "x86_64": "8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="explicitly authorize installing the verified executable")
    parser.add_argument("--destination", default="/usr/local/bin/ttyd", type=Path)
    args = parser.parse_args()
    if not args.install or platform.system() != "Linux" or os.geteuid() != 0:
        parser.error("run as root on Linux with --install; supports aarch64 and x86_64")
    architecture = platform.machine()
    if architecture not in RELEASES:
        parser.error("unsupported architecture; install a compatible ttyd with your package manager")
    destination = args.destination
    if not destination.is_absolute() or destination.is_symlink():
        parser.error("destination must be an absolute path, not a symlink")
    digest = RELEASES[architecture]
    if destination.exists():
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            print(f"Verified ttyd {VERSION} is already installed at {destination}")
            return
        parser.error("destination already exists with different contents; choose another path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/tsl0922/ttyd/releases/download/{VERSION}/ttyd.{architecture}"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".ttyd-", delete=False) as output:
            temporary = Path(output.name)
            checksum = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(url, timeout=60) as response:
                if not response.url.startswith("https://"):
                    raise RuntimeError("Download was redirected away from HTTPS")
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise RuntimeError("Unexpectedly large download")
                    checksum.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if checksum.hexdigest() != digest:
            raise RuntimeError("SHA-256 did not match the pinned upstream release; nothing was installed")
        os.chmod(temporary, 0o755)
        # link() fails if the destination appeared meanwhile; never overwrite it.
        os.link(temporary, destination)
        print(f"Installed verified ttyd {VERSION} at {destination}; no services started")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
