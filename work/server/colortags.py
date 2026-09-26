"""
Colored names: short tags typed on the phone -> Unity rich text for the client.

The 2016 client draws names with Unity's uGUI Text, which understands
<color=#rrggbb>...</color>. Typing that on a phone keyboard is miserable, so
names may carry short tags instead:

    [red]Ash               whole name red
    [gold]Ash[/]ketchum    "Ash" gold, the rest plain
    [f80]Ash / [ff8800]Ash any hex colour, 3 or 6 digits
    [rainbow]Ash           one colour per letter

Names are STORED with the short tags (so the client's length limits count only
what you see, via visible()) and expanded by expand() each time one is sent. An
unknown [tag] is left alone, so a plain name with brackets in it is unharmed.
"""
import re

COLORS = {
    "red": "ff4040", "orange": "ff9a1f", "yellow": "ffe14d", "gold": "f2b705",
    "green": "3ccf5a", "lime": "a6f03c", "teal": "1fbfa6", "cyan": "3de0ff",
    "blue": "3d8bff", "navy": "2340a0", "purple": "a55cff", "pink": "ff6fcf",
    "magenta": "ff3dd6", "brown": "a0632d", "white": "ffffff", "gray": "9aa4ad",
    "grey": "9aa4ad", "black": "111111",
    # the three teams
    "mystic": "3d8bff", "valor": "ff3d3d", "instinct": "ffd000",
}
RAINBOW = ["ff4040", "ff9a1f", "ffe14d", "3ccf5a", "3d8bff", "a55cff"]
TEAM_COLORS = {1: COLORS["mystic"], 2: COLORS["valor"], 3: COLORS["instinct"]}

_TAG = re.compile(r"\[(/|[a-z]+|[0-9a-f]{3}|[0-9a-f]{6})\]", re.I)


def _hex(tag):
    """'red' / 'f80' / 'ff8800' -> 'rrggbb', or None if it isn't a colour."""
    t = tag.lower()
    if t in COLORS:
        return COLORS[t]
    if re.fullmatch(r"[0-9a-f]{6}", t):
        return t
    if re.fullmatch(r"[0-9a-f]{3}", t):
        return "".join(c * 2 for c in t)
    return None


def _runs(s):
    """[(colour or None or 'rainbow', text)] -- the name split at its tags."""
    out, cur, pos = [], None, 0
    for m in _TAG.finditer(s or ""):
        tag = m.group(1).lower()
        new = None if tag == "/" else ("rainbow" if tag == "rainbow" else _hex(tag))
        if new is None and tag != "/":
            continue                          # not a colour: keep it as text
        out.append((cur, s[pos:m.start()]))
        cur, pos = new, m.end()
    out.append((cur, (s or "")[pos:]))
    return [(c, t) for c, t in out if t]


def has_tags(s):
    return any(c for c, _ in _runs(s))


def visible(s):
    """The name as the player sees it, tags removed."""
    return "".join(t for _, t in _runs(s))


def clip(s, n):
    """Keep at most n VISIBLE characters, tags intact."""
    out, left = [], n
    for m in re.finditer(r"\[[^\]]*\]|.", s or "", re.S):
        tok = m.group(0)
        if len(tok) > 1 and (tok[1:-1] == "/" or tok[1:-1].lower() == "rainbow"
                             or _hex(tok[1:-1])):
            out.append(tok)
            continue
        if len(tok) > 1:                      # an unknown [tag] is plain text
            if left < len(tok):
                out.append(tok[:left])
                break
            left -= len(tok)
            out.append(tok)
            continue
        if left <= 0:
            break
        out.append(tok)
        left -= 1
    return "".join(out)


def _paint(color, text):
    if not color:
        return text
    if color == "rainbow":
        return "".join(ch if ch.isspace() else
                       f"<color=#{RAINBOW[i % len(RAINBOW)]}>{ch}</color>"
                       for i, ch in enumerate(text))
    return f"<color=#{color}>{text}</color>"


def expand(s, default=None):
    """Short tags -> Unity rich text. default: a colour name/hex (or 'rainbow')
    for a name that carries no tags of its own."""
    if not s:
        return s or ""
    runs = _runs(s)
    if default and not any(c for c, _ in runs):
        d = "rainbow" if str(default).lower() == "rainbow" else _hex(str(default).lstrip("#"))
        runs = [(d, visible(s))]
    return "".join(_paint(c, t) for c, t in runs)


def _setting(key):
    try:
        import settings as _cfg
        return str(_cfg.get("names", key) or "").strip()
    except Exception:
        return ""


def trainer(name):
    """A trainer name as sent to the client (names.trainer_color when untagged)."""
    return expand(name, _setting("trainer_color"))


def fort(name, gym=False, team=0):
    """A PokeStop / Gym name as sent to the client. gym_color 'team' paints a gym
    in the colour of the team holding it (white when neutral: left plain)."""
    want = _setting("gym_color" if gym else "stop_color")
    if gym and want.lower() == "team":
        want = TEAM_COLORS.get(int(team or 0), "")
    return expand(name, want)
