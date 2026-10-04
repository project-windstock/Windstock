#!/usr/bin/env python3
"""
src/scripts/DOWNLOAD.py -- install the project's Python dependencies.

One command, every OS. It finds the right Python, makes sure pip exists, and
installs the packages the server and tools need.

    py src/scripts/DOWNLOAD.py               # install everything
    py src/scripts/DOWNLOAD.py --group core  # just the server runtime
    py src/scripts/DOWNLOAD.py --group assets  # the Asset Editor's stack
    py src/scripts/DOWNLOAD.py --list        # show what would be installed
    py src/scripts/DOWNLOAD.py --check       # report what is already installed
    py src/scripts/DOWNLOAD.py --dry-run     # print the commands, run nothing

Groups
------
    core     the game server's own runtime (s2sphere)
    db       the shared MariaDB backend at the repository root
    poi      OpenStreetMap data fetching (osmium)
    patcher  the desktop APK patcher GUI (customtkinter)
    assets   the Asset Editor: UnityPy==1.25.2, Pillow, numpy, pycryptodome,
             crcmod, lz4. Deliberately NOT needed to run the server.
    tools    protobuf / data-building tooling
    all      every group above (the default)

Elevation
---------
Linux   Installs system-wide ("root's profile") with sudo BY DEFAULT, so the
        game server, the World Manager and the tools all share one interpreter.
        Already running as root (containers, CI) -> sudo is skipped. Use
        --user for a per-user install, --venv for a throwaway virtualenv, or
        --no-sudo to use the current environment exactly as-is.
macOS   Normal install into the running interpreter; no sudo. Homebrew Python
        is user-writable, so this usually just works.
Windows No sudo exists, so it installs into the running interpreter.

Environment
-----------
    DOWNLOAD_GROUP   default group (core|db|poi|patcher|assets|tools|all)
    NO_COLOR         set to any value to disable the coloured output
"""

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

# --------------------------------------------------------------------------
# Dependencies, grouped. Each entry is (pip spec, import name to verify with).
# The spec may pin a version ("UnityPy==1.25.2") -- a pin is not decoration:
# importable() is true for the WRONG version too, so a pin is what lets
# --check notice that the installed one does not match. Kept in one place so
# the installer, --list and --check can never disagree.
#
# Nothing standard-library appears here on purpose: `threading` is not on PyPI
# at all (naming it made `pip install -r requirements.txt` fail), and the
# Python-2 `pathlib` backport that IS on PyPI can shadow the real one.
# --------------------------------------------------------------------------
GROUPS = {
    # The server itself refuses to start without this one.
    "core": [("s2sphere", "s2sphere")],
    # database.py's shared backend. SQLite needs no install.
    "db": [("mariadb", "mariadb")],
    # Downloading real PokeStop/Gym data from OpenStreetMap.
    "poi": [("s2sphere", "s2sphere"), ("osmium", "osmium")],
    # The desktop APK patcher GUI (src/patcher/patcher.py).
    "patcher": [("customtkinter", "customtkinter")],
    # The Asset Editor (src/asset_tools). The server imports NONE of this --
    # that is the point of keeping the tools out of src/server.
    #
    # UnityPy is pinned to 1.25.2 and must stay on 1.x: unitypy_fix is written
    # against that API, and its whole reason for existing is a 1.x save bug
    # (plain UnityPy clears every MonoBehaviour's script_type_index, which greys
    # the screen on the phone). 2.x changed the object model, and the editor
    # would appear to work right up until it wrote broken bundles.
    "assets": [
        ("UnityPy==1.25.2", "UnityPy"),
        ("Pillow", "PIL"),
        ("numpy", "numpy"),
        ("pycryptodome", "Crypto"),
        ("crcmod", "crcmod"),
        ("lz4", "lz4"),
    ],
    # Asset extraction / protobuf tooling. Optional; only needed to build data.
    "tools": [
        ("UnityPy==1.25.2", "UnityPy"),
        ("Pillow", "PIL"),
        ("pycryptodome", "Crypto"),
        ("protobuf", "google.protobuf"),
    ],
}
DEFAULT_GROUP = os.environ.get("DOWNLOAD_GROUP", "all").strip().lower() or "all"


# --------------------------------------------------------------------------
# Tiny colour helper (same idea as the server's run.py). Off when piped or
# NO_COLOR is set; Windows gets VT processing enabled where possible.
# --------------------------------------------------------------------------
def _colour_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            k32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            return False
    return True


_COLOR = _colour_enabled()


def c(text, *codes):
    if not _COLOR or not codes:
        return str(text)
    return "\033[" + ";".join(str(x) for x in codes) + "m" + str(text) + "\033[0m"


BOLD, DIM = 1, 2
RED, GREEN, YELLOW, CYAN, MAGENTA = 31, 32, 33, 36, 35
BGREEN, BCYAN, BYELLOW = 92, 96, 93


