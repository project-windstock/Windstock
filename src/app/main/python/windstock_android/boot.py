"""Windstock Android bootstrapper.

Runs *any* Python project from a folder on the phone, natively, the same way it
runs on a computer -- with the Windstock server (https://github.com/project-
windstock/windstock) as the out-of-the-box payload, and generic support for:

  * target="script"  -- run one specific .py file (runpy.run_path)
  * target="folder"  -- run a folder's entry point (__main__.py, main.py,
                        run.py, app.py or server.py), or fall back to the
                        Windstock stack if a `windstock/` package is present
  * target="windstock" -- the classic Windstock stack (game HTTPS server on
                        port 443, PTC-SSO route, 0.35 SSO bridge on port 80,
                        World Manager on 127.0.0.1:8080 -- 0.0.0.0 in LAN mode --
                        and, in LAN mode, the stock DNS redirector with
                        REDIRECT_IP pointing at the phone's LAN IP)
  * target="auto"     -- pick based on what's in the folder

The on-device DNS trick for "phone is the server" is handled by the Kotlin
VpnService; this module only needs to expose run/stop/state and let the server's
own stdout flow into the app's log UI through a Java callback.

Generic (non-Windstock) payloads run in a daemon thread and can be interrupted
with KeyboardInterrupt via ctypes, which is what `stop()` does for them.
"""

import io
import os
import sys
import threading
import time

_state = {
    "running": False,
    "phase": "idle",
    "error": None,
    "project": "",
    "mode": "local",
    "port": 443,
    "target": "auto",
    "script": "",
    "entry": "",
    "dns_lan": False,
    "pid": None,
}

_sink = None
_sink_lock = threading.Lock()
_wrapped_stdout = False
_target_thread = None

# Entry points tried, in order, when running a generic folder project.
_ENTRYPOINTS = ("__main__.py", "main.py", "run.py", "app.py", "server.py")


# ------------------------------------------------------------------ logging --
class _Tee(io.TextIOBase):
    """stdout/stderr wrapper: forwards every line to the Android log sink."""

    def __init__(self, orig):
        super().__init__()
        self.orig = orig

    def write(self, text):
        try:
            if self.orig is not None:
                self.orig.write(text)
                self.orig.flush()
        except Exception:
            pass
        if text and _sink is not None:
            try:
                for line in text.splitlines():
                    if line:
                        with _sink_lock:
                            _sink.onLine(line)
            except Exception:
                pass
        return len(text)

    def flush(self):
        try:
            if self.orig is not None:
                self.orig.flush()
        except Exception:
            pass


def set_sink(sink):
    """Attach a Java callback (e.g. the app's LogSink) to the Python output."""
    global _sink, _wrapped_stdout
    _sink = sink
    if not _wrapped_stdout and _sink is not None:
        _wrapped_stdout = True
        sys.stdout = _Tee(getattr(sys.stdout, "orig", sys.stdout))
        sys.stderr = _Tee(getattr(sys.stderr, "orig", sys.stderr))
    if _sink is None:
        return
    try:
        _sink.onNotice("log sink attached")
    except Exception:
        pass


# ------------------------------------------------------------------- config --
def configure(project_dir, mode="local", port=443, target="auto", script=""):
    """Tell the bootstrapper where the Python project lives and how to run it.

    target: "auto" | "windstock" | "folder" | "script"
    script: absolute path to a .py file (used when target == "script").
    """
    _state["project"] = str(project_dir)
    _state["mode"] = "local" if not mode else mode
    _state["port"] = int(port or 443)
    _state["target"] = target if target in ("auto", "windstock", "folder", "script") else "auto"
    _state["script"] = str(script or "")
    _state["phase"] = "configured"
    return _state["port"]


def _log(msg):
    print(msg, flush=True)


# ---------------------------------------------- dependencies (auto pip) -----
def _install_requirements(project_dir):
    """Best-effort `pip install -r requirements.txt`. Chaquopy already bundles
    the server's only runtime dependency (s2sphere), so this is a fallback that
    keeps the 'install the requirements automatically' promise on the phone."""
    try:
        requirements = None
        for candidate in (
            os.path.join(project_dir, "deploy", "requirements.txt"),
            os.path.join(project_dir, "requirements.txt"),
        ):
            if os.path.exists(candidate):
                requirements = candidate
                break
        if requirements is None:
            return "no requirements.txt found; skipping pip install"
        try:
            import s2sphere  # noqa: F401
            return "dependencies already available (s2sphere bundled)"
        except Exception:
            pass
        site = os.path.join(os.environ.get("HOME", project_dir), "windstock_site")
        os.makedirs(site, exist_ok=True)
        if site not in sys.path:
            sys.path.insert(0, site)
        try:
            from pip._internal.cli.main import main as pip_main
            _log("[pip] installing requirements from " + requirements)
            code = pip_main([
                "install", "--disable-pip-version-check", "--no-cache-dir",
                "--target", site, "-r", requirements,
            ])
            return f"pip install finished (code {code})"
        except Exception as exc:
            return f"pip install skipped ({exc}); bundled deps will be used"
    except Exception as exc:  # never let a pip hiccup block the server
        return f"pip install skipped ({exc})"


