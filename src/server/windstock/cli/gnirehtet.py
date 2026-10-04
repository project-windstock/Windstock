"""
Reverse tethering for the launcher (gnirehtet) -- play with no Wi-Fi DNS change,
no Administrator rights on the PC, and no root on the phone.

How it fits together
--------------------
`gnirehtet <https://github.com/Genymobile/gnirehtet>`_ hands the phone ALL of its
internet through this PC over the USB cable -- including UDP DNS. Crucially,
``gnirehtet start <serial> -d <PC_IP>`` also tells the phone to use **this PC as
its DNS server**, so:

  * the DNS redirector (``net/dns_redirect.py``) answers the Niantic/PTC
    hostnames with this PC's address,
  * the game's HTTPS traffic tunnels back over the cable to the game server, and
  * no phone-side DNS app is needed -- the phone's DNS is pointed at us by
    gnirehtet itself.

The CA (``certs/ca.crt``) is copied to the phone for its one-tap install.

Get gnirehtet with ``py src/scripts/INSTALL_GNIREHTET.py`` (no admin). See
``GNIREHTET.md`` for the beginner-friendly walkthrough.
"""
import os
import shutil
import subprocess
import time

RELAY_PORT = 31416


# ============================================================ tool discovery

def _common_adb_paths():
    paths = []
    local = os.environ.get("LOCALAPPDATA")
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if local:
        paths.append(os.path.join(local, "Android", "Sdk", "platform-tools",
                                  "adb.exe"))
    if home:
        paths.append(os.path.join(home, "adb", "platform-tools", "adb.exe"))
    paths += [
        r"C:\platform-tools\adb.exe",
        "/usr/bin/adb",
        "/usr/local/bin/adb",
        "/opt/homebrew/bin/adb",
    ]
    return paths


def find_adb():
    """``ADB`` env first (the tools use that convention), then PATH, then the
    usual SDK / standalone install locations."""
    env = os.environ.get("ADB")
    if env and os.path.exists(env):
        return env
    for name in ("adb", "adb.exe"):
        found = shutil.which(name)
        if found:
            return found
    for path in _common_adb_paths():
        if path and os.path.exists(path):
            return path
    return None


def _common_gnirehtet_paths():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA")
    paths = [
        # what src/scripts/INSTALL_GNIREHTET.py installs
        os.path.join(home, "gnirehtet", "gnirehtet.exe"),
        os.path.join(home, ".local", "share", "gnirehtet", "gnirehtet"),
        os.path.join(home, "Downloads", "gnirehtet", "gnirehtet.exe"),
        os.path.join(home, "Downloads", "gnirehtet-rust-win64", "gnirehtet.exe"),
        "/usr/local/bin/gnirehtet",
    ]
    if local:
        paths.append(os.path.join(local, "Microsoft", "WinGet", "Links",
                                  "gnirehtet.exe"))
    return paths


def find_gnirehtet():
    """Locate the gnirehtet executable (``GNIREHTET`` env, PATH, then the usual
    install/unzip locations). None when it is not installed."""
    env = os.environ.get("GNIREHTET")
    if env and os.path.exists(env):
        return env
    for name in ("gnirehtet", "gnirehtet.exe"):
        found = shutil.which(name)
        if found:
            return found
    for path in _common_gnirehtet_paths():
        if path and os.path.exists(path):
            return path
    return None


# ============================================================ process helpers

