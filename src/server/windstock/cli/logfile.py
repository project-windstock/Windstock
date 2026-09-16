"""
Teeing the server's console output to ``data/server-log.txt``.

The game client strips its own logs, so this file is the only window into what
the RPC / asset / map layer actually did. Output is APPENDED (a restart used to
wipe exactly the session you wanted to inspect) and rolled over at 5 MB.
``/log off`` in the console stops writing without losing the open file, so it can
be resumed in the same session.
"""
import os
import sys

from windstock.cli.theme import _ANSI_RE
from windstock.config import paths

# The open server-log.txt handle and whether anything is still being written to
# it. /log off leaves the file open but stops the tee writing to it, so a long
# session can be quietened without losing the file or restarting.
_LOG = {"stream": None, "on": True, "path": ""}


def _setup_logging(ui_stream=None):
    """Tee ALL console output to server-log.txt next to the exe/script, so the
    full RPC / asset / map trace can be sent for debugging (the game client strips
    its own logs, so this server log is our only window into what happened).
    `ui_stream` (the server window's activity pane) gets a copy too."""
    log_dir = paths.ensure()
    # APPEND, don't truncate. Opening "w" wiped the log on every restart, so
    # anything that happened in the previous session -- exactly the session you
    # want to look at when something "stopped working" -- was already gone by the
    # time anyone went looking. Roll over at 5 MB so it can't grow forever.
    log_path = os.path.join(log_dir, "server-log.txt")
    try:
        if os.path.getsize(log_path) > 5_000_000:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    try:
        logf = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered
    except OSError:
        logf = None
    try:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if logf:
            logf.write("\n" + "=" * 60 + "\n")
            logf.write(f"  Windstock PoGO private server  --  session started {stamp}\n")
            logf.write("=" * 60 + "\n")
    except Exception:
        pass
    _LOG.update(stream=logf, on=True, path=log_path)

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, t):
            for s in self.streams:
                if s is None:
                    continue
                if s is _LOG["stream"] and not _LOG["on"]:
                    continue                    # /log off: stop writing the log file
                try:
                    # The log file and the Windstock window aren't
                    # terminals, so strip our colour codes before they see
                    # them (server_gui parses these lines, too).
                    s.write(t if s in (sys.__stdout__, sys.__stderr__)
                            else _ANSI_RE.sub("", t))
                    s.flush()
                except Exception:
                    pass
            return len(t)

        def flush(self):
            for s in self.streams:
                if s:
                    try:
                        s.flush()
                    except Exception:
                        pass

    # sys.__stdout__ is None in the windowed exe; _Tee skips missing streams.
    sys.stdout = _Tee(sys.__stdout__, logf, ui_stream)
    sys.stderr = _Tee(sys.__stderr__, logf, ui_stream)


def set_log(on):
    """Turn server-log.txt capture on or off while the server runs (/log).

    Leaving the file object open means /log on can resume in the same session,
    and the gap is marked so a reader knows why time is missing."""
    on = bool(on)
    _LOG["on"] = on
    logf = _LOG["stream"]
    if on and logf:
        try:
            import datetime as _dt
            logf.write("\n" + "-" * 60 + "\n")
            logf.write("  logging resumed %s\n"
                       % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            logf.write("-" * 60 + "\n")
        except Exception:
            pass
    return on


def log_path():
    """Where server-log.txt lives (works before _setup_logging has run)."""
    return _LOG.get("path") or paths.path("server-log.txt")


def tail(lines=200, max_bytes=400_000):
    """The last `lines` lines of the log, read from the tail of the file so a
    multi-megabyte log is never loaded whole. [] if there is no log yet."""
    path = log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()              # drop the partial line we landed in
            data = fh.read()
    except OSError:
        return []
    out = data.decode("utf-8", "replace").splitlines()
    lines = max(0, int(lines))
    return out[-lines:] if lines and len(out) > lines else out


def clear_log(marker="  [log cleared]"):
    """Empty server-log.txt while the server keeps writing to it.

    The log handle is opened in append mode, so truncating the file cannot leave a
    sparse hole -- the next write still lands at end-of-file. Returns True on
    success.
    """
    path = log_path()
    try:
        with open(path, "w", encoding="utf-8") as fh:
            if marker:
                fh.write(str(marker) + "\n")
    except OSError:
        return False
    logf = _LOG.get("stream")
    if logf is not None and not logf.closed:
        try:
            logf.seek(0, 2)                # append-mode writes ignore this, but be tidy
        except Exception:
            pass
    return True
