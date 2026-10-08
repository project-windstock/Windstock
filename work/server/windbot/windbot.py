"""
Windbot: the Windstock Discord bot.

Runs next to the game server (systemd unit `windbot`, see windbot.service).

    - its status line in the member list ("3 trainers online" / "Server offline")
    - /status       is the server up, how many are playing
    - /leaderboard  the top trainers, this week or all time
    - /profile      one trainer's card
    - /events       what is on now and what is coming
    - /map          where you can play, with a link to the website's play map
    - /announcements  (server managers) post here when an event starts
    - /link, /unlink  tie this Discord account to a Windstock trainer
    - /trade        offer another trainer a Pokemon swap (Accept / Decline)
    - /bounty       send in a TikTok video for the video bounty (in-game rewards)
    - /bountyreview (server managers) review new bounty videos in this channel

Every card carries a Refresh button and keeps itself up to date: the bot edits
the status card whenever the status or the count changes, and the others every
five minutes.

The game server is asked over loopback (/windstock/players and
/windstock/events, see online.py) and so is the website (/api/leaderboard,
/api/trainer): the bot only learns what those already show the public.

    DISCORD_TOKEN   the bot's token (required; /etc/windbot.env on the VPS)
    GAME_URL        the game server on this machine  (http://127.0.0.1:8888)
    SITE_API        the website on this machine      (http://127.0.0.1:8787)
    SITE_URL        where the Website button goes    (no button when unset)
    SITE_GAME_KEY   the website's GAME_KEY: lets the bot ask which trainer a
                    Discord user is (no /link or /trade without it)
    TRADE_TEST_LOGIN  a game save the bot itself trades as: /trade @WindBot
                    and it accepts on its own (server managers only; unset = off)
    POLL_SECONDS    how often to look at the server  (30)
    REFRESH_SECONDS how often the other cards update (300)
    STATE_DIRECTORY where the bot keeps its state    (set by systemd)
"""
import asyncio
import json
import os
import sys
import time
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
GAME_URL = os.environ.get("GAME_URL", "http://127.0.0.1:8888").rstrip("/")
SITE_API = os.environ.get("SITE_API", "http://127.0.0.1:8787").rstrip("/")
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
SITE_GAME_KEY = os.environ.get("SITE_GAME_KEY", "").strip()
TRADE_TEST_LOGIN = os.environ.get("TRADE_TEST_LOGIN", "").strip()
POLL_SECONDS = max(15, int(os.environ.get("POLL_SECONDS", "30") or 30))
REFRESH_SECONDS = max(60, int(os.environ.get("REFRESH_SECONDS", "300") or 300))
STATE_DIR = os.environ.get("STATE_DIRECTORY") or "."
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OLD_CARDS_FILE = os.path.join(STATE_DIR, "cards.json")     # from before state.json
MAX_CARDS = 40
# A card the bot isn't allowed to edit is kept this long, so giving WindBot the
# channel permissions brings it back to life without posting it again.
FORBIDDEN_GRACE = 3 * 24 * 3600
# What a card needs in its channel to keep itself current. Without Embed Links
# Discord still accepts an edit but quietly drops the card from it.
NEEDS = ("view_channel", "read_message_history", "send_messages", "embed_links")
BOARD_ROWS = 10
STATUS_EVERY = 60                 # status cards are redrawn at least this often (seconds)

GREEN, RED, PURPLE, GREY = 0x3BA55D, 0xED4245, 0x9B59B6, 0x99AAB5
MAP_BLUE = 0x025AE5
TEAMS = {0: ("No team", GREY), 1: ("Mystic", 0x3B82F6), 2: ("Valor", 0xEF4444),
         3: ("Instinct", 0xFACC15)}
