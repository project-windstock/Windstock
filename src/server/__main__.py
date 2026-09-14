"""
PoGO private server — ONE launcher that runs everything in a single process:
the DNS redirector (UDP 53) and the game server (TCP 443, fake PTC SSO + RPC).

  Local (same Wi-Fi as the phone):
      py run.py                      # auto-detects this PC's LAN IP
  Remote (server with a public IP / VPS):
      py run.py 203.0.113.7          # the IP the phone should be pointed at
      RUN_IP=203.0.113.7 py run.py

Point the phone's Wi-Fi DNS at the IP this prints. Both 53/udp and 443/tcp must be
reachable from the phone (open them in the firewall; on Windows low ports are fine
without admin, but a firewall *allow* rule for inbound may be needed).

While it runs this window also takes WORLD MANAGER SLASH COMMANDS -- everything
the localhost web UI does, typed instead: /stop, /gym, /spawn, /ring, /give,
/accounts, /pw, /raid, /event, /loot, /downloads ... Type /help for the list.
/log off stops writing server-log.txt and /log on starts it again.

Build a standalone .exe (no Python needed on the target):
      py -m PyInstaller --onefile --name pogo-server \
         --add-data "certs;certs" --collect-all s2sphere run.py
"""
import contextlib
import os
import re
import socket
import sys
import threading
import time

# ---- resource base (works when frozen by PyInstaller too) -------------------
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS                         # bundled read-only data
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


# ---- ANSI colour ------------------------------------------------------------
# Escape sequences only -- no block-drawing glyphs, because the Windows console
# is cp1252 and those raise UnicodeEncodeError. Colour is just ESC[..m, which
# Windows 10+ terminals render once VT processing is switched on below.
# NO_COLOR=1 disables colour; FORCE_COLOR=1 forces it (handy when piping).
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

RESET = "\033[0m"
BOLD, DIM = "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = (
    "\033[31m", "\033[32m", "\033[33m", "\033[34m",
    "\033[35m", "\033[36m", "\033[37m")
BRED, BGREEN, BYELLOW, BBLUE, BMAGENTA, BCYAN, BWHITE = (
    "\033[91m", "\033[92m", "\033[93m", "\033[94m",
    "\033[95m", "\033[96m", "\033[97m")


def _enable_windows_ansi():
    """Switch on VT processing so Windows consoles interpret our colours."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _supports_colour():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        if not sys.__stdout__ or not sys.__stdout__.isatty():
            return False
    except Exception:
        return False
    return _enable_windows_ansi()


COLOR = _supports_colour()
if not COLOR:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = ""
    BRED = BGREEN = BYELLOW = BBLUE = BMAGENTA = BCYAN = BWHITE = ""


def paint(text, *styles):
    """Wrap text in ANSI styles (a no-op when colour is turned off)."""
    if not COLOR or not styles:
        return str(text)
    return "".join(styles) + str(text) + RESET


def fg256(n):
    """A 256-colour foreground code -- used for the banner gradient."""
    return "\033[38;5;%dm" % n if COLOR else ""


# Plain ASCII on purpose: the Windows console is cp1252, so block-drawing
# characters raise UnicodeEncodeError the moment they're printed.
BANNER = r"""
 __      __.__            .___        __                 __    
/  \    /  \__| ____    __| _/_______/  |_  ____   ____ |  | __
\   \/\/   /  |/    \  / __ |/  ___/\   __\/  _ \_/ ___\|  |/ /
 \        /|  |   |  \/ /_/ |\___ \  |  | (  <_> )  \___|    < 
  \__/\  / |__|___|  /\____ /____  > |__|  \____/ \___  >__|_ \
       \/          \/      \/    \/                   \/     \/
"""


def show_banner():
    """The Windstock wordmark, one shade per line, over a dim subtitle."""
    print()
    for line, shade in zip(BANNER.strip("\n").splitlines(), (51, 45, 39, 33, 27)):
        print(fg256(shade) + line + (RESET if COLOR else ""))
    print(paint("  a Pokemon GO 0.29 (July 2016) private server, made from scratch",
                DIM, WHITE))


def rule(char="=", width=64, colour=CYAN):
    print(paint(char * width, colour))


def kv(label, value, value_colour=BWHITE):
    """A dotted 'label .......... value' status line."""
    dots = "." * max(2, 36 - len(label))
    return ("  " + paint(label, DIM) + paint(" " + dots + " ", DIM)
            + paint(value, BOLD, value_colour))


def _route_ip():
    """The address the OS would send internet traffic from."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))              # no packets sent; reveals iface IP
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _local_ips():
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out:
                out.append(ip)
    except OSError:
        pass
    return out


def is_cgnat(ip):
    """100.64.0.0/10 -- carrier-grade NAT, which is what Tailscale hands out."""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def _usable(ip):
    return bool(ip) and not ip.startswith(("127.", "169.254."))


