"""
Install gnirehtet (reverse tethering) without Administrator rights.

Downloads the official Genymobile release for this OS, unpacks it into a
per-user folder, and marks the executable runnable. Nothing is installed
system-wide and no elevation is requested.

  py src/scripts/INSTALL_GNIREHTET.py            # install / update
  py src/scripts/INSTALL_GNIREHTET.py --check    # report only
  py src/scripts/INSTALL_GNIREHTET.py --dir D    # custom target folder

After it finishes, either leave the folder where it is (the Windstock launcher
looks in the default locations) or set the GNIREHTET environment variable to the
full path of the executable.
"""
import argparse
import hashlib
import os
import platform
import stat
import sys
import urllib.request
import zipfile

VERSION = "v2.5.1"
BASE = f"https://github.com/Genymobile/gnirehtet/releases/download/{VERSION}"

# Official release archives + their published SHA-256 (from the project README).
RELEASES = {
    "windows": (f"gnirehtet-rust-win64-{VERSION}.zip",
                "7f5b1063e7895182aa60def1437e50363c3758144088dcd079037bb7c3c46a1c"),
    "linux": (f"gnirehtet-rust-linux64-{VERSION}.zip",
              "dee55499ca4fef00ce2559c767d2d8130163736d43fdbce753e923e75309c275"),
}

EXE_NAME = "gnirehtet.exe" if os.name == "nt" else "gnirehtet"


def default_dir():
    """A per-user folder the Windstock launcher already looks in."""
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(home, "gnirehtet")
    return os.path.join(home, ".local", "share", "gnirehtet")


def platform_key():
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system.startswith("linux"):
        return "linux"
    return None


def existing(directory):
    path = os.path.join(directory, EXE_NAME)
    return path if os.path.exists(path) else None


def download(url, dest, log):
    log(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, \
            open(dest, "wb") as out:
        out.write(resp.read())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(directory, log=print):
    key = platform_key()
    if key is None:
        log(f"!! Unsupported OS for a prebuilt release: {platform.system()}")
        log("   Build gnirehtet from source: "
            "https://github.com/Genymobile/gnirehtet")
        return None

    name, want_sha = RELEASES[key]
    url = f"{BASE}/{name}"
    os.makedirs(directory, exist_ok=True)

    zip_path = os.path.join(directory, name)
    try:
        download(url, zip_path, log)
    except Exception as e:
        log(f"!! download failed: {e}")
        log("   Check your internet connection, or download the release by hand "
            f"from {BASE}")
        return None

    got_sha = sha256(zip_path)
    if got_sha != want_sha:
        log("!! checksum mismatch -- refusing to use the archive")
        log(f"   expected {want_sha}")
        log(f"   got      {got_sha}")
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        return None
    log("  checksum OK")

    log(f"  unpacking into {directory}")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(directory)

    # The archive has one top-level folder; move its contents up so the
    # executable sits directly in `directory`.
    entries = [e for e in os.listdir(directory)
               if os.path.isdir(os.path.join(directory, e))]
    for entry in entries:
        src_dir = os.path.join(directory, entry)
        if not os.path.exists(os.path.join(src_dir, EXE_NAME)):
            continue
        for item in os.listdir(src_dir):
            os.replace(os.path.join(src_dir, item),
                       os.path.join(directory, item))
        os.rmdir(src_dir)
    try:
        os.unlink(zip_path)
    except OSError:
        pass

    exe = existing(directory)
    if not exe:
        log("!! unpacked, but the executable was not found -- archive layout "
            "changed?")
        return None

    if os.name != "nt":
        mode = os.stat(exe).st_mode
        os.chmod(exe, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    apk = os.path.join(directory, "gnirehtet.apk")
    if not os.path.exists(apk):
        log("!! note: gnirehtet.apk is missing next to the executable; "
            "installing the phone app may fail")
    return exe


def main():
    ap = argparse.ArgumentParser(description="Install gnirehtet (no admin).")
    ap.add_argument("--dir", default=None,
                    help="target folder (default: a per-user folder)")
    ap.add_argument("--check", action="store_true",
                    help="report whether gnirehtet is already installed")
    ap.add_argument("--force", action="store_true",
                    help="reinstall even if it is already present")
    args = ap.parse_args()

    directory = args.dir or default_dir()
    found = existing(directory)

    if args.check:
        if found:
            print(f"gnirehtet is installed: {found}")
            return 0
        print(f"gnirehtet is NOT installed in {directory}")
        return 1

    if found and not args.force:
        print(f"gnirehtet is already installed: {found}")
        print("(use --force to reinstall)")
        return 0

    print(f"Installing gnirehtet {VERSION} into {directory}")
    exe = install(directory)
    if not exe:
        print("\nInstall failed.")
        return 1

    print("\nDone.")
    print(f"  executable : {exe}")
    print(f"  phone app  : {os.path.join(directory, 'gnirehtet.apk')}")
    print("\nStart playing with:")
    print("  py run.py --gnirehtet")
    print("\nIf Windstock can't find it, set the GNIREHTET environment variable:")
    print(f'  setx GNIREHTET "{exe}"      (Windows, then reopen the terminal)')
    print(f'  export GNIREHTET="{exe}"    (Linux/macOS)')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
