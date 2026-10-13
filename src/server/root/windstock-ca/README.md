# Windstock CA + host redirect (APatch / Magisk module)

Puts the Windstock CA into the **system** trust stores of a rooted phone and pins
the Niantic/PTC hostnames at the Windstock PC, so the game connects and trusts the
server without any APK patching for TLS and without relying on DNS.

Install it with `../deploy.sh` (it fills in the certificate name and the PC's IP);
`../README` explains when you need it and what to re-run.

| Part | Role |
|---|---|
| `module.prop` | Module metadata. Delete the module and reboot to undo everything. |
| `cert/<hash>.0` | The CA, named the way Android wants it (`openssl x509 -subject_hash_old`). Written by `deploy.sh`; the private key never leaves `certs/`. |
| `post-fs-data.sh` | Copy of both stores + our CA, each bind-mounted over the live one, plus the pinned `/system/etc/hosts`. |

Two stores are mounted because they are read by different code: on Android 14+
Java/conscrypt validates against `/apex/com.android.conscrypt/cacerts`, while a
native TLS library — like the 2016 client's own BoringSSL — reads
`/system/etc/security/cacerts` as CApath. Mounting only one of them leaves the
other path untrusted, and a rejected certificate looks like a client that closes
the connection mid-handshake (`[tls] handshake FAILED: SSLEOFError` server-side)
rather than like a clean error.
