"""
Which address the phone should be pointed at.

The launcher has to decide which of this machine's interfaces the phone can
actually reach, and getting it wrong is the classic "everything looks fine but
the game never connects" failure, so the logic is kept isolated and documented.
"""
import socket


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