def _run_tool(exe, args, timeout=30, env=None, cwd=None):
    try:
        return subprocess.run([exe] + list(args), capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None


def _run(adb, args, timeout=30):
    try:
        return subprocess.run([adb] + list(args), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def list_devices(adb):
    """[(serial, state)] from ``adb devices``."""
    r = _run(adb, ["devices"])
    if not r or r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


def push_ca(adb, serial, cert, log=print):
    """Copy the CA to a folder the phone's file manager can see, for one-tap
    install. ADB cannot accept the certificate dialog for the user."""
    if not cert or not os.path.exists(cert):
        return False
    r = _run(adb, ["push", cert, "/sdcard/Download/windstock-ca.crt"], timeout=60)
    if r and r.returncode == 0:
        log("[gnirehtet] CA copied to the phone (Download/windstock-ca.crt) -- "
            "install it as a user certificate")
        return True
    log("[gnirehtet] could not copy the CA -- push certs/ca.crt to the phone by "
        "hand")
    return False


# ============================================================ gnirehtet

def find_apk(exe=None):
    """Locate ``gnirehtet.apk``.

    ``GNIREHTET_APK`` env first, then next to gnirehtet.exe, then the copy
    bundled in the repository (``src/server/gnirehtet.apk``). The bundled copy
    means a fresh checkout works even when gnirehtet was installed without its
    APK beside it.
    """
    env = os.environ.get("GNIREHTET_APK")
    if env and os.path.exists(env):
        return env
    if exe:
        beside = os.path.join(os.path.dirname(os.path.abspath(exe)),
                              "gnirehtet.apk")
        if os.path.exists(beside):
            return beside
    here = os.path.dirname(os.path.abspath(__file__))          # .../cli
    bundled = os.path.join(os.path.dirname(os.path.dirname(here)),
                           "gnirehtet.apk")                    # .../server
    if os.path.exists(bundled):
        return bundled
    return None


def start_gnirehtet(serial, dns_ip, adb=None, gnirehtet=None, log=print):
    """Reverse-tether the phone through this PC and point its DNS at us.

    Starts the relay in the background, installs the phone app if needed, then
    starts the client with ``-d dns_ip``. Returns the relay's Popen handle.
    """
    exe = gnirehtet or find_gnirehtet()
    if not exe:
        log("[gnirehtet] not found -- run py src/scripts/INSTALL_GNIREHTET.py, "
            "or set the GNIREHTET environment variable (see GNIREHTET.md)")
        return None

    # gnirehtet looks for gnirehtet.apk relative to the WORKING DIRECTORY, which
    # is the server folder when the launcher runs -- so it never finds the copy
    # sitting next to gnirehtet.exe. Run from the executable's own folder and
    # point GNIREHTET_APK at the APK explicitly.
    exe_dir = os.path.dirname(os.path.abspath(exe))
    env = dict(os.environ)
    if adb:
        env.setdefault("ADB", adb)      # gnirehtet shells out to adb itself
    apk = find_apk(exe)
    if apk:
        env.setdefault("GNIREHTET_APK", apk)
    else:
        log("[gnirehtet] note: no gnirehtet.apk found (beside gnirehtet.exe or "
            "in src/server); phone-side install may fail")

    try:
        relay = subprocess.Popen([exe, "relay"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.STDOUT, env=env, cwd=exe_dir)
    except OSError as e:
        log(f"[gnirehtet] could not start the relay: {e}")
        return None
    log("[gnirehtet] relay started -- this PC now provides the phone's internet")
    time.sleep(1.5)

    _run_tool(exe, ["install"] + ([serial] if serial else []), timeout=180,
              env=env, cwd=exe_dir)
    args = ["start"] + ([serial] if serial else [])
    if dns_ip:
        args += ["-d", dns_ip]
    r = _run_tool(exe, args, timeout=90, env=env, cwd=exe_dir)
    if r and r.returncode == 0:
        log(f"[gnirehtet] reverse tethering ON -- phone DNS set to {dns_ip}; "
            "accept the VPN prompt on the phone the first time")
    else:
        detail = (r.stderr or r.stdout or "").strip() if r else "gnirehtet failed"
        log(f"[gnirehtet] could not start the phone client: {detail}")
    return relay


def stop_gnirehtet(serial, relay, gnirehtet=None, log=print):
    exe = gnirehtet or find_gnirehtet()
    if exe and serial:
        _run_tool(exe, ["stop", serial], timeout=30,
                  cwd=os.path.dirname(os.path.abspath(exe)))
    if relay is not None:
        try:
            relay.terminate()
        except Exception:
            pass
    if exe:
        log("[gnirehtet] reverse tethering stopped")


# ============================================================ entry points

def start(cert=None, dns_ip=None, log=print, adb=None, gnirehtet=None):
    """Check the phone, copy the CA, and start reverse tethering.

    Returns a session dict (``serial``/``relay``) for :func:`teardown`.
    """
    state = {"serial": None, "relay": None}
    adb = adb or find_adb()
    if not adb:
        log("[gnirehtet] adb not found -- install Android platform-tools, or set "
            "the ADB environment variable to adb's path")
        return state

    devices = list_devices(adb)
    if "unauthorized" in {s for _, s in devices}:
        log("[gnirehtet] phone is 'unauthorized' -- tap ALLOW on the 'Allow USB "
            "debugging?' prompt, then run again")
        return state
    ready = [s for s, st in devices if st == "device"]
    if not ready:
        log("[gnirehtet] no authorized device -- plug the phone in and enable USB "
            "debugging")
        return state
    serial = ready[0]
    state["serial"] = serial
    if len(ready) > 1:
        log(f"[gnirehtet] {len(ready)} devices attached; using {serial}")

    if cert:
        push_ca(adb, serial, cert, log=log)
    state["relay"] = start_gnirehtet(serial, dns_ip, adb=adb,
                                     gnirehtet=gnirehtet, log=log)
    return state


def teardown(state, log=print):
    """Stop reverse tethering and the relay."""
    if not state:
        return
    stop_gnirehtet(state.get("serial"), state.get("relay"), log=log)
