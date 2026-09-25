"""Run the server with ``python .`` / ``python -m`` from the server folder.

Equivalent to ``py run.py``; the implementation lives in the package.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from windstock.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