def info(msg):
    print(c("  -> ", BOLD, BCYAN) + msg)


def ok(msg):
    print(c("  ok ", BOLD, BGREEN) + msg)


def warn(msg):
    print(c("  !! ", BOLD, BYELLOW) + msg)


def fail(msg):
    print(c("  xx ", BOLD, RED) + msg)


def header(msg):
    print()
    print(c("=" * 66, BCYAN))
    print(c("  " + msg, BOLD))
    print(c("=" * 66, BCYAN))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def packages_for(group):
    """The (pip, import) list for a group name, de-duplicated."""
    group = (group or DEFAULT_GROUP).lower()
    if group == "all":
        seen, out = set(), []
        for entries in GROUPS.values():
            for pkg, mod in entries:
                if pkg.lower() not in seen:
                    seen.add(pkg.lower())
                    out.append((pkg, mod))
        return out
    if group not in GROUPS:
        raise SystemExit(f"unknown group {group!r}; "
                         f"use one of: {', '.join(GROUPS)}, all")
    return GROUPS[group]


def importable(module):
    """True when `module` can be imported (dotted names allowed)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def split_spec(spec):
    """'UnityPy==1.25.2' -> ('UnityPy', '==', '1.25.2'); 'Pillow' -> ('Pillow', None, None)."""
    for sep in ("==", ">=", "<=", "~=", ">", "<"):
        if sep in spec:
            name, _, want = spec.partition(sep)
            return name.strip(), sep, want.strip()
    return spec.strip(), None, None


def installed_version(name):
    """Installed version of a distribution, or None if it is not installed."""
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        return None


def _version_ok(have, op, want):
    """Does `have` satisfy `op want`?

    `packaging` does this properly and is present in almost every environment,
    so it is used when available. Without it, only '==' is decided (by exact
    string) and a range falls back to "importable, version unverified" -- which
    is the honest answer rather than a wrong one: comparing '0.2.5' >= '0.2'
    as strings says False for a version that plainly satisfies it.
    """
    try:
        from packaging.specifiers import SpecifierSet
        return SpecifierSet(f"{op}{want}").contains(have or "", prereleases=True)
    except Exception:
        pass
    if op == "==":
        return have == want
    return True                      # unverified, but not a failure


def status(spec, mod):
    """(satisfied, detail) for one entry.

    A pin is checked against the INSTALLED version, not merely against
    importability: UnityPy 2.x imports perfectly happily and is still wrong
    here, so an import-only check would report the editor as ready when its
    selftest() would refuse to write a single bundle.
    """
    name, op, want = split_spec(spec)
    if not importable(mod):
        return False, "not installed"
    have = installed_version(name)
    if op is None:
        return True, have or "importable"
    if _version_ok(have, op, want):
        return True, have
    return False, f"have {have or 'unknown'}, need {op}{want}"


def venv_python(path):
    return os.path.join(path, "Scripts" if IS_WINDOWS else "bin",
                        "python.exe" if IS_WINDOWS else "python")


def ensure_venv(path, prefix):
    py = venv_python(path)
    if os.path.exists(py):
        ok(f"using existing virtualenv at {path}")
        return py
    info(f"creating virtualenv at {path}")
    subprocess.run(prefix + [sys.executable, "-m", "venv", path], check=True)
    if not os.path.exists(py):                       # pragma: no cover
        raise SystemExit(f"virtualenv was not created at {path}")
    return py


def elevation_prefix(args):
    """['sudo'] on Linux when installing system-wide, else []."""
    if not IS_LINUX or args.no_sudo or args.user or args.venv:
        return []
    if getattr(os, "geteuid", lambda: 1)() == 0:     # already root
        return []
    if shutil.which("sudo") is None:
        warn("sudo not found; installing into the current environment instead")
        return []
    return ["sudo"]


def have_pip(python_exe, prefix):
    try:
        return subprocess.run(prefix + [python_exe, "-m", "pip", "--version"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def ensure_pip(python_exe, prefix):
    if have_pip(python_exe, prefix):
        return True
    info("pip is missing; bootstrapping it with ensurepip")
    try:
        rc = subprocess.run(prefix + [python_exe, "-m", "ensurepip", "--upgrade"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    except OSError as e:
        fail(f"could not run {python_exe}: {e}")
        return False
    if rc != 0 or not have_pip(python_exe, prefix):
        fail("pip is unavailable and could not be bootstrapped")
        return False
    return True


def pip_install(python_exe, prefix, specs, args):
    """Run pip install, transparently handling PEP 668 'externally managed'."""
    cmd = prefix + [python_exe, "-m", "pip", "install"] + list(specs)
    if args.user:
        cmd.append("--user")
    if args.upgrade:
        cmd.append("--upgrade")
    if args.force:
        cmd.append("--force-reinstall")
    if args.index_url:
        cmd += ["--index-url", args.index_url]

    info("$ " + " ".join(cmd))
    if args.dry_run:
        return True

    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        return True

    blob = (proc.stdout + proc.stderr).lower()
    if "externally-managed-environment" in blob:
        # Debian/Ubuntu 23+, Homebrew: system pip now refuses to touch the
        # interpreter's own site-packages unless explicitly told it is okay.
        warn("this Python is externally managed (PEP 668); retrying with "
             "--break-system-packages")
        cmd2 = cmd + ["--break-system-packages"]
        info("$ " + " ".join(cmd2))
        return subprocess.run(cmd2).returncode == 0
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="DOWNLOAD.py",
        description="Install the project's Python dependencies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--group", "-g", default=DEFAULT_GROUP,
                   choices=list(GROUPS) + ["all"],
                   help="which set of dependencies to install (default: %(default)s)")
    p.add_argument("--list", "-l", action="store_true",
                   help="show the packages for the chosen group and exit")
    p.add_argument("--check", action="store_true",
                   help="report which packages are importable; install nothing")
    p.add_argument("--venv", nargs="?", const=".venv", metavar="PATH",
                   help="install into a virtualenv (created if missing)")
    p.add_argument("--user", action="store_true",
                   help="install into the per-user site-packages (pip --user)")
    p.add_argument("--no-sudo", action="store_true",
                   help="never elevate, even on Linux")
    p.add_argument("--upgrade", action="store_true", default=True,
                   help="upgrade already-installed packages (default: yes)")
    p.add_argument("--no-upgrade", dest="upgrade", action="store_false",
                   help="leave already-satisfied packages alone")
    p.add_argument("--force", action="store_true",
                   help="reinstall even if already present")
    p.add_argument("--index-url", metavar="URL", default=None,
                   help="use an alternate package index (mirrors, proxies)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the pip commands without running them")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.user and args.venv:
        raise SystemExit("--user and --venv are mutually exclusive")

    packages = packages_for(args.group)

    print()
    print(c("  Windstock", BOLD, BCYAN) + c("  dependency installer", DIM))
    print(c("  Python " + platform.python_version()
            + " on " + platform.system() + " " + platform.release(), DIM))
    print(c("  group: " + args.group, DIM))

    if args.list:
        header(f"Packages in group '{args.group}'")
        for spec, mod in packages:
            good, detail = status(spec, mod)
            mark = c("[have]", BGREEN) if good else c("[need]", BYELLOW)
            print(f"  {mark}  {spec:<18} (import: {mod}, {detail})")
        print()
        return 0

    if args.check:
        header("Dependency check")
        missing = []
        for spec, mod in packages:
            good, detail = status(spec, mod)
            if good:
                ok(f"{spec:<18} {detail}")
            else:
                fail(f"{spec:<18} MISSING ({detail}, import: {mod})")
                missing.append(split_spec(spec)[0])
        print()
        if missing:
            warn(f"{len(missing)} missing or the wrong version -- run this "
                 "script to install them")
            return 1
        ok("all dependencies are present, at the right versions")
        return 0

    # Decide which interpreter (and whether to elevate).
    prefix = elevation_prefix(args)
    if args.venv:
        if args.dry_run:
            python_exe = venv_python(args.venv)
            info(f"would create/use virtualenv at {args.venv} (dry run)")
        else:
            python_exe = ensure_venv(args.venv, prefix)
        prefix = []                                   # venv needs no sudo
    else:
        python_exe = sys.executable

    if prefix:
        info("installing system-wide with sudo (use --user or --venv to avoid)")
    elif args.user:
        info("installing into the per-user site-packages")
    elif args.venv:
        info(f"installing into {args.venv}")

    if not args.dry_run and not ensure_pip(python_exe, prefix):
        return 1

    # Pass a package to pip when it is missing, at the WRONG version, or when
    # upgrading was asked for. A pin is always re-asserted: that is the whole
    # point of pinning, since the wrong version still imports.
    todo = [spec for spec, mod in packages
            if args.force or args.upgrade or not status(spec, mod)[0]]

    header(f"Installing {len(todo)} of {len(packages)} packages")
    if not todo:
        ok("everything is already installed at the right versions")
        print()
        return 0

    if not pip_install(python_exe, prefix, todo, args):
        print()
        fail("installation failed -- see the pip output above")
        return 1

    if args.dry_run:
        print()
        ok("dry run complete; nothing was installed")
        return 0

    header("Verifying")
    missing = [spec for spec, mod in packages if not status(spec, mod)[0]]
    for spec, mod in packages:
        good, detail = status(spec, mod)
        (ok if good else fail)(f"{spec:<18} {detail if good else 'STILL WRONG'}")
    print()
    if missing:
        warn("some packages need a new shell/session before Python sees them "
             "(or were not installed)")
        return 1
    ok("all dependencies installed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        warn("interrupted")
        sys.exit(130)