PERIODS = {"week": "This week", "all": "All time"}
METRICS = {"caught": "Caught", "spun": "PokéStops spun", "walked": "Walked", "level": "Level"}
MEDALS = {1: "\N{FIRST PLACE MEDAL}", 2: "\N{SECOND PLACE MEDAL}", 3: "\N{THIRD PLACE MEDAL}"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EVERY = f"Updates every {REFRESH_SECONDS // 60} minutes"


def _log(msg):
    print(f"[windbot] {msg}", flush=True)


def trainers(n):
    return f"{n} trainer{'' if n == 1 else 's'} online"


def clean(name):
    """A trainer's name, safe inside a card (no markdown, no mentions)."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(name or "?")))


def amount(metric, value):
    if metric == "walked":
        return f"{float(value or 0):,.1f} km"
    if metric == "level":
        return f"Level {int(value or 0)}"
    return f"{int(value or 0):,}"


# ------------------------------------------------------------------ the cards
#
# Every card is (kind, args): "st" status, "lb" leaderboard ("week:caught"),
# "pf" profile (the trainer's name), "ev" events. render() draws one from
# scratch, which is all a Refresh or a timed update has to do.

def status_card(s):
    # Discord keeps "<t:...:R>" ticking on its own, so the card always says how
    # fresh it is, even between the bot's edits.
    checked = f"\nChecked <t:{int(s.get('checked') or time.time())}:R>"
    if s["up"]:
        card = discord.Embed(title="Windstock is online", colour=GREEN,
                             description=f"**{trainers(s['players'])}**{checked}")
    else:
        card = discord.Embed(title="Windstock is offline", colour=RED,
                             description=f"The server isn't answering right now.{checked}")
    card.set_footer(text="Updates every minute")
    return card


def unreachable(title):
    card = discord.Embed(title=title, colour=GREY,
                         description="Couldn't reach Windstock just now. This card will try again.")
    card.set_footer(text=EVERY)
    return card


async def leaderboard_card(args):
    period, _, by = args.partition(":")
    period = period if period in PERIODS else "week"
    by = by if by in METRICS else "caught"
    if by == "level":
        period = "all"                    # level has no weekly board
    title = f"Leaderboard: {METRICS[by]}, {PERIODS[period].lower()}"
    d = await bot.site(f"/api/leaderboard?period={period}&by={by}")
    if not d or not d.get("ok"):
        return unreachable(title)
    lines = []
    for row in (d.get("rows") or [])[:BOARD_ROWS]:
        rank = int(row.get("rank") or len(lines) + 1)
        place = MEDALS.get(rank, f"`{rank:>2}.`")
        extra = "" if by == "level" else f" · Lv {int(row.get('level') or 0)}"
        lines.append(f"{place} **{clean(row.get('username'))}**{extra} · {amount(by, row.get(by))}")
    text = "\n".join(lines) or "Nobody is on this board yet."
    if period == "week" and d.get("weekEndsAt"):
        text += f"\n\nThe week ends <t:{int(d['weekEndsAt']) // 1000}:R>."
    card = discord.Embed(title=title, colour=PURPLE, description=text)
    card.set_footer(text=EVERY)
    return card


async def profile_card(name):
    d = await bot.site("/api/trainer?name=" + urllib.parse.quote(name), missing_ok=True)
    if d is None:
        return unreachable(f"Trainer {clean(name)}")
    t = d.get("trainer")
    if not d.get("ok") or not t:
        return discord.Embed(title=f"Trainer {clean(name)}", colour=GREY,
                             description="No trainer by that name is on the leaderboard.")
    team, colour = TEAMS.get(int(t.get("team") or 0), TEAMS[0])
    total = int(d.get("total") or 0)
    card = discord.Embed(title=clean(t.get("username")), colour=colour,
                         description=f"**Level {int(t.get('level') or 0)}** · {team}")
    ranks = t.get("ranks") or {}
    for period in ("all", "week"):
        stats = t if period == "all" else (t.get("week") or {})
        lines = []
        for metric in ("caught", "spun", "walked"):
            rank = (ranks.get(period) or {}).get(metric)
            place = f" · #{rank}" if rank else ""
            lines.append(f"{METRICS[metric]}: **{amount(metric, stats.get(metric))}**{place}")
        card.add_field(name=PERIODS[period], value="\n".join(lines))
    seen = int(t.get("updatedAt") or 0) // 1000
    tail = f"Ranks are out of {total} trainer{'' if total == 1 else 's'}."
    if seen:
        tail = f"Last played <t:{seen}:R>. " + tail
    card.add_field(name="​", value=tail, inline=False)
    card.set_footer(text=EVERY)
    return card


def _day(text):
    """'2026-10-20' -> 'Oct 20'."""
    try:
        t = time.strptime(text, "%Y-%m-%d")
        return f"{time.strftime('%b', t)} {t.tm_mday}"
    except (TypeError, ValueError):
        return str(text or "")


def when(e):
    """When an event runs, in words: its dates, weekdays and hours."""
    parts = []
    if e.get("from") and e.get("to") and e["from"] != e["to"]:
        parts.append(f"{_day(e['from'])} to {_day(e['to'])}")
    elif e.get("from") or e.get("to"):
        parts.append(_day(e.get("from") or e.get("to")))
    if e.get("days"):
        parts.append(", ".join(WEEKDAYS[d % 7] for d in sorted(e["days"])))
    start, end = e.get("start") or "00:00", e.get("end") or "24:00"
    if not (start == "00:00" and end >= "23:59"):
        parts.append(f"{start} to {end}")
    return ", ".join(parts)


def event_lines(e):
    out = []
    w = when(e)
    if w:
        out.append(w)
    elif e.get("end_ms"):
        out.append(f"Ends <t:{int(e['end_ms']) // 1000}:R>")
    if e.get("bonuses"):
        out.append(" · ".join(clean(b) for b in e["bonuses"]))
    return "\n".join(out) or "On now."


async def events_card(started=None):
    """The /events card; `started` (an event's name) words it as an announcement."""
    d = await bot.game("/windstock/events")
    if not d or not d.get("ok"):
        return unreachable("Windstock events")
    now, coming = d.get("current"), d.get("upcoming") or []
    title = f"{clean(started)} has started!" if started else "Windstock events"
    card = discord.Embed(title=title, colour=PURPLE)
    if now:
        card.add_field(name=f"On now: {clean(now.get('name'))}", value=event_lines(now), inline=False)
    elif not coming:
        card.description = "No event is running and none is scheduled."
    else:
        card.description = "No event is running right now."
    for e in ([] if started else coming):
        card.add_field(name=f"Coming up: {clean(e.get('name'))}", value=event_lines(e), inline=False)
    if d.get("local_time") and (now or coming):
        card.add_field(name="​", inline=False,
                       value="Dates and hours go by your own phone's clock.")
    if not started:
        card.set_footer(text=EVERY)
    return card


SITE_PAGES = {"lb": "/leaderboard.html", "pf": "/leaderboard.html", "ev": "/events.html"}


def view_for(kind, args=""):
    view = discord.ui.View(timeout=None)
    view.add_item(RefreshButton(kind, args))
    if SITE_URL:
        view.add_item(discord.ui.Button(label="Go to website",
                                        url=SITE_URL + SITE_PAGES.get(kind, "")))
        if kind == "st":
            view.add_item(discord.ui.Button(label="Play map", url=SITE_URL + "/map.html"))
    return view


async def render(kind, args=""):
    """(embed, view) for a card, drawn fresh."""
    if kind == "lb":
        card = await leaderboard_card(args)
    elif kind == "pf":
        card = await profile_card(args)
    elif kind == "ev":
        card = await events_card()
    else:
        kind, args = "st", ""
        card = status_card(await bot.check())
    card.timestamp = discord.utils.utcnow()
    return card, view_for(kind, args)


class RefreshButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"wb:(?P<kind>[a-z]{2})(?::(?P<args>.*))?"):
    """Refresh, on any card. What the card is rides in the button's own id, so
    it still works on a card posted before the bot last restarted."""

    def __init__(self, kind, args=""):
        super().__init__(discord.ui.Button(
            label="Refresh", style=discord.ButtonStyle.secondary,
            emoji="\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}",
            custom_id=f"wb:{kind}:{args}"[:100]))
        self.kind, self.args = kind, args

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"], match["args"] or "")

    async def callback(self, interaction: discord.Interaction):
        card, view = await render(self.kind, self.args)
        await interaction.response.edit_message(embed=card, view=view)


class OldStatusView(discord.ui.View):
    """The Refresh button on status cards from before RefreshButton."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Refresh", custom_id="windbot:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        card, view = await render("st")
        await interaction.response.edit_message(embed=card, view=view)


# ------------------------------------------------------------------ the bot

class Windbot(discord.Client):
    def __init__(self):
        # No intents: the bot reads nothing from the servers it is in.
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)
        self.http_session = None
        self.state = {"up": False, "players": 0, "checked": 0.0}
        self.shown = None                 # the status line Discord has now
        self.status_shown = None          # the (up, players) the status cards say now
        self.status_drawn = 0.0           # when the status cards were last redrawn
        self.names = (0.0, [])            # trainer names for /profile's suggestions
        self.linked = (0.0, [])           # linked trainers for /trade's suggestions
        self.boxes = {}                   # login -> (when, trading.box answer)
        # Kept on disk, so a restart forgets nothing:
        #   cards     the cards that keep themselves current, oldest first:
        #             {"c": channel id, "m": message id, "k": kind, "a": args}
        #   announce  the channel event starts are posted in (or None)
        #   event     the running event already announced (or None)
        #   bounties  TikTok bounty entries: {id: {"u", "url", "login", "name", "at", "paid", "state"}}
        #   bounty_channel  where new entries go for review (or None)
        self.cards, self.announce, self.event = [], None, None
        self.bounties, self.bounty_channel = {}, None
        self.event_known = False          # False until the bot has looked once, ever
        self.load()

    def load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            self.cards = [dict({"c": int(r["c"]), "m": int(r["m"]), "k": str(r["k"]), "a": str(r.get("a") or "")},
                               **({"denied": float(r["denied"])} if r.get("denied") else {}))
                          for r in d.get("cards") or []]
            self.announce = int(d["announce"]) if d.get("announce") else None
            self.event = d.get("event") or None
            self.event_known = bool(d.get("event_known"))
            self.bounties = d.get("bounties") if isinstance(d.get("bounties"), dict) else {}
            self.bounty_channel = int(d["bounty_channel"]) if d.get("bounty_channel") else None
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        try:                              # the status cards an older Windbot tracked
            with open(OLD_CARDS_FILE, encoding="utf-8") as fh:
                self.cards = [{"c": int(c), "m": int(m), "k": "st", "a": ""}
                              for c, m in json.load(fh).items()]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save(self):
        try:
            with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"cards": self.cards, "announce": self.announce,
                           "event": self.event, "event_known": self.event_known,
                           "bounties": self.bounties, "bounty_channel": self.bounty_channel}, fh)
            os.replace(STATE_FILE + ".tmp", STATE_FILE)
        except OSError as e:
            _log(f"could not save state: {e}")

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5))
        # Registered here so Refresh on a card from before a restart still works.
        self.add_dynamic_items(RefreshButton, TradeButton, BountyButton)
        self.add_view(OldStatusView())
        await self.tree.sync()
        self.poll.start()
        self.refresh.start()

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

    # -------------------------------------------------------------- asking

    async def fetch(self, url, missing_ok=False, data=None, headers=None):
        """The JSON at `url`, or None when it can't be had. With missing_ok an
        error's own JSON is returned too (the site says why in it). With `data`
        it is a POST of that JSON."""
        try:
            ask = (self.http_session.post(url, json=data, headers=headers) if data is not None
                   else self.http_session.get(url, headers=headers))
            async with ask as r:
                if r.status == 200 or (missing_ok and 400 <= r.status < 500):
                    d = await r.json(content_type=None)
                    return d if isinstance(d, dict) else None
        except Exception:
            pass
        return None

    async def game(self, path):
        return await self.fetch(GAME_URL + path)

    async def site(self, path, missing_ok=False):
        return await self.fetch(SITE_API + path, missing_ok)

    async def game_post(self, path, data):
        return await self.fetch(GAME_URL + path, missing_ok=True, data=data)

    async def site_game(self, path, data=None):
        """The website's GAME_KEY routes (Discord links), errors' JSON included."""
        return await self.fetch(SITE_API + path, missing_ok=True, data=data,
                                headers={"X-Game-Key": SITE_GAME_KEY})

    async def game_admin(self, path, data):
        """The game server's /windstock/admin/* routes (loopback + the same GAME_KEY)."""
        return await self.fetch(GAME_URL + path, missing_ok=True, data=data,
                                headers={"X-Game-Key": SITE_GAME_KEY})

    async def trainer_of(self, user_id):
        """{login, name} of the trainer this Discord user linked, or None."""
        if TRADE_TEST_LOGIN and self.user and int(user_id) == self.user.id:
            return {"ok": True, "login": TRADE_TEST_LOGIN, "name": "Test Trainer"}
        d = await self.site_game(f"/api/game/discord/who?id={int(user_id)}")
        return d if d and d.get("ok") and d.get("login") else None

    async def linked_trainers(self):
        """[{id, login, name, discord}]: trainers who /link'ed (kept 20 s)."""
        at, rows = self.linked
        if time.time() - at > 20:
            d = await self.site_game("/api/game/discord/linked")
            if d and d.get("ok"):
                rows = d.get("trainers") or []
            self.linked = (time.time(), rows)
        return rows

    async def box(self, login, fresh=False):
        """A trainer's Pokemon (trading.box), kept 20 s for the autocompletes."""
        at, d = self.boxes.get(login, (0.0, None))
        if fresh or d is None or time.time() - at > 20:
            d = await self.game("/windstock/trade/box?login=" + urllib.parse.quote(login))
            self.boxes[login] = (time.time(), d)
            if len(self.boxes) > 200:
                self.boxes.pop(next(iter(self.boxes)))
        return d

    async def check(self):
        """Ask the game server. Any failure at all means "offline"."""
        d = await self.game("/windstock/players") or {}
        up = bool(d.get("ok"))
        players = int(d.get("players") or 0) if up else 0
        if up != self.state["up"]:
            _log("server is " + ("up" if up else "DOWN"))
        self.state = {"up": up, "players": players, "checked": time.time()}
        return self.state

    async def trainer_names(self):
        """Names on the board, for /profile's suggestions (asked at most once a minute)."""
        at, names = self.names
        if time.time() - at > 60:
            d = await self.site("/api/leaderboard?period=all&by=caught")
            if d and d.get("ok"):
                names = [str(r.get("username")) for r in d.get("rows") or [] if r.get("username")]
            self.names = (time.time(), names)
        return names

    # -------------------------------------------------------------- live cards

    def track_card(self, channel_id, message_id, kind, args=""):
        """This is now the card of its kind that updates itself in its channel
        (profiles: one per trainer)."""
        self.cards = [r for r in self.cards
                      if not (r["c"] == channel_id and r["k"] == kind
                              and (kind != "pf" or r["a"].lower() == args.lower()))]
        self.cards.append({"c": channel_id, "m": message_id, "k": kind, "a": args})
        del self.cards[:-MAX_CARDS]                   # the oldest go first
        self.save()

    async def update_cards(self, kinds):
        """Redraw every live card of these kinds. One that is deleted is
        forgotten; one in a channel the bot may not see is kept for a while
        (FORBIDDEN_GRACE) in case it is let in."""
        gone, changed = [], False
        for row in [r for r in self.cards if r["k"] in kinds]:
            message = self.get_partial_messageable(row["c"]).get_partial_message(row["m"])
            try:
                card, view = await render(row["k"], row["a"])
                await message.edit(embed=card, view=view)
                if row.pop("denied", None):
                    _log(f"a {row['k']} card can be edited again")
                    changed = True
            except discord.NotFound:
                gone.append(row)
            except discord.Forbidden as e:
                if not row.get("denied"):
                    row["denied"] = time.time()
                    changed = True
                    _log(f"no access to a {row['k']} card's channel ({e.status}: {e.text}); "
                         "give WindBot View Channel, Send Messages, Embed Links + Read Message History there")
                elif time.time() - row["denied"] > FORBIDDEN_GRACE:
                    gone.append(row)
            except discord.HTTPException as e:
                _log(f"card not updated this time: {e!r}")
        if gone or changed:
            self.cards = [r for r in self.cards if r not in gone]
            self.save()

    async def announce_events(self):
        """Post once in the announcements channel when an event starts."""
        d = await self.game("/windstock/events")
        if not d or not d.get("ok"):
            return                        # server down: decide nothing
        name = (d.get("current") or {}).get("name") or None
        if name == self.event and self.event_known:
            return
        first_look, self.event, self.event_known = not self.event_known, name, True
        self.save()
        if first_look or not name or not self.announce:
            return                        # an event already running when the bot arrived is not news
        try:
            card = await events_card(started=name)
            card.timestamp = discord.utils.utcnow()
            await self.get_partial_messageable(self.announce).send(embed=card, view=view_for("ev"))
            _log(f"announced {name}")
        except discord.HTTPException as e:
            _log(f"could not announce {name}: {e!r}")

    # -------------------------------------------------------------- the clock

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll(self):
        s = await self.check()
        line = trainers(s["players"]) if s["up"] else "Server offline"
        if line != self.shown:            # Discord limits how often this may change
            await self.change_presence(
                status=discord.Status.online if s["up"] else discord.Status.dnd,
                activity=discord.CustomActivity(name=line))
            self.shown = line
            _log(f"status line: {line}")
        now = (s["up"], s["players"])
        if now != self.status_shown or time.time() - self.status_drawn >= STATUS_EVERY - 1:
            await self.update_cards({"st"})
            self.status_shown, self.status_drawn = now, time.time()
        await self.announce_events()

    @tasks.loop(seconds=REFRESH_SECONDS)
    async def refresh(self):
        await self.update_cards({"lb", "pf", "ev"})

    @poll.before_loop
    @refresh.before_loop
    async def _before_loops(self):
        await self.wait_until_ready()

    @poll.error
    async def _poll_error(self, error):
        _log(f"poll failed: {error!r}")
        self.shown = self.status_shown = None
        self.poll.restart()

    @refresh.error
    async def _refresh_error(self, error):
        _log(f"refresh failed: {error!r}")
        self.refresh.restart()

    async def on_ready(self):
        _log(f"signed in as {self.user}; {len(self.cards)} live card(s)")
        self.shown = None                 # a reconnect starts with no status line


bot = Windbot()


async def post(interaction, kind, args=""):
    """Answer a command with a card, and keep that card up to date from now on."""
    card, view = await render(kind, args)
    await interaction.response.send_message(embed=card, view=view)
    try:
        sent = await interaction.original_response()
        bot.track_card(sent.channel.id, sent.id, kind, args)
    except discord.HTTPException as e:
        _log(f"card won't auto-update: {e!r}")
        return
    # A command answer goes through even where the bot can't see the channel,
    # but its timed updates won't: say so now rather than leave a frozen card.
    perms = interaction.app_permissions
    if interaction.guild_id and not all(getattr(perms, p, False) for p in NEEDS):
        try:
            await interaction.followup.send(
                "Heads up: this card can't update itself here. In this channel's permissions, "
                "give the **WindBot** role **View Channel**, **Send Messages**, **Embed Links** "
                "and **Read Message History**; the card starts updating on its own.", ephemeral=True)
        except discord.HTTPException:
            pass


@bot.tree.command(name="status", description="Is the Windstock server up, and how many are playing?")
async def status(interaction: discord.Interaction):
    await post(interaction, "st")


@bot.tree.command(name="leaderboard", description="The top Windstock trainers")
@app_commands.describe(period="This week (the default) or all time", by="What to rank by")
@app_commands.choices(
    period=[app_commands.Choice(name=label, value=key) for key, label in PERIODS.items()],
    by=[app_commands.Choice(name=label, value=key) for key, label in METRICS.items()])
async def leaderboard(interaction: discord.Interaction, period: str = "week", by: str = "caught"):
    if by == "level":
        period = "all"
    await post(interaction, "lb", f"{period}:{by}")


@bot.tree.command(name="profile", description="A trainer's level and stats")
@app_commands.describe(trainer="The trainer's name, as the leaderboard shows it")
async def profile(interaction: discord.Interaction, trainer: app_commands.Range[str, 1, 40]):
    name = trainer.strip()
    d = await bot.site("/api/trainer?name=" + urllib.parse.quote(name), missing_ok=True)
    if d is not None and not d.get("ok"):
        return await interaction.response.send_message(
            f"No trainer called **{clean(name)}** is on the leaderboard.", ephemeral=True)
    await post(interaction, "pf", name)


@profile.autocomplete("trainer")
async def _profile_names(interaction: discord.Interaction, current: str):
    want = current.strip().lower()
    names = [n for n in await bot.trainer_names() if want in n.lower()]
    return [app_commands.Choice(name=n[:100], value=n[:40]) for n in names[:25]]


@bot.tree.command(name="events", description="What's on in Windstock now, and what's coming")
async def events(interaction: discord.Interaction):
    await post(interaction, "ev")


# ------------------------------------------------------------------ the play map
#
# The website's map.html draws where there are real PokeStops and Gyms from its
# assets/coverage.json (rebuilt by the site's tools/build-coverage.py). /map
# sums that file up and links to the page; it's a plain reply, not a card.

_coverage = {"at": 0, "data": None}


async def coverage():
    if _coverage["data"] is None or time.time() - _coverage["at"] > 3600:
        d = await bot.site("/assets/coverage.json")
        if d and d.get("regions"):
            _coverage.update(at=time.time(), data=d)
    return _coverage["data"]


def map_card(d):
    card = discord.Embed(title="Where you can play", colour=MAP_BLUE,
                         url=(SITE_URL + "/map.html") if SITE_URL else None)
    if not d:
        card.description = "Couldn't load the map's numbers just now."
        return card
    total = d["total"]["stops"] + d["total"]["gyms"]
    card.description = (f"**{total:,}** real PokéStops and Gyms. Outside these areas wild Pokémon "
                        "still appear, but there are no stops or gyms yet.")
    for r in sorted(d.get("regions", []), key=lambda r: r["stops"] + r["gyms"], reverse=True):
        card.add_field(name=clean(r.get("name")), value=f"{r['stops'] + r['gyms']:,}", inline=True)
    cities = sorted(d.get("cities", []), key=lambda c: c["stops"] + c["gyms"], reverse=True)[:5]
    if cities:
        card.add_field(name="Busiest cities", inline=False, value="\n".join(
            f"{clean(c['name'])}: {c['stops'] + c['gyms']:,}" for c in cities))
    if d.get("generated"):
        card.set_footer(text=f"Coverage updated {d['generated']}")
    return card


@bot.tree.command(name="map", description="Where in the world you can play Windstock")
async def play_map(interaction: discord.Interaction):
    await interaction.response.defer()
    view = discord.utils.MISSING
    if SITE_URL:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Open the play map", url=SITE_URL + "/map.html"))
    await interaction.followup.send(embed=map_card(await coverage()), view=view)


@bot.tree.command(name="woah", description="woah")
async def woah(interaction: discord.Interaction):
    await interaction.response.send_message("woah 😮")


@bot.tree.command(name="announcements", description="Post in this channel when a Windstock event starts")
@app_commands.describe(where="Here, or switch the announcements off")
@app_commands.choices(where=[app_commands.Choice(name="In this channel", value="here"),
                             app_commands.Choice(name="Off", value="off")])
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def announcements(interaction: discord.Interaction, where: str = "here"):
    if where == "off":
        bot.announce = None
        bot.save()
        return await interaction.response.send_message("Event announcements are off.", ephemeral=True)
    # A real message, not a reply: a reply always goes through, and an
    # announcement later won't have one to ride on.
    try:
        await bot.get_partial_messageable(interaction.channel_id).send(
            "Windstock event announcements will be posted in this channel.")
    except discord.HTTPException:
        return await interaction.response.send_message(
            "I can't post in this channel. Give WindBot **View Channel**, **Send Messages** "
            "and **Embed Links** here, then run this again.", ephemeral=True)
    bot.announce = interaction.channel_id
    bot.save()
    await interaction.response.send_message("Done.", ephemeral=True)


# ------------------------------------------------------------------ trading
#
# The game has no trade screen, so trades are made here. /link ties a Discord
# user to their trainer (a code from their profile page on the website); /trade
# posts an offer with Accept / Decline buttons, and Accept asks the game server
# to swap the two Pokemon (trading.py there checks everything again and does it
# all or not at all). The whole offer rides in the buttons' ids, so an offer
# survives a restart of the bot; it runs out after OFFER_MINUTES.

OFFER_MINUTES = 30
SPRITE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{}{}.png"
TRADE_GOLD = 0xF1C40F
SPARKLE = "\N{SPARKLES}"


def mon_text(m):
    """'✨ Charizard "Blaze" · CP 1520'"""
    name = clean(m.get("name"))
    if m.get("nickname"):
        name += f" “{clean(m['nickname'])}”"
    return f"{SPARKLE + ' ' if m.get('shiny') else ''}{name} · CP {int(m.get('cp') or 0):,}"


def mon_choice(m):
    """The same words as mon_text, plain: suggestions show no markdown."""
    name = str(m.get("name") or "?")
    if m.get("nickname"):
        name += f" \u201c{m['nickname']}\u201d"
    label = f"{SPARKLE + ' ' if m.get('shiny') else ''}{name} · CP {int(m.get('cp') or 0):,}"
    return app_commands.Choice(name=label[:100], value=str(m["uid"]))


def sprite(m):
    return SPRITE.format("shiny/" if m.get("shiny") else "", int(m.get("id") or 0))


def not_linked(whose="You"):
    where = f" on {SITE_URL}/account.html" if SITE_URL else " on your Windstock profile page"
    return (f"{whose} haven't linked a Windstock trainer yet. Get a code{where}, "
            "then type `/link` and the code here.") if whose == "You" else \
           f"{whose} hasn't linked a Windstock trainer to Discord yet, so they can't trade."


def offer_card(q, state="open", note=""):
    """The offer, as the two trainers see it. q is trading.quote's answer."""
    a, b = q["a_mon"], q["b_mon"]
    titles = {"open": "Trade offer", "done": "Trade complete!", "declined": "Trade declined",
              "cancelled": "Trade cancelled", "expired": "Trade offer ran out"}
    colour = {"open": TRADE_GOLD, "done": GREEN}.get(state, GREY)
    card = discord.Embed(title=titles[state], colour=colour)
    card.add_field(name=f"{clean(q['a_name'])} gives", value=mon_text(a))
    card.add_field(name=f"{clean(q['b_name'])} gives", value=mon_text(b))
    cost = int(q.get("cost") or 0)
    candy = int(q.get("candy") or 0)
    deal = f"{cost:,} stardust each"
    if candy:
        deal += f" · each gets {candy} candy for the Pokémon they send"
    card.add_field(name="Cost", value=deal, inline=False)
    if note:
        card.add_field(name="​", value=note, inline=False)
    card.set_thumbnail(url=sprite(b if state == "done" else a))
    return card


def offer_view(a_id, b_id, a_uid, b_uid):
    view = discord.ui.View(timeout=None)
    for act in ("y", "n"):
        view.add_item(TradeButton(act, a_id, b_id, a_uid, b_uid))
    return view


class TradeButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"wt:(?P<act>[yn]):(?P<a>\d+):(?P<b>\d+):(?P<au>\d+):(?P<bu>\d+)"):
    """Accept (y) or Decline (n) on a trade offer. `a` offered to `b`; `au` is
    the Pokemon a gives, `bu` the one b gives."""

    def __init__(self, act, a, b, au, bu):
        accept = act == "y"
        super().__init__(discord.ui.Button(
            label="Accept" if accept else "Decline",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"wt:{act}:{a}:{b}:{au}:{bu}"))
        self.act, self.a, self.b, self.au, self.bu = act, int(a), int(b), str(au), str(bu)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], match["a"], match["b"], match["au"], match["bu"])

    async def callback(self, interaction: discord.Interaction):
        who = interaction.user.id
        say = interaction.response.send_message
        if who not in (self.a, self.b):
            return await say("This trade isn't yours.", ephemeral=True)
        ta, tb = await bot.trainer_of(self.a), await bot.trainer_of(self.b)
        if not ta or not tb:
            return await say("One of you isn't linked to a trainer any more.", ephemeral=True)
        body = {"a": ta["login"], "b": tb["login"], "a_uid": self.au, "b_uid": self.bu}
        q = await bot.game_post("/windstock/trade/quote", body)
        if q is None:
            return await say("Couldn't reach Windstock just now. Try again in a minute.", ephemeral=True)
        q = q if q.get("ok") else None
        sent = interaction.message.created_at if interaction.message else None
        expired = sent and (discord.utils.utcnow() - sent).total_seconds() > OFFER_MINUTES * 60
        if self.act == "n" or expired:
            state = "expired" if expired else ("cancelled" if who == self.a else "declined")
            if q:
                return await interaction.response.edit_message(embed=offer_card(q, state), view=None, content=None)
            return await interaction.response.edit_message(
                embed=discord.Embed(title=f"Trade {state if state != 'expired' else 'offer ran out'}",
                                    colour=GREY), view=None, content=None)
        if who != self.b:
            return await say("Waiting for the other trainer to accept.", ephemeral=True)
        d = await bot.game_post("/windstock/trade/do", body)
        if d is None:
            return await say("Couldn't reach Windstock just now. Try again in a minute.", ephemeral=True)
        if not d.get("ok"):
            return await say(f"The trade didn't go through: {d.get('error') or 'unknown reason'}", ephemeral=True)
        bot.boxes.pop(ta["login"], None)
        bot.boxes.pop(tb["login"], None)
        _log(f"trade: {ta['login']} {d['a_mon']['name']} <-> {tb['login']} {d['b_mon']['name']}")
        await interaction.response.edit_message(
            content=f"<@{self.a}> <@{self.b}>",
            embed=offer_card(d, "done", "Your new Pokémon is in your box next time the game refreshes."),
            view=None, allowed_mentions=discord.AllowedMentions(users=True))
        # An edit never notifies anyone: ping the trainer who made the offer.
        await interaction.followup.send(
            f"<@{self.a}>, **{clean(tb.get('name'))}** accepted your trade! You got **{clean(d['b_mon']['name'])}**.",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(self.a)]))


