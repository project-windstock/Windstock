#!/system/bin/sh
# Windstock private CA + host redirect, installed systemlessly at boot.
#
# Why this is explicit instead of relying on a module system/ tree:
# APatch (and, on some devices, Magisk) runs module scripts but does NOT overlay
# a module's system/ tree -- on the Pixel 8 / Android 16 box this was written for
# /system/etc/security/cacerts never gained the certificate and no overlay mount
# existed, while this script's own bind mount did work. Every step here is therefore
# a bind mount made before Zygote starts, which is why app processes inherit it.
#
# Nothing on a real partition is ever written: deleting
# /data/adb/modules/windstock-ca and rebooting leaves the phone exactly as it was.
#
#   1. /apex/com.android.conscrypt/cacerts  <- that store + our CA. What modern
#      Java/conscrypt (and so Android's own HTTP stacks) validate against.
#   2. /system/etc/security/cacerts         <- the same, for NATIVE TLS libraries.
#      A 2016-era Unity client uses its own BoringSSL/OpenSSL, which reads this
#      path as CApath and does not consult the APEX store at all.
#   3. /system/etc/hosts                    <- the Niantic/PTC hostnames pinned to
#      the Windstock PC. DNS alone is not enough: a second resolver, a cached
#      record or an encrypted-DNS mode can win the race and send the client to
#      the real Niantic servers, whose only symptom is "unable to authenticate"
#      with nothing whatsoever in the server log (see src/tools/patch_sso_host.py).
#
# Best-effort throughout: no step can hang the boot, and the script always exits 0.
MODULE=/data/adb/modules/windstock-ca
HOSTS=/data/adb/windstock-hosts
APEX=/apex/com.android.conscrypt/cacerts
SYS=/system/etc/security/cacerts
CTX=u:object_r:system_security_cacerts_file:s0

CERT=$(ls "$MODULE"/cert/*.0 2>/dev/null | head -1)
[ -n "$CERT" ] || exit 0

merge() {                       # $1 = live store, $2 = scratch dir
    rm -rf "$2" || return 1
    mkdir -p "$2" || return 1
    cp -a "$1"/. "$2"/ 2>/dev/null
    cp "$CERT" "$2"/ 2>/dev/null
    chmod 644 "$2"/* 2>/dev/null
    chown 0:0 "$2"/* 2>/dev/null
    # The copies live on /data and carry /data's SELinux type; the real store is
    # system_security_cacerts_file. Without this the mount is up but every
    # handshake still fails, because the reader is denied by SELinux.
    chcon "$CTX" "$2" 2>/dev/null
    chcon "$CTX" "$2"/* 2>/dev/null
    return 0
}

i=0
while [ ! -d "$APEX" ] && [ "$i" -lt 30 ]; do
    sleep 1
    i=$((i + 1))
done

if [ -d "$APEX" ]; then
    merge "$APEX" /data/adb/windstock-apex-store && \
        mount --bind /data/adb/windstock-apex-store "$APEX" 2>/dev/null
fi

if [ -d "$SYS" ]; then
    merge "$SYS" /data/adb/windstock-sys-store && \
        mount --bind /data/adb/windstock-sys-store "$SYS" 2>/dev/null
fi

if [ -s "$HOSTS" ]; then
    mount --bind "$HOSTS" /system/etc/hosts 2>/dev/null
fi

exit 0
