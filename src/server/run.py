"""
Entry point for the Windstock server.

    py run.py                 # local, auto-detect this PC's LAN IP
    py run.py 203.0.113.7     # remote, the IP the phone should be pointed at
    RUN_IP=203.0.113.7 py run.py

All the real work lives in the :mod:`windstock` package; this file only makes the
package importable so the server can also be launched from outside the folder.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from windstock.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