@bot.tree.command(name="link", description="Link your Windstock trainer, so you can trade")
@app_commands.describe(code="The code from your profile page on the Windstock website")
async def link(interaction: discord.Interaction, code: app_commands.Range[str, 4, 20]):
    if not SITE_GAME_KEY:
        return await interaction.response.send_message("Trading isn't set up on this bot yet.", ephemeral=True)
    d = await bot.site_game("/api/game/discord/link",
                            {"code": code.strip(), "id": str(interaction.user.id), "name": interaction.user.name})
    if d is None:
        return await interaction.response.send_message("Couldn't reach the website just now. Try again in a minute.", ephemeral=True)
    if not d.get("ok"):
        return await interaction.response.send_message(d.get("error") or "That didn't work.", ephemeral=True)
    await interaction.response.send_message(
        f"Linked! You're **{clean(d.get('name'))}** in Windstock. Try `/trade` to swap Pokémon with another trainer.",
        ephemeral=True)


@bot.tree.command(name="unlink", description="Unlink your Windstock trainer from this Discord account")
async def unlink(interaction: discord.Interaction):
    d = await bot.site_game("/api/game/discord/unlink", {"id": str(interaction.user.id)})
    if d is None:
        return await interaction.response.send_message("Couldn't reach the website just now.", ephemeral=True)
    await interaction.response.send_message(
        "Unlinked." if d.get("was") else "This Discord account wasn't linked to a trainer.", ephemeral=True)


