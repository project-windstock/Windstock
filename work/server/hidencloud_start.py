"""Start the server on a HidenCloud free (Pterodactyl) server.

The free tier gives two ports and no root, so ports 443/80/53 are out:

  * game HTTPS   -> the primary allocation (the panel passes it as SERVER_PORT)
  * login bridge -> the second allocation: put its number in PLAIN_PORT below
  * DNS          -> bound on an unexposed high port just so it stays quiet;
                    phones can't use it, so this is for the iOS launcher's
                    Remote mode only (host + port rewrite, no DNS needed)

Set the panel's startup file to this script.
"""
import os
import sys

PLAIN_PORT = ""   # <- your second allocation, e.g. "25566"

os.environ["PORT"] = os.environ.get("SERVER_PORT") or os.environ.get("PORT", "8443")
os.environ["SSO_BRIDGE_PORT"] = os.environ.get("SSO_BRIDGE_PORT") or PLAIN_PORT or "8080"
os.environ.setdefault("DNS_PORT", "15353")
os.environ.setdefault("ADMIN_PORT", "8090")
os.environ.setdefault("RUN_IP", "127.0.0.1")   # only shown in DNS answers/banner

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = sys.argv[:1]

import run

run.main()
