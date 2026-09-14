# ⚡ Windstock — a Pokémon GO 0.29-35 Private Server

> A from-scratch, private server for the original **Pokémon GO 0.29.0** (July 2016).
> 
> **The corporate suits in Saudi Arabia, Mohammed bin Salman and sovereign wealth funds thought they could buy our nostalgia, lock it behind a paywall, and strip away the soul of what made July 2016 magic.**
> 
> When the mega-corporations and dictators took the reins of modern mobile gaming, they turned a global cultural phenomenon into a localized cash extraction machine. 
>
> **We didn't accept it. We took it back. And succeeded.**
>
> Windstock is a completely independent, built-from-scratch private server that liberates the original Pokémon GO client from corporate greed and centralized control. By reverse-> engineering the exact protocol of version 0.29.0, we have stripped away the tracking, the aggressive monetization, and the external interference. This project returns the game > entirely to the community. Your data, your server, your terms.

Welcome back to the wilderness of 2016—free from the boardrooms, free from the gatekeepers.


| | |
|---|---|
| 🎮 **Client** | Pokémon GO 0.29.0 (July 2016) · 0.35 also supported |
| 🌐 **Network** | MITM, LAN |
| 🧪 **Status** | Playable — catching, PokéStops, Gyms, every single request supported|
| 🐍 **Runtime** | Python 3 |

---

## 📚 Table of contents