@bot.tree.command(name="trade", description="Offer another trainer a Pokémon swap")
@app_commands.describe(trainer="Who to trade with (only trainers who used /link)", give="Your Pokémon to send",
                       get="Their Pokémon you want")
@app_commands.guild_only()
async def trade(interaction: discord.Interaction, trainer: str, give: str, get: str):
    say = interaction.response.send_message
    if not SITE_GAME_KEY:
        return await say("Trading isn't set up on this bot yet.", ephemeral=True)
    trainer = await trainer_user(interaction, trainer)
    if not trainer:
        return await say("Pick the trainer from the suggestions as you type.", ephemeral=True)
    test = bool(TRADE_TEST_LOGIN) and trainer.id == bot.user.id
    if test and not interaction.permissions.manage_guild:
        return await say("Only server managers can trade with the test trainer.", ephemeral=True)
    if (trainer.bot and not test) or trainer.id == interaction.user.id:
        return await say("Pick another trainer to trade with.", ephemeral=True)
    if not give.isdigit() or not get.isdigit():
        return await say("Pick both Pokémon from the suggestions as you type.", ephemeral=True)
    me, them = await bot.trainer_of(interaction.user.id), await bot.trainer_of(trainer.id)
    if not me:
        return await say(not_linked(), ephemeral=True)
    if not them:
        return await say(not_linked(trainer.display_name), ephemeral=True)
    q = await bot.game_post("/windstock/trade/quote",
                            {"a": me["login"], "b": them["login"], "a_uid": give, "b_uid": get})
    if q is None:
        return await say("Couldn't reach Windstock just now. Try again in a minute.", ephemeral=True)
    if not q.get("ok"):
        return await say(f"That trade can't happen: {q.get('error')}", ephemeral=True)
    if test:
        return await test_trade(interaction, q, {"a": me["login"], "b": them["login"], "a_uid": give, "b_uid": get})
    note = f"<@{trainer.id}>, press **Accept** to trade. This offer runs out in {OFFER_MINUTES} minutes."
    await say(content=f"<@{trainer.id}>, **{clean(me.get('name'))}** wants to trade with you!",
              embed=offer_card(q, "open", note),
              view=offer_view(interaction.user.id, trainer.id, give, get),
              allowed_mentions=discord.AllowedMentions(users=[trainer]))


