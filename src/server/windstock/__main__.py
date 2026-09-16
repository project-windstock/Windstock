"""
Windstock launcher -- ONE process that runs everything: the DNS redirector
(UDP 53) and the game server (TCP 443, fake PTC SSO + RPC), plus the localhost
World Manager and the interactive slash console.

  Local (same Wi-Fi as the phone):
      py run.py                      # auto-detects this PC's LAN IP
  Remote (server with a public IP / VPS):
      py run.py 203.0.113.7          # the IP the phone should be pointed at
      RUN_IP=203.0.113.7 py run.py

Point the phone's Wi-Fi DNS at the IP this prints. Both 53/udp and 443/tcp must
be reachable from the phone (open them in the firewall; on Windows low ports are
fine without admin, but a firewall *allow* rule for inbound may be needed).

Build a standalone .exe (no Python needed on the target):
      py -m PyInstaller --onefile --name pogo-server \
         --add-data "certs;certs" --collect-all s2sphere run.py
"""
import os
import sys
import threading
import time

# Allow both ``py run.py`` and ``python -m windstock``: make sure the folder that
# CONTAINS the package is importable before we touch any package module.
if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from windstock.cli.console import (  # noqa: E402
    start_console, show_banner, rule, kv, paint)
from windstock.cli.logfile import _setup_logging  # noqa: E402
from windstock.cli.network import detect_ip, _local_ips, _usable  # noqa: E402
from windstock.cli.theme import (  # noqa: E402
    BOLD, DIM, YELLOW, MAGENTA, BRED, BGREEN, BYELLOW, BBLUE, BMAGENTA, BCYAN)


def main(ui_stream=None):
    """Run everything. Blocks in server.main(). A GUI window can call this on a
    background thread and pass its activity pane as ``ui_stream``."""
    _setup_logging(ui_stream)
    redirect_ip = (sys.argv[1] if len(sys.argv) > 1
                   else os.environ.get("RUN_IP") or detect_ip())
    # Show the alternatives. When the phone can't reach the server, the first
    # thing to check is whether this is even the right address for the network
    # the phone is on -- so make it impossible to miss.
    others = [ip for ip in _local_ips()
              if _usable(ip) and ip != redirect_ip]
    if others:
        print(paint("  (this PC is also reachable at: ", DIM)
              + paint(", ".join(others), BYELLOW)
              + paint(" -- if the phone can't connect, run this with the right"
                      " one as an argument)", DIM))

    # Hand config to the imported modules via env (read at their import time).
    os.environ["REDIRECT_IP"] = redirect_ip
    os.environ.setdefault("PORT", "443")
    os.environ.setdefault("BIND", "0.0.0.0")
    os.environ.setdefault("DNS_PORT", "53")
    from windstock.config import paths
    os.environ.setdefault("CERT_DIR", paths.CERT_DIR)
    # Serve the real 2016 game master so the client has Pokemon templates and will
    # actually request + render the wild Pokemon (asset bundles now shipped in
    # assets/). Set SERVE_GAME_MASTER=0 to fall back to the bare template set.
    os.environ.setdefault("SERVE_GAME_MASTER", "1")

    from windstock.net import server
    from windstock.net import dns_redirect
    from windstock.net import sso_bridge

    if not (os.path.exists(server.CERT) and os.path.exists(server.KEY)):
        print(paint("!! ", BOLD, BRED)
              + paint(f"TLS certs not found in {os.environ['CERT_DIR']}.", BRED))
        print(paint("   Run gen_certs.py once, or ship the certs/ folder "
                    "alongside this.", DIM))
        sys.exit(1)

    # World Manager (localhost only -- never exposed to the phone/network)
    try:
        from windstock.config import settings as _cfg
        admin_port = _cfg.get("server", "world_manager_port", env="ADMIN_PORT",
                              cast=int)
    except Exception:
        admin_port = int(os.environ.get("ADMIN_PORT", "8080"))
    try:
        from windstock.web import admin
        admin.start(port=admin_port)
        admin_url = f"http://127.0.0.1:{admin_port}"
    except Exception as e:
        admin_url = f"(failed to start: {e})"

    show_banner()
    rule("=")
    print("  " + paint("WINDSTOCK", BOLD, BBLUE)
          + paint("  /  Pokemon GO 0.29 private server", DIM))
    rule("-")
    print(kv("World Manager (this PC)", admin_url, BGREEN))
    print(kv("Point the phone's DNS at", redirect_ip, BCYAN))
    print(kv("Game server", f"https://{redirect_ip}:{os.environ['PORT']}", BGREEN))
    print(kv("DNS redirect", f"udp {redirect_ip}:{os.environ['DNS_PORT']}", BYELLOW))
    rule("-")
    print("  " + paint("[ready]", BOLD, BGREEN)
          + paint("  Ctrl-C to stop  --  everything below is live server activity",
                  DIM))
    print("  " + paint("[console]", BOLD, BCYAN)
          + paint("  Type /help + Enter for World Manager commands "
                  "(/log on|off toggles server-log.txt)", DIM))
    rule("=")
    print()

    # DNS in a supervised background thread (auto-restarts if it ever dies)
    def dns_loop():
        while True:
            try:
                dns_redirect.main()
            except Exception as e:
                print(paint("[dns] ", BOLD, BYELLOW)
                      + paint(f"restarting after: {e}", YELLOW), flush=True)
                time.sleep(1)
    threading.Thread(target=dns_loop, daemon=True).start()

    # Plain-HTTP door for the 0.35 client's PTC login, supervised the same way.
    # 0.35 dropped the permissive cert-validation callback 0.29 had, so its
    # Mono/C# login trusts NO certificate we can serve -- not our CA and not a
    # real Let's Encrypt one (Unity 5.3.5's root store predates ISRG Root X1).
    # The 0.35 build's SSO URLs are patched to http:// and land here; the hop is
    # inside the tailnet, so it is still encrypted end to end. 0.29 is untouched
    # and keeps using HTTPS. Only /sso is served; everything else gets a 404.
    def sso_bridge_loop():
        while True:
            try:
                sso_bridge.serve(int(os.environ.get("SSO_BRIDGE_PORT", "80")),
                                 int(os.environ["PORT"]))
            except Exception as e:
                print(paint("[bridge] ", BOLD, BMAGENTA)
                      + paint(f"restarting after: {e}", MAGENTA), flush=True)
                time.sleep(1)
    threading.Thread(target=sso_bridge_loop, daemon=True).start()

    # The World Manager, typed: /help lists the commands. Harmless when there is
    # no console to read (the windowed exe), so it can never break a GUI launch.
    start_console()

    # HTTPS server on the main thread (blocks until Ctrl-C)
    try:
        server.main()
    except KeyboardInterrupt:
        print()
        print(paint("  shutting down.", DIM))


if __name__ == "__main__":
    main()