- [✨ What works](#-what-works)
- [🆕 Recent changes](#-recent-changes)
- [📱 Platforms](#-platforms)
- [⚠️ Known limitations](#️-known-limitations)
- [🧠 How it works](#-how-it-works)
- [🚀 Running it](#-running-it)
- [🛠️ Toolbox](#️-toolbox)
- [🔬 Reverse engineering the client](#-reverse-engineering-the-client)
- [🗂️ Repository layout](#️-repository-layout)
- [⚖️ Legal / disclaimer](#️-legal--disclaimer)
- [🙌 Credits](#-credits)

---

## ✨ What works

- 🔑 **Login** — any username and password.
- 🧬 **Full boot handshake** — the exact 0.29 RPC sequence (redirect → player → remote config →
  settings → asset digest → item templates → map), with field numbers checked against the live client.
- 📡 **Every request the client can send gets a real answer.** All 71 request types in the 0.29
  client were checked against its own metadata; every one that has a message layout in the client
  is handled (gym recall, incense/lure encounters, badges, contact settings, and more).
- 🗺️ **Live map at your real GPS** — wild Pokémon, PokéStops and Gyms placed around you.
- 🎯 **Catching** — encounters, throw scoring (Nice/Great/Excellent, curveballs), Razz Berries,
  break-outs and flees, capture odds — all decided by the server and **saved** per account.
- 🏛️ **PokéStops** — shown on the map, named, spinnable for items and XP on a cooldown; they also
  drop eggs.
- ⚔️ **Gyms** — deploy defenders, **train** friendly gyms to raise prestige and level, **battle** to
  take enemy gyms, real 2016 **type-matchup damage** (so HP bars follow the fight), **flee**
  mid-battle, the Shop **defender bonus** (coins and stardust for held gyms), a prestige/level
  system, coin payouts when a defender comes home, and a raid mode.
- 📈 **Progression** — teams (Mystic/Valor/Instinct), evolve / power up / transfer / favorite /
  nickname, the Pokédex, and medals scored against the real 2016 targets.
- 🎉 **Level-ups** — the level-up screen plays **the moment you level up**, and each level's
  rewards are paid exactly once.
- 🥚 **Eggs and incubators** — eggs hatch by the distance you actually walk. Every trainer has one
  unlimited incubator; basic incubators have three uses, like 2016.
- 🛒 **In-game Shop** — buy items with the coins you earn.
- 📜 **Real 2016 game master** — a converter rebuilds the period-correct item-template database
  (151 Kanto Pokémon, moves, items, cameras) into the format the client accepts.
- 🧊 **3D Pokémon models render** *when you supply genuine 2016 asset bundles* — the full pipeline is
  implemented (`GET_ASSET_DIGEST` → `GET_DOWNLOAD_URLS` → serve the encrypted bundle). Without them
  the map, catching, stops and gyms all still work; creatures fall back to the client's 2D icons.
- 🧭 **World Manager** — a local web UI (`http://localhost:8080`) to place and manage PokéStops and
  Gyms, download real POIs, run events, and inspect server state.
- 💬 **Help Center** and a public status/site page.
- 📦 **One-file launcher** and an optional standalone `.exe` (no Python needed on the host).

## 🆕 Recent changes

- **Level-up screen shows up during play.** Inventory replies now echo the client's own timestamp
  back, so each poll is a real update and the client fires the level-up itself.
- **Level rewards can't be paid twice.** The check-and-claim is now a single atomic step.
- **Incubators work.** Shop and level-reward incubators are real incubators with three uses, and
  any stuck in a bag are converted automatically on next login.
- **Used-up items update on screen.** Items that reach 0 are sent as count 0, so the bag never
  shows a stale number.
- **All remaining requests answered**, and `DOWNLOAD_SETTINGS` now fills in every `GlobalSettings`
  field the client reads (including `level_settings`).
- **Deleting a save resets the player.** Removing `data/saves/<name>.json` while the server is
  running now really starts that trainer over, instead of writing their old progress back.

## 📱 Platforms

- 🍎 **iPhone (0.29 and 0.35):** works with the stock client — no patching — reached over Tailscale.
- 🤖 **Android (0.29):** needs a one-time static metadata patch to the APK (see
  [Reverse engineering](#-reverse-engineering-the-client)); no root, user-installed CA.
- 🤖 **Android (0.35):** also supported, through an APK patcher

Step-by-step device guides live in `src/server/DEVICE_SETUP.md`, `src/server/RUN.md` and
`src/server/VPN.md`.

## ⚠️ Known limitations

- **Gym defender counter-attacks don't animate**, and enemy-gym battles are shown as *training*
  battles. The 0.29 client simulates gym combat **on the phone** and ignores the battle actions the
  server sends, so the outcome, HP and prestige are ours to control — but the defender's attack
  animation is up to the client. The fights are real; the animation can't change without
  disassembling the client.
- **3D models need genuine 2016 bundles** you can DM (direct message) me on Discord at `@chucny` or contact me at `koppispoke@gmail.com` if you need assets from public archives.
## 🧠 How it works


| Component | File | Role |
|---|---|---|
| 🌐 DNS redirector | `src/server/dns_redirect.py` | Points the Niantic/PTC hosts at this PC; forwards everything else |
| 🔐 HTTPS server | `src/server/server.py` | One TLS listener, routes by `Host` header |
| 🔑 Fake PTC SSO | `src/server/sso.py` | Accepts any credentials, puts the username in the token |
| 🌉 0.35 SSO bridge | `src/server/sso_bridge.py` | Plain-HTTP login door for the 0.35 client |
| 📨 RPC handler | `src/server/rpc.py` | The game protocol: boot handshake, map, catching, forts, gyms, shop |
| 🧱 Response builders | `src/server/protocol.py` | Every message sent to the client (the heart of the project) |
| 🧬 Protobuf codec | `src/server/pb.py` | Hand-written protobuf reader/writer (no `protoc`) |
| 💾 World state | `src/server/world.py` | Per-account inventory, Pokémon and XP, plus shared gyms, saved to disk |
| 🎛️ Game data | `src/server/gamedata.py`, `settings.py` | Stats, moves, types; hot-reloaded tuning in `settings.json` |
| 🛍️ Shop / Help / Site | `src/server/shop.py`, `helpcenter.py`, `website.py` | In-game store, support pages, status site |
| 🧭 World Manager | `src/server/webui.py`, `admin.py` | Local web UI for stops, gyms, events and POIs |
| 🚀 Launcher | `src/server/__main__.py` | Runs the DNS redirector and game server (plus bridges) in one process |
| 🪟 Server window | `src/server/server_gui.py`, `pogo_manager.py` | Desktop status/control windows |
| 🧪 Game master converter | `src/tools/convert_gm.py` | Rebuilds the 2016 GAME_MASTER into 0.29 item templates |
| 🔎 Metadata reader | `src/tools/metadata_fields.py` | Reads exact protobuf field numbers straight out of the client |
| 🩹 APK patcher | `src/patcher/patcher.py` | GUI that strips certificate pinning from an APK |
| 📦 Installer | `src/scripts/DOWNLOAD.py` | Cross-platform dependency installer |
| 🗄️ Backups | `database.py` | MariaDB / SQLite backup and restore of server data |

TLS uses a local CA that you install on the phone. Field numbers were checked against the live
client, and several differ from the public POGOProtos (e.g. `RequestEnvelope.requests` is #4, not
#3; `PokemonData.id` is a `fixed64`, not the `int32` the protos claim).

## 🚀 Running it

**Prerequisites:** Python 3, and OpenSSL (for generating certificates). Then install the Python
dependencies with the bundled installer — it handles Linux, Windows and macOS:

```bash
py src/scripts/DOWNLOAD.py
```
Or alternatively, install dependencies manually (recommended)

On Linux it installs system-wide via `sudo` by default; pass `--user`, `--venv` or `--no-sudo` to
change that. `--check`, `--list` and `--dry-run` are available too.

**1 · Generate the certificates (once):**

```bash
cd src/server
py gen_certs.py
```

**2 · Start the server:**

```bash
py __main__.py
```

`__main__.py` detects this PC's LAN IP and starts the DNS redirector and the game server. Pass an
IP to use a specific one, such as a Tailscale address:

```bash
py __main__.py 100.x.y.z
```

You can also run the directory directly — `py src/server` — which executes its `__main__.py`.
The World Manager opens on `http://127.0.0.1:8080`.

**3 · Build a standalone exe (optional):**

```bash
py -m PyInstaller "Start-Pokemon-GO-Server.spec" --distpath ../../RELEASE --noconfirm --clean
```

### 📲 On the phone (Android, no root)

1. Install your patched 0.29 APK, and install the CA (`src/server/certs/ca.crt`) as a **user**
   certificate.
2. In Wi-Fi settings, set **DNS to the IP the launcher prints** (leave DNS 2 blank).
3. For a stable map indoors, use a **mock-GPS app** set as the mock-location app, with Location
   mode set to **GPS only** (not High accuracy).
4. Start the game and log in with any name.

### 🎚️ Tuning

Spawn rates, catch odds, gym payouts, the defender bonus and more live in
`src/server/data/settings.json`, which is written on first run with a comment for every value and
**hot-reloads** as you edit it. To reset a player for testing, delete their
`src/server/data/saves/<name>.json` — this works even while the server is running.

## 🛠️ Toolbox

Beyond the server itself, the repo ships the tooling the project was built with.

### 📦 `src/scripts/DOWNLOAD.py` — dependency installer

One command, every OS. Finds the right Python, bootstraps pip if needed, and installs the packages
the server and tools need. Groups: `core`, `db`, `poi`, `tools`, `patcher`, `all`.

```bash
py src/scripts/DOWNLOAD.py --group core   # server runtime only
py src/scripts/DOWNLOAD.py --check        # report what's missing, install nothing
```

### 🩹 `src/patcher/patcher.py` — APK patcher

A desktop GUI (CustomTkinter) that runs `npx apk-mitm` over an APK and strips **certificate
pinning**, so the phone will trust your local CA. Requires **Node.js** (for `npx`) and the
`patcher` dependency group.

```bash
py src/scripts/DOWNLOAD.py --group patcher
py src/patcher/patcher.py
```

### 🗄️ `database.py` — backups

Backs up `settings.json`, `places.json` and the whole `saves/` folder into a single `files` table,
and restores them. Two backends: **MariaDB/MySQL** for a shared server, or **SQLite** as a
zero-install fallback. Credentials come from `config.py` (or the `DB_*` environment variables).

```bash
py database.py            # interactive console
py database.py backup_all # one-shot
```

## 🔬 Reverse engineering the client

Reverse engineering the client took a long time, and thanks to `chucny` and `bracky-dev`, it has now been done up to 100%. The server supports every single request from client and handles them properly.

## 🗂️ Repository layout

```
src/server/    the server (Python) + docs + deploy guides
src/tools/     game-master conversion + reverse-engineering scripts
src/patcher/   APK patcher GUI (certificate-pinning removal)
src/scripts/   helper scripts (dependency installer)
database.py    MariaDB / SQLite backup + restore
config.py      database credentials for database.py
README.md      this file
```

The whole repository root is tracked. `.gitignore` still excludes secrets and copyrighted material:
the APK/IPA, Niantic's asset bundles and game-master binary, extracted app data, TLS private keys,
player saves, runtime data and logs, packaged builds, and third-party tools.

## ⚖️ Legal / disclaimer

This is an independent, educational reverse-engineering project for **personal, offline** use with
a client you already own. It includes **none** of Niantic's copyrighted code, assets, or data — only
original interoperability code. The `/src/server/game_master.bin` is a custom-made file from a text file and a script, not an original protobuf dump.

## 🙌 Credits

- [AeonLucid/POGOProtos](https://github.com/AeonLucid/POGOProtos) — protocol definitions (a
  reference; several field numbers were re-checked against the live 0.29 client here)
- [rastapasta/pokemon-go-mitm](https://github.com/rastapasta/pokemon-go-mitm) — map-object field layout
- [maierfelix/POGOServer](https://github.com/maierfelix/POGOServer) — a known-good server used to
  cross-check response layouts (timestamps, GlobalSettings, defender bonus)
- [apk-mitm](https://github.com/shroudedcode/apk-mitm) — the patcher's certificate-pinning removal
- The community 2016 GAME_MASTER dump

## ⌨️ Developers
- `Chucny` - main developer and owner
- `bracky-dev` main developer and owner

## ⚖️ License
`This project is licensed under the GPL 3.0 License. See LICENSE file for details.`