async def test_trade(interaction, q, body):
    """A trade with the test trainer (TRADE_TEST_LOGIN): shown as an offer,
    then accepted by the bot itself a moment later."""
    await interaction.response.send_message(
        embed=offer_card(q, "open", "Test Trainer is looking at your offer..."))
    await asyncio.sleep(3)
    d = await bot.game_post("/windstock/trade/do", body)
    if d and d.get("ok"):
        bot.boxes.pop(body["a"], None)
        bot.boxes.pop(body["b"], None)
        _log(f"test trade: {body['a']} {d['a_mon']['name']} <-> {d['b_mon']['name']}")
        card = offer_card(d, "done", "Your new Pokémon is in your box next time the game refreshes.")
    else:
        why = (d or {}).get("error") or "couldn't reach Windstock"
        card = offer_card(q, "declined", f"The trade didn't go through: {why}")
    await interaction.edit_original_response(embed=card)


async def trainer_user(interaction, value):
    """The Discord user /trade's trainer option names (a Discord id from the
    suggestions, or a mention typed by hand), or None."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not 5 <= len(digits) <= 25:
        return None
    uid = int(digits)
    if interaction.guild and interaction.guild.get_member(uid):
        return interaction.guild.get_member(uid)
    try:
        return await bot.fetch_user(uid)
    except discord.HTTPException:
        return None


@trade.autocomplete("trainer")
async def _trainer_choices(interaction: discord.Interaction, current: str):
    want = current.strip().lower()
    rows = [r for r in await bot.linked_trainers() if str(r.get("id")) != str(interaction.user.id)]
    if TRADE_TEST_LOGIN and interaction.permissions.manage_guild and bot.user:
        rows.append({"id": str(bot.user.id), "name": "Test Trainer", "discord": bot.user.name})
    out = []
    for r in sorted(rows, key=lambda r: str(r.get("name") or "").lower()):
        label = str(r.get("name") or "?") + (f" (@{r['discord']})" if r.get("discord") else "")
        if want and want not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(r["id"])))
        if len(out) == 25:
            break
    return out


async def _box_choices(user_id, current):
    t = await bot.trainer_of(user_id) if user_id else None
    d = await bot.box(t["login"]) if t else None
    if not d or not d.get("ok"):
        return []
    want = current.strip().lower()
    mons = [m for m in d.get("pokemon") or [] if not m.get("blocked")
            and (not want or want in m["name"].lower() or want in str(m.get("nickname") or "").lower())]
    return [mon_choice(m) for m in mons[:25]]


@trade.autocomplete("give")
async def _give_choices(interaction: discord.Interaction, current: str):
    return await _box_choices(interaction.user.id, current)


@trade.autocomplete("get")
async def _get_choices(interaction: discord.Interaction, current: str):
    other = "".join(ch for ch in str(getattr(interaction.namespace, "trainer", "") or "") if ch.isdigit())
    return await _box_choices(int(other) if 5 <= len(other) <= 25 else None, current)


# ------------------------------------------------------------------ video bounty
#
# /bounty <link> sends in a TikTok video. The entry goes to the review channel
# (/bountyreview) with one button per reward; a manager presses the ones it has
# earned and the bot pays them in game through /windstock/admin/reward. Each
# reward is paid once per video, and each video counts once.

# key: (button label, coins, {item id: count}, what the player is told)
BOUNTY_REWARDS = {
    "post": ("Posted", 50, {}, "50 coins for posting"),
    "1k": ("1k views", 150, {301: 2}, "150 coins + 2 Lucky Eggs for 1k views"),
    "10k": ("10k views", 800, {401: 5, 301: 3}, "800 coins + 5 Incense + 3 Lucky Eggs for 10k views"),
    "25k": ("25k views", 1500, {4: 1, 402: 2}, "1,500 coins + 1 Master Ball + 2 Shiny Incense for 25k views"),
}
BOUNTY_PINK = 0xFE2C55
BOUNTY_OPEN = True                # False = /bounty stops taking new videos


def tiktok_link(text):
    """The TikTok video link in `text`, tidied, or None."""
    u = urllib.parse.urlsplit(str(text or "").strip())
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return None
    if not u.path.strip("/"):
        return None
    return f"https://{host}{u.path.rstrip('/')}"


def bounty_card(bid, e):
    paid = e.get("paid") or []
    state = e.get("state") or "open"
    colour = {"open": BOUNTY_PINK, "rejected": GREY}.get(state, GREEN if paid else BOUNTY_PINK)
    card = discord.Embed(title="Video bounty entry", url=e["url"], colour=colour,
                         description=e["url"])
    card.add_field(name="Trainer", value=f"{clean(e.get('name'))} (<@{e['u']}>)")
    card.add_field(name="Sent in", value=f"<t:{int(e['at'])}:R>")
    if state == "rejected":
        card.add_field(name="Status", value="Rejected", inline=False)
    else:
        card.add_field(name="Paid", value=", ".join(BOUNTY_REWARDS[k][0] for k in paid if k in BOUNTY_REWARDS)
                       or "Nothing yet", inline=False)
    card.set_footer(text=f"Entry {bid} · check views 7 days after posting")
    return card


def bounty_view(bid, e):
    view = discord.ui.View(timeout=None)
    if (e.get("state") or "open") == "rejected":
        return view
    for key in BOUNTY_REWARDS:
        view.add_item(BountyButton(key, bid, done=key in (e.get("paid") or [])))
    if not e.get("paid"):
        view.add_item(BountyButton("no", bid))
    return view


class BountyButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"wv:(?P<act>[a-z0-9]+):(?P<bid>\d+)"):
    """On a review card: pay one reward (a BOUNTY_REWARDS key) or reject (no)."""

    def __init__(self, act, bid, done=False):
        label = "Reject" if act == "no" else BOUNTY_REWARDS.get(act, (act,))[0]
        style = (discord.ButtonStyle.danger if act == "no" else
                 discord.ButtonStyle.secondary if done else discord.ButtonStyle.success)
        super().__init__(discord.ui.Button(label=("✓ " if done else "") + label, style=style,
                                           disabled=done, custom_id=f"wv:{act}:{bid}"))
        self.act, self.bid = act, str(bid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["act"], match["bid"])

    async def callback(self, interaction: discord.Interaction):
        say = interaction.response.send_message
        if not interaction.permissions.manage_guild:
            return await say("Only server managers can pay bounties.", ephemeral=True)
        e = bot.bounties.get(self.bid)
        if not e:
            return await say("That entry is gone.", ephemeral=True)
        if self.act == "no":
            if e.get("paid"):
                return await say("This one has already been paid.", ephemeral=True)
            e["state"] = "rejected"
            bot.save()
            _log(f"bounty {self.bid} rejected by {interaction.user}")
            return await interaction.response.edit_message(embed=bounty_card(self.bid, e), view=bounty_view(self.bid, e))
        reward = BOUNTY_REWARDS.get(self.act)
        if not reward or self.act in (e.get("paid") or []) or e.get("state") == "rejected":
            return await say("Nothing to pay there.", ephemeral=True)
        label, coins, items, told = reward
        d = await bot.game_admin("/windstock/admin/reward",
                                 {"login": e["login"], "coins": coins, "items": {str(k): v for k, v in items.items()},
                                  "why": f"video bounty {label}"})
        if d is None:
            return await say("Couldn't reach Windstock just now. Try again in a minute.", ephemeral=True)
        if not d.get("ok"):
            return await say(f"Not paid: {d.get('error') or 'unknown reason'}", ephemeral=True)
        e.setdefault("paid", []).append(self.act)
        e["state"] = "paid"
        bot.save()
        _log(f"bounty {self.bid}: paid {label} to {e['login']} ({interaction.user})")
        await interaction.response.edit_message(embed=bounty_card(self.bid, e), view=bounty_view(self.bid, e))
        try:                              # a DM can be refused; the payout stands anyway
            user = await bot.fetch_user(int(e["u"]))
            await user.send(f"Your Windstock video bounty paid out: **{told}**! "
                            "It's in your bag next time the game refreshes. Thanks for spreading the word!")
        except discord.HTTPException:
            pass


def bounty_rules():
    lines = "\n".join(f"• **{r[0]}**: {r[3].split(' for ')[0]}" for r in BOUNTY_REWARDS.values())
    return ("**Windstock video bounty**\n"
            "Post a TikTok of you playing Windstock, mention **windstock.org** in the caption, "
            "then send the link here with `/bounty link:<your video>`.\n\n"
            f"{lines}\n\n"
            "Views are checked 7 days after posting. Rewards go to the trainer you `/link`ed. "
            "Only your own videos; bought views or reposted clips don't count.")


@bot.tree.command(name="bounty", description="Send in your TikTok video for the Windstock video bounty")
@app_commands.describe(link="Your TikTok video's link (leave empty to see the rules)")
@app_commands.guild_only()
async def bounty(interaction: discord.Interaction, link: str = ""):
    say = interaction.response.send_message
    if not link.strip():
        return await say(bounty_rules(), ephemeral=True)
    if not BOUNTY_OPEN:
        return await say("The video bounty isn't taking new videos right now.", ephemeral=True)
    if not SITE_GAME_KEY or not bot.bounty_channel:
        return await say("The video bounty isn't set up yet. Ask a server manager.", ephemeral=True)
    url = tiktok_link(link)
    if not url:
        return await say("That isn't a TikTok video link. Copy it from TikTok's Share button.", ephemeral=True)
    if any(e["url"].lower() == url.lower() for e in bot.bounties.values()):
        return await say("That video has already been sent in.", ephemeral=True)
    me = await bot.trainer_of(interaction.user.id)
    if not me:
        return await say(not_linked() + " Rewards go to your linked trainer.", ephemeral=True)
    mine = [e for e in bot.bounties.values() if str(e["u"]) == str(interaction.user.id)
            and time.time() - e["at"] < 24 * 3600]
    if len(mine) >= 3:
        return await say("That's 3 videos today. Send more tomorrow!", ephemeral=True)
    bid = str(max([int(k) for k in bot.bounties] or [0]) + 1)
    e = {"u": str(interaction.user.id), "url": url, "login": me["login"], "name": me.get("name") or me["login"],
         "at": time.time(), "paid": [], "state": "open"}
    try:
        await bot.get_partial_messageable(bot.bounty_channel).send(embed=bounty_card(bid, e), view=bounty_view(bid, e))
    except discord.HTTPException as err:
        _log(f"bounty entry not posted: {err!r}")
        return await say("Couldn't send that in just now. Try again later.", ephemeral=True)
    bot.bounties[bid] = e
    bot.save()
    _log(f"bounty {bid} from {me['login']}: {url}")
    await say(f"Got it! Entry #{bid} for **{clean(e['name'])}** is in. The team checks views about 7 days "
              "after you posted; you'll get a DM when it pays out.", ephemeral=True)


@bot.tree.command(name="bountyreview", description="Send new video bounty entries to this channel for review")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def bountyreview(interaction: discord.Interaction):
    try:
        await bot.get_partial_messageable(interaction.channel_id).send(
            "New video bounty entries will be posted here for review.")
    except discord.HTTPException:
        return await interaction.response.send_message(
            "I can't post in this channel. Give WindBot **View Channel**, **Send Messages** "
            "and **Embed Links** here, then run this again.", ephemeral=True)
    bot.bounty_channel = interaction.channel_id
    bot.save()
    await interaction.response.send_message("Done. Keep this channel managers-only.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("windbot: DISCORD_TOKEN is not set (put it in /etc/windbot.env)")
    bot.run(TOKEN, log_handler=None)