def detect_ip():
    """This PC's address on the phone's network.

    The obvious trick -- open a UDP socket towards the internet and read back the
    local address -- returns whichever interface holds the DEFAULT ROUTE, and with
    Tailscale running that is the 100.x tailnet address. The DNS redirector then
    answers the game with an address the phone can only reach while its own
    Tailscale is up, so the game silently fails to connect while everything else
    looks fine. Prefer a real LAN address, and only fall back to the tailnet one.
    """
    route = _route_ip()
    if _usable(route) and not is_cgnat(route):
        return route
    # Host-only adapters (VMware, VirtualBox, Hyper-V) always take the .1 of their
    # own subnet, so a .1 is the last thing to try, not the first.
    lan = [ip for ip in _local_ips() if _usable(ip) and not is_cgnat(ip)]
    lan.sort(key=lambda ip: ip.endswith(".1"))
    if lan:
        return lan[0]
    return route or "127.0.0.1"


# The open server-log.txt handle and whether anything is still being written to
# it. /log off leaves the file open but stops the tee writing to it, so a long
# session can be quietened without losing the file or restarting.
_LOG = {"stream": None, "on": True, "path": ""}


def _setup_logging(ui_stream=None):
    """Tee ALL console output to server-log.txt next to the exe/script, so the
    full RPC / asset / map trace can be sent for debugging (the game client strips
    its own logs, so this server log is our only window into what happened).
    `ui_stream` (the server window's activity pane) gets a copy too."""
    import datadir
    log_dir = datadir.ensure()
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


# ============================================================ slash console
# The World Manager, typed. Everything the localhost web UI can do is here as a
# /command (see /help): place stops, gyms and Pokemon, hand out items, run an
# event, edit the loot table, reset a password, download world data. The handlers
# call the SAME modules the admin site's HTTP endpoints call -- places / events /
# world / helpcenter / poidownload -- so the two can't drift apart.
#
# The loop runs on a daemon thread reading stdin, so it never blocks the game
# server; with no console attached (the windowed exe) it simply doesn't start.

def _out(text="", *styles):
    """A console reply -- printed normally, so it also lands in the log."""
    print(paint(text, *styles))


def _ok(msg):
    _out("[ok] " + msg, BGREEN)


def _fail(msg):
    _out("[!] " + msg, BRED)


def _dex():
    import admin
    return admin.DEX


def _giveable():
    import admin
    return admin.GIVEABLE


def _norm(s):
    return str(s).lower().replace("pok\u00e9", "poke").replace("\u00e9", "e")


def _squash(s):
    """_norm with the separators taken out, so 'UltraBall' == 'ultra ball'."""
    return _norm(s).replace(" ", "").replace("-", "").replace("_", "")


def _species(token):
    """'25', 'pikachu' or 'pika' -> a Pokedex number (0 / 'random' = random)."""
    t = str(token).strip()
    if _norm(t) in ("random", "any", "?"):
        return 0
    if t.isdigit():
        n = int(t)
        if 0 <= n <= 151:
            return n
        raise ValueError(f"species must be 0..151 (0 = random), not {n}")
    dex, low = _dex(), _squash(t)
    for i, name in enumerate(dex):
        if i and _squash(name) == low:
            return i
    hits = [i for i, name in enumerate(dex) if i and _squash(name).startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"{t!r} matches " + ", ".join(dex[i] for i in hits[:6]))
    raise ValueError(f"no Pokemon called {t!r}")


def _item(token):
    """An item the 'Give to a player' panel can hand out, by name or id."""
    t, pool = str(token).strip(), _giveable()
    if t.isdigit():
        n = int(t)
        if n in dict(pool):
            return n
        raise ValueError(f"item {n} isn't one you can give out -- see /help")
    low = _squash(t)
    for i, label in pool:
        if _squash(label) == low:
            return i
    for i, label in pool:
        if _squash(label).startswith(low):
            return i
    raise ValueError(f"no item called {t!r}")


