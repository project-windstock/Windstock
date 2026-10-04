# `--gnirehtet` — play over the USB cable (no admin, no root, no DNS app)

This page assumes you have never used a terminal command before. If a word looks
scary, the **Plain English** box right after it explains it.

Running `py run.py --gnirehtet` gives you the whole setup: the game server, the
DNS redirect, and the phone's connection — all over the USB cable.

---

## The short version

```
py src/scripts/INSTALL_GNIREHTET.py     # one time: gets gnirehtet
py run.py --gnirehtet                    # every time: start everything
```

Then on the phone: **accept the VPN prompt**, **install the CA** (one time), and
start Pokémon GO.

---

## Why this needs no DNS app

> **DNS** is the phone's address book: it turns `pgirelease.nianticlabs.com` into
> a number (an IP address) the phone can connect to.

The normal Wi-Fi method works by making your **phone's Wi-Fi settings** use this
PC as their DNS server. On a school PC the Wi-Fi/firewall is locked, so that route
is blocked.

`gnirehtet` does it differently. It sends **all** of the phone's internet down the
USB cable to this PC — and it lets us tell the phone, as it connects, **"use this
PC as your DNS server"** (`gnirehtet ... -d <PC-IP>`). So:

- the phone asks **this PC** for `pgorelease.nianticlabs.com`,
- the server answers with this PC's own address,
- the game's secure traffic tunnels back down the cable to the game server.

Nothing is installed with Administrator rights, the phone is **not** rooted, and
you do **not** need any DNS app on the phone.

---

## What you need

- A Windows PC (this one) with the Windstock server folder.
- A USB cable and your Android phone.
- The **CA certificate** the server makes (`certs/ca.crt`) — installed once.
- **gnirehtet** — got for you by the script below.

---

## Step 1 — Install gnirehtet (one time)

In a terminal, from the repository root, run:

```
py src/scripts/INSTALL_GNIREHTET.py
```

This downloads the official gnirehtet release, **checks its SHA-256 checksum**,
and unpacks it into `C:\Users\<you>\gnirehtet`. No Administrator prompt appears.

You should see something like:

```
Installing gnirehtet v2.5.1 into C:\Users\you\gnirehtet
  downloading https://github.com/Genymobile/gnirehtet/releases/...
  checksum OK
  unpacking into C:\Users\you\gnirehtet

Done.
  executable : C:\Users\you\gnirehtet\gnirehtet.exe
```

> On Linux the script installs to `~/.local/share/gnirehtet` instead. Same
> command, same result.

A copy of the phone app (`gnirehtet.apk`) also ships in this folder
(`src/server/gnirehtet.apk`), so the launcher always has one to install -- even
if the gnirehtet install has no APK beside `gnirehtet.exe`. You do not need to
do anything with it by hand.

---

## Step 2 — Prepare your phone (one time)

1. On the phone, open **Settings → About phone**.
2. Tap **Build number** seven times. A message says *"You are now a developer"*.
3. Go to **Settings → System → Developer options**.
4. Turn on **USB debugging**.
5. Plug the phone into the PC.
6. When the phone asks *"Allow USB debugging?"*, tap **Allow**.

---

## Step 3 — Start everything

```
py run.py --gnirehtet
```

You should see a Windstock banner, then lines starting with `[gnirehtet]`, then
*"PoGO private server listening on https://…:443"*.

**The first time**, your phone pops up a question asking to let gnirehtet create a
VPN connection. Tap **OK**. A small **key icon** appears in the phone's status bar
— that means it is working.

---

## Step 4 — Trust the game's certificate (one time)

The launcher copies the CA to the phone. Install it once:

1. On the phone, open **Settings → Security → Encryption & credentials**.
2. Choose **Install a certificate → CA certificate**.
3. Pick `windstock-ca.crt` (it is in the phone's **Download** folder).
4. Accept the *"network may be monitored"* warning.

Then start **Pokémon GO** and log in with any username and password.

---

## Keep one network ON (do NOT use Airplane mode)

An app decides whether it has internet by asking **Android** — it does not test a
packet itself. Android only reports "internet" for a VPN when there is an
**underlying network that has internet**. In Airplane mode there is none, so games
show **"no internet connectivity"** even though the tunnel is working fine.

So:

- **Leave Wi-Fi connected** (to any network with real internet) **or leave mobile
  data on.** This only supplies that "I have internet" signal.
- **Do NOT switch on Airplane mode.**
- The tunnel still carries the game's traffic and DNS to this PC, so the game
  never reaches the real Niantic servers.

Also set **Private DNS** to **Off**
(Settings → Network & internet → Private DNS → Off). Private DNS would bypass our
redirect entirely.

**Symptom to recognise:** the phone's browser works but the game says *"no
internet"* — that is exactly this signal problem. Make sure a network is connected.

## Stopping it

Press **Ctrl-C** in the terminal. Windstock tells the phone to stop and shuts the
relay server down for you.

---

## Troubleshooting

| What you see | What it means / what to do |
|---|---|
| `[gnirehtet] not found` | Run `py src/scripts/INSTALL_GNIREHTET.py`. If you placed gnirehtet somewhere else, set the `GNIREHTET` environment variable to the full path of `gnirehtet.exe`. |
| `[gnirehtet] no authorized device` | Plug the cable in, turn on USB debugging, tap **Allow**. |
| `[gnirehtet] phone is 'unauthorized'` | Unplug, replug, tap **Allow** again. |
| Key icon missing on the phone | The app didn't start or you declined the VPN prompt. Re-run the command and tap **OK**. |
| Game can't connect / stuck on load | Make sure the **PC** has internet, and that the CA was installed as a *user* certificate. Re-run and try again. |
| Internet dead on the phone | Run `gnirehtet tunnel` from the gnirehtet folder to reset the connection, then start again. |

---

## Good to know

- gnirehtet is no longer actively maintained by its author, but it still works.
- It relays **IPv4 TCP and UDP** (including DNS). It does **not** do IPv6.
- Nothing here needs Administrator on the PC or root on the phone.
- The launcher passes `-d <this PC's IP>` so the phone's DNS points at us; keep
  the PC on the same address the launcher prints if you want to be sure.
- The Wi-Fi DNS method is still the default and is unchanged; `--gnirehtet` is
  purely an alternative for locked-down PCs.