# -------------------------------------------------------------- windstock --
def _serve():
    from windstock.net import server as srv

    if not (os.path.exists(srv.CERT) and os.path.exists(srv.KEY)):
        _log("!! TLS certs missing -- app should have extracted bundled certs "
             "to " + srv.CERT)
        return

    import ssl
    from http.server import ThreadingHTTPServer

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(srv.CERT, srv.KEY)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except (ssl.SSLError, ValueError):
        pass

    class LoggingHTTPSServer(ThreadingHTTPServer):
        def get_request(self):
            try:
                return super().get_request()
            except ssl.SSLError as exc:
                _log(f"[tls] handshake failed: {exc!r}")
                raise OSError(str(exc)) from exc

    host = os.environ.get("BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "443"))
    httpd = LoggingHTTPSServer((host, port), srv.Handler)
    httpd.socket = ctx.wrap_socket(
        httpd.socket, server_side=True, do_handshake_on_connect=True)
    _state["httpd"] = httpd
    _log(f"[game] Windstock game server listening on https://{host}:{port}")
    try:
        httpd.serve_forever(poll_interval=0.25)
    except Exception as exc:
        _log(f"[game] server stopped: {exc}")


def _admin():
    try:
        from windstock.web import admin
        port = int(os.environ.get("ADMIN_PORT", "8080"))
        host = "0.0.0.0" if _state.get("mode") == "lan" else "127.0.0.1"
        admin.start(port=port, host=host)
    except Exception as exc:
        _log(f"[admin] World Manager could not start: {exc}")


def _bridge():
    try:
        from windstock.net import sso_bridge
        sso_bridge.serve(int(os.environ.get("SSO_BRIDGE_PORT", "80")),
                         int(os.environ.get("PORT", "443")))
    except Exception as exc:
        _log(f"[bridge] 0.35 SSO bridge could not start: {exc}")


def _dns_lan():
    try:
        from windstock.net import dns_redirect
        dns_redirect.main()
        _state["dns_lan"] = True
    except Exception as exc:
        lan_ip = _lan_ip()
        _log(f"[dns] LAN DNS redirector (UDP 53) unavailable ({exc})")
        if lan_ip:
            _log(f"[dns] instead, set your clients' DNS server to {lan_ip}")
        _log("[dns] use the app's on-phone tunnel for the same device")


def _lan_ip():
    """Best-effort: the phone's outbound/LAN IP (via the default route)."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 9))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return None


def _run_windstock():
    """All non-blocking services; the game server keeps the process hot."""
    try:
        threading.Thread(target=_admin, daemon=True).start()
        threading.Thread(target=_bridge, daemon=True).start()
        mode = _state.get("mode")
        if mode == "lan":
            threading.Thread(target=_dns_lan, daemon=True).start()
        _serve()
    except Exception as exc:
        _state["error"] = str(exc)
        _log(f"[fatal] {exc}")


# ---------------------------------------------------------- generic python --
def _detect_entrypoint(project):
    for name in _ENTRYPOINTS:
        if os.path.isfile(os.path.join(project, name)):
            return name
    return None


def _resolve_target(project, requested, script):
    """Figure out which runner to use for the requested target."""
    if requested == "script":
        return "script"
    if requested == "windstock":
        return "windstock"
    if requested == "folder":
        entry = _detect_entrypoint(project)
        if entry:
            return "folder"
        if os.path.isdir(os.path.join(project, "windstock")):
            return "windstock"
        return "folder"
    # auto
    if script:
        return "script"
    if os.path.isdir(os.path.join(project, "windstock")):
        return "windstock"
    return "folder"


def _payload_env(project, entry, script):
    """Shared setup for running arbitrary Python: cwd + sys.path + argv."""
    if project not in sys.path:
        sys.path.insert(0, project)
    os.chdir(project)
    os.environ.setdefault("PORT", str(_state.get("port", 443)))
    if entry:
        _state["entry"] = entry
        sys.argv = [os.path.join(project, entry)]
    elif script:
        _state["entry"] = os.path.basename(script)
        sys.argv = [script]
    else:
        sys.argv = [project]


def _run_payload(fn):
    """Run a script/folder payload; translate exceptions into log lines."""
    global _target_thread
    _target_thread = threading.current_thread()
    try:
        fn()
    except KeyboardInterrupt:
        _log("[app] interrupted")
    except SystemExit as exc:
        _log(f"[app] exited (code {exc.code})")
    except Exception:
        import traceback
        _log("[error] " + traceback.format_exc().rstrip())
    finally:
        _target_thread = None
        _state["running"] = False
        _state["phase"] = "stopped"


def _run_script(script):
    import runpy
    runpy.run_path(script, run_name="__main__")


def _run_folder(project, entry):
    import runpy
    runpy.run_path(os.path.join(project, entry), run_name="__main__")


# -------------------------------------------------------------- lifecycle --
def prepare():
    """Run before start(): pip (best effort), then report readiness."""
    _state["phase"] = "preparing"
    project = _state.get("project", "")
    message = _install_requirements(project) if project else "no project set"
    _log("[boot] " + message)
    _log(f"[boot] running project from {project}")
    _state["phase"] = "ready"
    return message


def run():
    """Start whichever payload is configured. Returns immediately; the payload
    runs in a daemon thread. Returns "started" or an error message."""
    if _state.get("running"):
        return "already running"

    project = _state.get("project", "")
    mode = _state.get("mode", "local")
    port = _state.get("port", 443)
    requested = _state.get("target", "auto")
    script = _state.get("script", "")

    if not project or not os.path.isdir(project):
        _state["error"] = "project folder does not exist"
        return _state["error"]

    target = _resolve_target(project, requested, script)
    _state["target"] = target

    if target == "script":
        if not script or not os.path.isfile(script):
            _state["error"] = "script file does not exist: " + script
            return _state["error"]
        _payload_env(project, None, script)
        _log(f"[boot] running script {os.path.basename(script)}")
        _state["running"] = True
        _state["phase"] = "running"
        _state["error"] = None
        threading.Thread(
            target=_run_payload, args=(lambda: _run_script(script),),
            daemon=True, name="windstock-script").start()
        return "started"

    if target == "folder":
        entry = _detect_entrypoint(project)
        if entry is None:
            _state["error"] = ("no entry point found -- add one of: "
                               + ", ".join(_ENTRYPOINTS))
            return _state["error"]
        _payload_env(project, entry, None)
        _log(f"[boot] running folder project ({entry})")
        _state["running"] = True
        _state["phase"] = "running"
        _state["error"] = None
        threading.Thread(
            target=_run_payload,
            args=(lambda: _run_folder(project, entry),),
            daemon=True, name="windstock-folder").start()
        return "started"

    # windstock -- mirrors what windstock/__main__.py does for the real launcher.
    _payload_env(project, None, None)
    if mode == "lan":
        lan_ip = _lan_ip()
        if lan_ip:
            os.environ["REDIRECT_IP"] = lan_ip
            _log(f"[boot] LAN mode: game server on 0.0.0.0; clients should "
                 f"resolve PTC hosts to {lan_ip}")
        else:
            os.environ["REDIRECT_IP"] = "0.0.0.0"
            _log("[boot] LAN mode: could not detect the phone's LAN IP "
                 "(set REDIRECT_IP manually if clients fail)")
    else:
        os.environ["REDIRECT_IP"] = "127.0.0.1"
    os.environ["PORT"] = str(port)
    os.environ["BIND"] = "0.0.0.0"
    os.environ["DNS_PORT"] = os.environ.get("DNS_PORT", "53")
    os.environ["ADMIN_PORT"] = os.environ.get("ADMIN_PORT", "8080")
    os.environ["SERVE_GAME_MASTER"] = "1"

    from windstock.config import paths
    os.environ.setdefault("CERT_DIR", paths.CERT_DIR)

    _state["running"] = True
    _state["phase"] = "running"
    _state["error"] = None
    threading.Thread(target=_run_windstock, daemon=True, name="windstock-runner").start()
    _log(f"[boot] Windstock started (mode={mode}, port={port})")
    return "started"


def _interrupt():
    """Raise KeyboardInterrupt in the active payload thread (non-Windstock)."""
    global _target_thread
    t = _target_thread
    if t is None:
        return False
    try:
        import ctypes
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_long(t.ident), ctypes.py_object(KeyboardInterrupt))
        return res == 1
    except Exception:
        return False


def stop():
    """Stop whatever is running. Windstock uses httpd.shutdown(); generic
    payloads get a KeyboardInterrupt in their thread."""
    if _state.get("target") == "windstock":
        httpd = _state.get("httpd")
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            _state["httpd"] = None
    elif _state.get("running"):
        if not _interrupt():
            return "could not interrupt payload"
    _state["running"] = False
    _state["phase"] = "stopped"
    _log("[app] stopped")
    return "stopped"


def state():
    return dict(_state)