def _loot_item(token):
    """A loot-table item (a superset of the giveable ones), by name or id."""
    import protocol
    t = str(token).strip()
    names = {int(r[0]): str(r[1]) for r in protocol.LOOT_TABLE}
    names.update(dict(_giveable()))
    if t.isdigit():
        n = int(t)
        if n > 0:
            return n, names.get(n, f"item {n}")
        raise ValueError("item ids are positive numbers")
    low = _squash(t)
    for i, label in names.items():
        if _squash(label) == low:
            return i, label
    hits = [(i, label) for i, label in names.items()
            if _squash(label).startswith(low)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(f"{t!r} matches " + ", ".join(n for _i, n in hits[:6]))
    raise ValueError(f"no item called {t!r}")


def _int(token, lo, hi, what="value"):
    try:
        n = int(str(token).strip())
    except ValueError:
        raise ValueError(f"{what} must be a whole number, not {token!r}")
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be {lo}..{hi}")
    return n


def _on_off(token):
    t = _norm(token)
    if t in ("on", "yes", "true", "1"):
        return True
    if t in ("off", "no", "false", "0"):
        return False
    raise ValueError(f"expected on or off, not {token!r}")


def _coords(args):
    """Take an optional @lat,lng out of the arguments: (lat, lng, rest)."""
    rest, pair = [], None
    for a in args:
        if a.startswith("@"):
            pair = a[1:]
        else:
            rest.append(a)
    if pair is None:
        return None, None, rest
    try:
        la, ln = (float(x) for x in pair.split(","))
    except ValueError:
        raise ValueError("coordinates look like @39.19000,-96.58000")
    return la, ln, rest


def _take_player(args):
    """Pull an optional 'as <trainer>' out of the arguments: (who, rest)."""
    rest, who = list(args), ""
    for i, tok in enumerate(rest):
        if _norm(tok) in ("as", "for") and i + 1 < len(rest):
            who = rest[i + 1]
            del rest[i:i + 2]
            break
    return who, rest


@contextlib.contextmanager
def _acting(who):
    """Run as `who`; blank means the trainer who played most recently."""
    import rpc
    import world
    name = (who or "").strip() or (rpc._last_user[0] or "")
    if not name:
        raise ValueError("nobody is playing right now -- name a trainer with "
                         "'as <name>'")
    # acting_as is a context manager, so an unknown name only raises on ENTER --
    # wrap that, not the call, or the KeyError escapes as a raw traceback line.
    ctx = world.acting_as(name)
    try:
        ctx.__enter__()
    except KeyError:
        raise ValueError(f"no account called {name!r}")
    try:
        yield name
    finally:
        ctx.__exit__(None, None, None)


def _last_loc():
    import rpc
    return rpc._last_loc[0], rpc._last_loc[1]


def _here(la, ln):
    """Fill in the trainer's location when no @lat,lng was given."""
    if la is not None:
        return la, ln
    la, ln = _last_loc()
    if not la and not ln:
        _fail("no trainer location yet -- open the game once, or pass @lat,lng "
              "(e.g. @39.19000,-96.58000)")
        return None, None
    return la, ln


# ---- world: what is running ------------------------------------------------
def cmd_status(args):
    import events as EV
    import places as PL
    import rpc
    import settings as CFG
    import world
    state, cfg, raid, dex = PL.get(), EV.get(), world.raid(), _dex()
    gyms = sum(1 for f in state["forts"] if f.get("kind") == "gym")
    pid = int(raid.get("pokemon_id") or 0)
    boss = dex[pid] if 0 < pid < len(dex) else "?"
    last = rpc._last_user[0]
    la, ln = _last_loc()
    _out("  world", BOLD, BCYAN)
    _out(f"    placed .......... {len(state['forts']) - gyms} stops / {gyms} gyms"
         f" / {len(state['spawns'])} spawn points")
    _out(f"    random .......... stops+gyms "
         f"{'ON' if state['procedural_forts'] else 'OFF'}, wild Pokemon "
         f"{'ON' if state['procedural_spawns'] else 'OFF'}")
    _out(f"    event ........... {cfg['event_name']} / density "
         f"{cfg['spawn_density']} / CP {cfg['min_cp']}-{cfg['max_cp']} / "
         f"shiny {cfg['shiny_rate']}")
    _out("    raid ............ " + (f"ON -- {boss} CP{raid['cp']} in every gym"
                                     if raid["on"] else "off"))
    _out(f"    loot ............ {CFG.get('pokestops', 'min_items_per_spin')}-"
         f"{CFG.get('pokestops', 'max_items_per_spin')} items per spin")
    if last:
        _out(f"    trainer ......... {last} at {la:.5f}, {ln:.5f}")
    else:
        _out("    trainer ......... nobody has played yet", DIM)
    _out(f"    server log ...... {'ON' if _LOG['on'] else 'OFF'}"
         f"   ({_LOG['path']})")


def cmd_list(args):
    import places as PL
    dex, state = _dex(), PL.get()
    rows = []
    for f in state["forts"]:
        rows.append(("GYM" if f.get("kind") == "gym" else "STOP",
                     f.get("name") or "", f["lat"], f["lng"], f.get("id", "")))
    for s in state["spawns"]:
        pid = int(s.get("pokemon_id") or 0)
        rows.append(("MON", dex[pid] if 0 < pid < len(dex) else "Random",
                     s["lat"], s["lng"], s.get("id", "")))
    if not rows:
        return _out("  nothing placed yet -- /stop, /gym or /spawn adds something",
                    DIM)
    _out(f"  {len(rows)} placed object(s)", BOLD, BCYAN)
    for tag, name, la, ln, oid in rows:
        _out(f"    {tag:<4} {name[:22]:<22} {la:.5f}, {ln:.5f}   {oid}")


# ---- world: place things ---------------------------------------------------
def _place_fort(args, kind):
    import places as PL
    la, ln, rest = _coords(args)
    la, ln = _here(la, ln)
    if la is None:
        return
    fort = PL.add_fort(la, ln, kind, " ".join(rest))
    _ok(f"placed {fort['kind']} {fort['name']!r} at {la:.5f}, {ln:.5f}"
        f"   id={fort['id']}")


def cmd_stop(args):
    _place_fort(args, "stop")


def cmd_gym(args):
    _place_fort(args, "gym")


def cmd_spawn(args):
    import places as PL
    la, ln, rest = _coords(args)
    pid = _species(rest[0]) if rest else 0
    la, ln = _here(la, ln)
    if la is None:
        return
    s = PL.add_spawn(la, ln, pid, " ".join(rest[1:]))
    who = _dex()[pid] if pid else "random Pokemon"
    _ok(f"added a {who} spawn point at {la:.5f}, {ln:.5f}   id={s['id']}")


def cmd_remove(args):
    import places as PL
    if not args:
        raise ValueError("usage: /remove <id>   (/list shows the ids)")
    if PL.remove(args[0]):
        _ok(f"removed {args[0]}")
    else:
        _fail(f"nothing here with id {args[0]!r}")


def cmd_clear(args):
    import places as PL
    what = args[0].lower() if args else "all"
    if what not in ("all", "forts", "spawns"):
        raise ValueError("usage: /clear [forts|spawns|all]")
    state = PL.clear(what)
    _ok(f"cleared {what} -- {len(state['forts'])} forts and "
        f"{len(state['spawns'])} spawns left")


def cmd_ring(args):
    import math
    import places as PL
    la, ln, rest = _coords(args)
    la, ln = _here(la, ln)
    if la is None:
        return
    nums, gym = [], True
    for tok in rest:
        if _norm(tok) in ("nogym", "no-gym"):
            gym = False
            continue
        try:
            nums.append(float(tok))
        except ValueError:
            raise ValueError("usage: /ring [count] [radius_m] [@lat,lng] [nogym]")
    count = max(1, min(24, int(nums[0]))) if nums else 8
    radius = max(10.0, min(500.0, nums[1])) if len(nums) > 1 else 60.0
    for i in range(count):
        a = 2 * math.pi * i / count
        dlat = (radius * math.cos(a)) / 111320.0
        dlng = (radius * math.sin(a)) / (111320.0 * max(0.2,
                                                         math.cos(math.radians(la))))
        PL.add_fort(la + dlat, ln + dlng, "stop", f"Ring Stop {i + 1}")
    if gym:
        PL.add_fort(la, ln, "gym", "Home Gym")
    _ok(f"built {count} stops" + (" and a gym" if gym else "")
        + f" around {la:.5f}, {ln:.5f}")


def cmd_makestop(args):
    """The in-game 'spawn a PokeStop here' -- first one free, then 1000 coins."""
    import math
    import places as PL
    import world
    la, ln, rest = _coords(args)
    la, ln = _here(la, ln)
    if la is None:
        return
    who, rest = _take_player(rest)
    name = " ".join(rest)
    with _acting(who) as target:
        ok, charged, reason = world.buy_stop(1000)
        if not ok:
            return _fail(reason)
        if not name:
            # Name it after the closest real OSM place within 250 m, as the UI does.
            try:
                import pois
                best, bestd = "", 250.0
                for f in pois.forts():
                    dy = (f["lat"] - la) * 111320.0
                    dx = (f["lng"] - ln) * 111320.0 * max(0.2,
                                                          math.cos(math.radians(la)))
                    dm = math.hypot(dx, dy)
                    if dm < bestd:
                        best, bestd = f["name"], dm
                name = best or "Windstock Stop"
            except Exception:
                name = "Windstock Stop"
        fort = PL.add_fort(la, ln, "stop", name[:40])
    price = "free" if charged == 0 else f"{charged}c"
    _ok(f"placed {fort['name']!r} at your location ({price}) for {target}"
        f"   id={fort['id']}")


# ---- players ---------------------------------------------------------------
def cmd_give(args):
    import world
    if not args:
        raise ValueError("usage: /give item|candy|dust <thing> <count> "
                         "[as <trainer>]")
    kind = _norm(args[0])
    rest = args[1:]
    if kind in ("item", "items"):
        who, rest = _take_player(rest)
        if len(rest) < 2:
            raise ValueError("usage: /give item <item> <count> [as <trainer>]")
        iid = _item(rest[0])
        count = _int(rest[1], 1, 999, "count")
        label = dict(_giveable())[iid]
        with _acting(who) as target:
            total = world.add_item(iid, count)
        _ok(f"gave {target} {count} x {label} (now {total})")
    elif kind in ("candy", "candies"):
        who, rest = _take_player(rest)
        if len(rest) < 2:
            raise ValueError("usage: /give candy <species> <count> [as <trainer>]")
        import protocol
        pid, count, dex = _species(rest[0]), _int(rest[1], 1, 999, "count"), _dex()
        fam = protocol.pokemon_family(pid)
        with _acting(who) as target:
            total = world.add_candy(fam, count)
        label = dex[fam] if fam < len(dex) else str(fam)
        extra = f" ({dex[pid]}'s family)" if fam != pid else ""
        _ok(f"gave {target} {count} {label} candy (now {total}){extra}")
    elif kind in ("dust", "stardust"):
        who, rest = _take_player(rest)
        if not rest:
            raise ValueError("usage: /give dust <count> [as <trainer>]")
        count = _int(rest[0], 1, 999999, "count")
        with _acting(who) as target:
            total = world.add_stardust(count)
        _ok(f"gave {target} {count} stardust (now {total})")
    else:
        raise ValueError(f"you can give item, candy or dust, not {args[0]!r}")


def cmd_accounts(args):
    import world
    rows = world.accounts()
    if not rows:
        return _out("  no accounts yet", DIM)
    _out(f"  {len(rows)} account(s)", BOLD, BCYAN)
    for a in rows:
        _out(f"    {a['username'][:20]:<20} lvl {a['level']:>2}  "
             f"{a['caught']:>4} caught  {a['coins']:>6} coins  {a['xp']:>10} xp")


def cmd_pw(args):
    import world
    if len(args) < 2:
        raise ValueError("usage: /pw <trainer> <new password>")
    who, pw = args[0], " ".join(args[1:])
    if world.set_password(who, pw):
        _ok(f"{who}'s password has been reset")
    else:
        _fail(f"no account called {who!r}")


# ---- gyms, spawn switches, events -----------------------------------------
def cmd_raid(args):
    import world
    dex = _dex()
    if not args:
        cfg = world.raid()
        pid = int(cfg.get("pokemon_id") or 0)
        who = dex[pid] if 0 < pid < len(dex) else "?"
        return _out("  raid is " + (f"ON -- {who} CP{cfg['cp']} in every gym as "
                                    f"{cfg['trainer']!r}" if cfg["on"] else "off"))
    first = _norm(args[0])
    if first in ("off", "no", "false", "0"):
        world.set_raid(on=False)
        return _ok("raid off -- gyms are back to normal and empty")
    if first not in ("on", "yes", "true", "1"):
        raise ValueError("usage: /raid on [species] [cp] [trainer] | /raid off")
    rest = args[1:]
    pid = _species(rest[0]) if rest else None
    cp = _int(rest[1], 10, 9999, "CP") if len(rest) > 1 else None
    trainer = " ".join(rest[2:])[:16] if len(rest) > 2 else None
    cfg, sent = world.set_raid(True, pid, cp, trainer)
    who = dex[cfg["pokemon_id"]] if cfg["pokemon_id"] < len(dex) else "?"
    _ok(f"raid ON -- {who} CP{cfg['cp']} is now defending every gym as "
        f"{cfg['trainer']!r}" + (f"; {sent} defender(s) sent home" if sent else ""))


def cmd_procedural(args):
    import places as PL
    if len(args) < 2:
        raise ValueError("usage: /procedural <forts|spawns|both> <on|off>")
    what = _norm(args[0])
    if what not in ("forts", "spawns", "both"):
        raise ValueError("the first word must be forts, spawns or both")
    on = _on_off(args[1])
    PL.set_procedural(on, what)
    state = PL.get()
    _ok(f"random {what}: {'ON' if on else 'OFF'}   (stops+gyms "
        f"{'ON' if state['procedural_forts'] else 'OFF'}, wild Pokemon "
        f"{'ON' if state['procedural_spawns'] else 'OFF'})")


def _event_summary(cfg):
    dex = _dex()
    mode = cfg["species_mode"]
    if mode == "single":
        pid = int(cfg["single_species"])
        sp = "only " + (dex[pid] if pid < len(dex) else str(pid))
    elif mode == "list":
        sp = "list " + ",".join(str(x) for x in cfg["species_list"])
    else:
        sp = "all 151"
    return (f"{cfg['event_name']!r} -- density {cfg['spawn_density']}, "
            f"CP {cfg['min_cp']}-{cfg['max_cp']}, species {sp}, "
            f"shiny {cfg['shiny_rate']}")


def cmd_presets(args):
    import events as EV
    _out("  one-click events", BOLD, BCYAN)
    for name, cfg in EV.PRESETS.items():
        _out(f"    {name:<18} density {cfg.get('spawn_density'):>2}   "
             f"CP {cfg.get('min_cp')}-{cfg.get('max_cp')}")


def cmd_preset(args):
    import events as EV
    if not args:
        raise ValueError("usage: /preset <name>   (/presets lists them)")
    want = _norm(" ".join(args))
    name = next((n for n in EV.PRESETS if _norm(n) == want), None)
    if name is None:
        hits = [n for n in EV.PRESETS if _norm(n).startswith(want)]
        if len(hits) != 1:
            raise ValueError(f"no preset called {' '.join(args)!r} -- try /presets")
        name = hits[0]
    _ok("event is now " + _event_summary(EV.apply_preset(name)))


def cmd_event(args):
    import events as EV
    if not args:
        return _out("  " + _event_summary(EV.get()))
    key, rest = _norm(args[0]), args[1:]
    try:
        if key in ("name", "title"):
            if not rest:
                raise ValueError("give the event a name")
            cfg = EV.save({"event_name": " ".join(rest)})
        elif key in ("density", "spawn_density"):
            cfg = EV.save({"spawn_density": _int(rest[0], 0, 60, "density")})
        elif key == "cp":
            cfg = EV.save({"min_cp": _int(rest[0], 10, 5000, "min CP"),
                           "max_cp": _int(rest[1], 10, 5000, "max CP")})
        elif key == "shiny":
            cfg = EV.save({"shiny_rate": max(0.0, min(1.0, float(rest[0])))})
        elif key in ("species", "species_mode", "pokemon"):
            mode = _norm(rest[0])
            if mode in ("all", "everything"):
                cfg = EV.save({"species_mode": "all"})
            elif mode in ("single", "one"):
                cfg = EV.save({"species_mode": "single",
                               "single_species": _species(rest[1])})
            elif mode == "list":
                ids = [_species(x) for x in ",".join(rest[1:]).split(",") if x.strip()]
                if not ids:
                    raise ValueError("list which species, e.g. "
                                     "/event species list 1,4,7")
                cfg = EV.save({"species_mode": "list", "species_list": ids})
            else:
                raise ValueError("species mode is all, list or single")
        else:
            raise ValueError("event settings are name, density, cp, species, shiny")
    except IndexError:
        raise ValueError(f"/event {key} needs a value -- see /help")
    _ok("event updated -- " + _event_summary(cfg))


# ---- PokeStops, nominations, world data -----------------------------------
def cmd_loot(args):
    import protocol
    import settings as CFG
    rows = [[int(r[0]), str(r[1]), int(r[2]), int(r[3]), int(r[4])]
            for r in protocol.loot_table()]
    lo = CFG.get("pokestops", "min_items_per_spin")
    hi = CFG.get("pokestops", "max_items_per_spin")
    if not args or _norm(args[0]) in ("list", "show"):
        _out(f"  {len(rows)} loot rows, {lo}-{hi} items per spin", BOLD, BCYAN)
        for iid, name, chance, mn, mx in rows:
            _out(f"    {iid:<4} {name[:22]:<22} chance {chance:>3}%   {mn}-{mx}")
        _out("    /loot set <item> <chance %> [min max]    /loot off <item>    "
             "/loot spin <min> <max>", DIM)
        return
    verb = _norm(args[0])
    if verb in ("spin", "per"):
        nlo = _int(args[1], 0, 10, "min")
        nhi = _int(args[2], 1, 10, "max")
        CFG.set("pokestops", "min_items_per_spin", nlo)
        CFG.set("pokestops", "max_items_per_spin", max(nlo, nhi))
        return _ok(f"every spin now hands out {nlo}-{max(nlo, nhi)} items")
    if verb in ("set", "edit", "add") or verb in ("off", "remove", "rm"):
        iid, _label = _loot_item(args[1])
        if verb in ("off", "remove", "rm"):
            chance, mn, mx = 0, None, None
        else:
            if len(args) < 3:
                raise ValueError("usage: /loot set <item> <chance %> [min max]")
            chance = _int(args[2], 0, 100, "chance")
            mn = _int(args[3], 1, 99, "min") if len(args) > 3 else None
            mx = _int(args[4], 1, 99, "max") if len(args) > 4 else None
        row = next((r for r in rows if r[0] == iid), None)
        if row is None:
            rows.append([iid, _label, chance, mn or 1, mx or 1])
        else:
            row[2] = chance
            if mn is not None:
                row[3] = mn
            if mx is not None:
                row[4] = mx
        row = next(r for r in rows if r[0] == iid)
        row[3] = max(1, row[3])
        row[4] = max(row[3], row[4])
        CFG.set("pokestops", "loot_table", rows)
        return _ok(f"{row[1]}: chance {row[2]}%"
                   + (" (never drops)" if row[2] == 0 else f", {row[3]}-{row[4]} per hit"))
    raise ValueError("usage: /loot [list | set <item> <chance %> [min max] | "
                     "off <item> | spin <min> <max>]")


def cmd_noms(args):
    import helpcenter as HC
    import places as PL
    if args and _norm(args[0]) in ("remove", "rm", "delete", "reject"):
        if len(args) < 2:
            raise ValueError("usage: /noms remove <id>")
        row = HC.resolve(args[1], "rejected")
        if not row:
            return _fail(f"no nomination with id {args[1]!r}")
        # Nominations are placed the moment they arrive, so resolving one only
        # ever means taking that fort back out again (as the web UI does).
        for f in list(PL.get()["forts"]):
            if (abs(f["lat"] - row["lat"]) < 1e-6
                    and abs(f["lng"] - row["lng"]) < 1e-6):
                PL.remove(f["id"])
        return _ok(f"removed {row['name']!r}")
    rows = HC.recent(25)
    if not rows:
        return _out("  no nominations waiting", DIM)
    _out(f"  {len(rows)} nomination(s)", BOLD, BCYAN)
    for n in rows:
        _out(f"    {n.get('id','')}  {n.get('kind','stop'):<4} {n.get('name','')[:22]:<22}"
             f" {n['lat']:.5f},{n['lng']:.5f}  by {n.get('player','?')}")
    _out("    /noms remove <id> takes one back out again", DIM)


def _region(text):
    """'Kansas' / 'germany' -> (label, Geofabrik region)."""
    import poidownload as DL
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("which country or US state? e.g. /downloads add Kansas")
    table = list(DL.US_STATES.items()) + list(DL.COUNTRIES.items())
    hits = [(n, r) for n, r in table if n.lower() == t]
    if not hits:
        hits = [(n, r) for n, r in table if n.lower().startswith(t)]
    if not hits:
        raise ValueError(f"no region called {text!r} -- countries and US states "
                         "only (the World Data page lists them)")
    if len(hits) > 1:
        raise ValueError(f"{text!r} matches " + ", ".join(n for n, _r in hits[:8])
                         + " -- be more specific")
    name, region = hits[0]
    if region == "north-america/us":
        raise ValueError("the whole US is enormous -- pick a state, e.g. "
                         "/downloads add California")
    return name, region


def cmd_downloads(args):
    import poidownload as DL
    status = DL.status()
    if not args:
        _out(f"  {status['forts']:,} real stops/gyms in osm_forts.json", BOLD, BCYAN)
        if status["done"]:
            _out("    added: " + ", ".join(status["done"]))
        for region, what in status["busy"].items():
            _out(f"    working on {region}: {what}", BYELLOW)
        for region, err in status["errors"].items():
            _out(f"    {region} FAILED: {err}", BRED)
        _out("    /downloads add <country|state>   /downloads remove <country|state>",
             DIM)
        return
    verb = _norm(args[0])
    if verb in ("add", "get", "download", "update"):
        name, region = _region(" ".join(args[1:]))
        if DL.start(name, region):
            _ok(f"downloading {name} ({region}) -- /downloads shows progress")
        else:
            _fail(f"{name} is already downloading")
    elif verb in ("remove", "rm", "delete", "del"):
        name, region = _region(" ".join(args[1:]))
        if DL.remove(name, region):
            _ok(f"removing {name}'s stops and gyms from the world")
        else:
            _fail(f"{name} is busy -- try again in a moment")
    else:
        raise ValueError("usage: /downloads [add|remove <country|state>]")


# ---- the log and the console itself ----------------------------------------
def cmd_log(args):
    if not args:
        return _out(f"  server-log.txt is {'ON' if _LOG['on'] else 'OFF'}"
                    f"   ({_LOG['path']})", BOLD,
                    BGREEN if _LOG["on"] else BYELLOW)
    if _on_off(args[0]):
        set_log(True)
        _ok(f"server-log.txt is ON -- writing to {_LOG['path']}")
    else:
        set_log(False)
        _out("[log] server-log.txt is OFF -- nothing more is written to it until "
             "/log on", BOLD, BYELLOW)


def cmd_quit(args):
    _out("  stopping the server.", DIM)
    os._exit(0)


def cmd_help(args):
    _out("  World Manager commands -- everything the web UI does, typed instead",
         BOLD, BCYAN)
    for _names, usage, blurb, _fn in _COMMANDS:
        _out(f"    {usage:<58} {blurb}")
    _out("    @lat,lng picks a spot; without it the trainer's last position is "
         "used", DIM)
    _out("    'as <trainer>' names who to act on; without it, whoever is playing",
         DIM)
    _out("    quote a name with spaces: /give item \"great ball\" 5", DIM)


_COMMANDS = [
    (("help", "?"), "/help", "this list", cmd_help),
    (("status",), "/status", "counts, event, raid, logging", cmd_status),
    (("list", "ls"), "/list", "everything placed, with its id", cmd_list),
    (("stop",), "/stop [name] [@lat,lng]", "place a PokeStop", cmd_stop),
    (("gym",), "/gym [name] [@lat,lng]", "place a Gym", cmd_gym),
    (("spawn",), "/spawn <species|#> [name] [@lat,lng]",
     "add a wild Pokemon spawn", cmd_spawn),
    (("remove", "rm", "del"), "/remove <id>", "delete one placed object",
     cmd_remove),
    (("clear",), "/clear [forts|spawns|all]", "wipe the placed world", cmd_clear),
    (("ring",), "/ring [count] [radius_m] [@lat,lng] [nogym]",
     "a ring of stops around you", cmd_ring),
    (("makestop",), "/makestop [name] [as <trainer>] [@lat,lng]",
     "the in-game 'stop here' (first free)", cmd_makestop),
    (("give",), "/give item|candy|dust <thing> <count> [as <trainer>]",
     "hand out items, candy or stardust", cmd_give),
    (("accounts",), "/accounts", "every trainer and their level", cmd_accounts),
    (("pw", "password"), "/pw <trainer> <new password>", "reset a password",
     cmd_pw),
    (("raid",), "/raid on [species] [cp] [trainer] | /raid off",
     "a raid boss in every gym", cmd_raid),
    (("procedural",), "/procedural <forts|spawns|both> <on|off>",
     "random stops/gyms and wild spawns", cmd_procedural),
    (("preset",), "/preset <name>", "apply a one-click event", cmd_preset),
    (("presets",), "/presets", "list the one-click events", cmd_presets),
    (("event",), "/event [name|density|cp|species|shiny <value>]",
     "the live spawn event", cmd_event),
    (("loot",), "/loot list | set <item> <pct> | off <item> | spin <a> <b>",
     "what a PokeStop spin hands out", cmd_loot),
    (("noms", "nominations"), "/noms [remove <id>]",
     "player-nominated stops and gyms", cmd_noms),
    (("downloads", "download"), "/downloads [add|remove <country|state>]",
     "real stops/gyms from OpenStreetMap", cmd_downloads),
    (("log",), "/log on|off", "write server-log.txt, or stop writing it", cmd_log),
    (("quit", "exit"), "/quit", "stop the server", cmd_quit),
]

_DISPATCH = {}
for _names, _usage, _blurb, _fn in _COMMANDS:
    for _n in _names:
        _DISPATCH[_n] = (_usage, _fn)


def _dispatch(line):
    import shlex
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    if not parts:
        return
    entry = _DISPATCH.get(parts[0].lstrip("/").lower())
    if not entry:
        return _fail(f"no command called {parts[0]!r} -- /help lists them")
    _usage, fn = entry
    try:
        fn(parts[1:])
    except ValueError as e:
        _fail(str(e))
    except SystemExit:
        raise
    except Exception as e:
        _fail(f"{type(e).__name__}: {e}")


def _console_loop():
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return                      # no more input, or Ctrl-C: just stop reading
        except Exception:
            return
        line = line.strip()
        if not line:
            continue
        try:
            _dispatch(line)
        except SystemExit:
            raise
        except Exception as e:          # a command bug must never kill the server
            _fail(f"{type(e).__name__}: {e}")


def start_console():
    """Run the slash-command console on a daemon thread. No-op with no stdin
    (the windowed exe), so a GUI launch is unaffected."""
    try:
        if sys.stdin is None or sys.stdin.closed:
            return None
    except Exception:
        return None
    t = threading.Thread(target=_console_loop, name="console", daemon=True)
    t.start()
    return t


def main(ui_stream=None):
    """Run everything. Blocks in server.main(). The server window
    (server_gui.py) calls this on a background thread with its own stream."""
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
    # hand config to the imported modules via env (read at their import time)
    os.environ["REDIRECT_IP"] = redirect_ip
    os.environ.setdefault("PORT", "443")
    os.environ.setdefault("BIND", "0.0.0.0")
    os.environ.setdefault("DNS_PORT", "53")
    os.environ.setdefault("CERT_DIR", os.path.join(BASE, "certs"))
    # Serve the real 2016 game master so the client has Pokemon templates and will
    # actually request + render the wild Pokemon (asset bundles now shipped in
    # assets/). Set SERVE_GAME_MASTER=0 to fall back to the bare template set.
    os.environ.setdefault("SERVE_GAME_MASTER", "1")

    import server
    import dns_redirect
    import sso_bridge

    if not (os.path.exists(server.CERT) and os.path.exists(server.KEY)):
        print(paint("!! ", BOLD, BRED)
              + paint(f"TLS certs not found in {os.environ['CERT_DIR']}.", BRED))
        print(paint("   Run gen_certs.py once, or ship the certs/ folder "
                    "alongside this.", DIM))
        sys.exit(1)

    # World Manager (localhost only -- never exposed to the phone/network)
    try:
        import settings as _cfg
        admin_port = _cfg.get("server", "world_manager_port", env="ADMIN_PORT", cast=int)
    except Exception:
        admin_port = int(os.environ.get("ADMIN_PORT", "8080"))
    try:
        import admin
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
