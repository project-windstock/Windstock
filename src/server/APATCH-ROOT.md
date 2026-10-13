# A rooted phone: CA in the system store + host pins (APatch / Magisk)

For phones that are already rooted this replaces the user-certificate step of
[`GNIREHTET.md`](GNIREHTET.md) and removes DNS from the equation entirely. It was
written and verified on a Pixel 8 running Android 16 with **APatch** on
2026-10-13; the same module works on Magisk.

Everything is systemless: a module in `/data/adb/modules/windstock-ca` and two
files of scratch data under `/data/adb/`. Nothing on a real partition is written,
and deleting the module folder and rebooting undoes all of it.

## Why this is needed

Three separate failures look like "the server is broken" but are not:

1. **The client does not trust our CA.** On Android 14+ Java/conscrypt validates
   against `/apex/com.android.conscrypt/cacerts`, but the 2016 client's own
   BoringSSL reads `/system/etc/security/cacerts` as CApath. Whichever store you
   miss, the client closes the TLS handshake and the server logs
   `[tls] handshake FAILED: SSLEOFError` while the game says *unable to
   authenticate*.
2. **A module `system/` tree is not enough.** APatch runs a module's
   `post-fs-data.sh` but does **not** overlay its `system/` directory — verified:
   the cert never appeared in `/system/etc/security/cacerts` and no overlay mount
   existed. Both stores therefore have to be bind-mounted explicitly, which is
   what the module script does.
3. **DNS races.** `sso.pokemon.com` is the one host a phone's network keeps
   resolving to the real Niantic servers — a second resolver, a cached record or
   an encrypted-DNS mode can win the race, and the only symptom is
   *unable to authenticate* with **nothing** in the server log (the same failure
   `src/tools/patch_sso_host.py` describes). Pinning the hosts makes the answer
   independent of DNS.

## Install

```bash
# 1. once, on the PC: the CA the server will present
cd src/server
py windstock/tools/gen_certs.py          # writes certs/ca.crt (+ private keys, ignored by git)

# 2. put the CA in both system stores and pin the hosts (root on the phone)
cd root
MSYS_NO_PATHCONV=1 ./deploy.sh           # Windows/Git Bash; plain ./deploy.sh elsewhere

# 3. the mounts must exist before Zygote starts
adb reboot

# 4. prove it, in app namespaces, before blaming the server
./verify.sh
```

`verify.sh` must print `apex=OURS sys=OURS` for app processes (including
`com.nianticlabs.pokemongo`) and list the pins. Anything else means the module
did not take effect.

Then start the server as usual:

```bash
py src/server/run.py --gnirehtet     # USB mode; or: cd src/server && py run.py
```

## Re-run `deploy.sh` when…

* **the PC's LAN IP changes** — the `/system/etc/hosts` pins are an address, so
  re-run `deploy.sh` from the new network. The certificate mounts are
  address-independent and never need redoing.
* you regenerate the CA (deleting `certs/ca.*` makes a new root; a new root means
  every phone must be re-deployed).

## Uninstall

```sh
su -c "rm -rf /data/adb/modules/windstock-ca /data/adb/windstock-hosts \
       /data/adb/windstock-apex-store /data/adb/windstock-sys-store"
adb reboot
```

## Gotchas this setup was debugged through

| Symptom | Cause | Fix |
|---|---|---|
| `unable to authenticate`, nothing in the server log | client resolved the host elsewhere (DNS race) | the hosts pins; if you changed the PC's IP, re-run `deploy.sh` |
| TLS handshake never completes; server logs `SSLEOFError` | CA missing from *one* of the two stores | re-run `deploy.sh`, reboot, `verify.sh` |
| The whole phone's networking dies after unplugging/replugging the USB cable, while DNS keeps working | gnirehtet leaves the TCP relay half-dead | restart the server (`Ctrl-C`, then run it again); it re-issues `gnirehtet start` |
| Game window frozen at ~0 FPS, phone idle, no crash | the guest's Unity main thread parked on a futex inside the bridge, having been launched into a low-memory device | see below |

### When the game freezes instead of running

Measured symptoms: Unity's main thread parked in
`syscall -> libzbridge.so` (a guest futex), the game's `SurfaceView` presenting
**zero** frames, 440 MB free RAM with 3.2 GB of swap in use, no thermal
throttling, and one idle `ESTABLISHED` socket to the server whose last request
had already been answered. That is a hang, not slowness.

What cleared it:

```sh
adb shell am kill-all                    # reclaim RAM from cached background apps
adb shell am force-stop com.chucny.translator.bridge
# relaunch the plugin exactly as the launcher does (the data URI and the
# clear-task flag matter: Android matches a task against its own intent)
adb shell am start -a android.intent.action.MAIN --activity-clear-task \
  -n com.chucny.translator.bridge/com.zettabridge.launcher.GuestLaunchActivity \
  -d "translator://guest/com.nianticlabs.pokemongo.classic" \
  --es com.chucny.translator.bridge.PACKAGE com.nianticlabs.pokemongo.classic
```

After that the game logged in and rendered normally. Freeing RAM first is not
cosmetic: the guest had been launched into a phone with ~440 MB free, and the
hang did not recur once the cached apps were gone.

Diagnostics used, if it comes back:

```sh
# real frame rate of the game's surface (0 frames = a hang, not a slow frame)
adb shell su -c 'dumpsys SurfaceFlinger --list | grep -i surfaceview'
adb shell su -c "dumpsys SurfaceFlinger --latency '<layer name>'"

# what the guest's threads are actually doing
adb shell su -c 'debuggerd -b <guest pid>'        # look for "UnityMain"
adb shell su -c 'cat /proc/<tid>/wchan'           # futex_wait / sk_wait_data / ...
```
