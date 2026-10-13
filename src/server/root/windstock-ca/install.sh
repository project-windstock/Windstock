#!/system/bin/sh
# Installs the staged module into /data/adb/modules/windstock-ca and stages the
# pinned hosts file. Runs ON THE PHONE as root; deploy.sh pushes the files here
# and calls this, so none of the root-side work lives in nested shell quoting.
set -eu

STAGE=/data/local/tmp/windstock-mod
MOD=/data/adb/modules/windstock-ca

[ -d "$STAGE" ] || { echo "nothing staged at $STAGE" >&2; exit 1; }
ls "$STAGE"/cert/*.0 >/dev/null 2>&1 || { echo "no certificate staged" >&2; exit 1; }

rm -rf "$MOD"
mkdir -p "$MOD"
cp -r "$STAGE"/. "$MOD"/
rm -f "$MOD/hosts"                      # only the /data copy is used at boot
chmod 755 "$MOD/post-fs-data.sh"
chmod 644 "$MOD/module.prop"
chmod 644 "$MOD"/cert/*.0
chown -R 0:0 "$MOD"

# The module script copies the CA into stores whose files are labeled
# system_security_cacerts_file; give it the same type up front so the chcon in
# the script is a no-op rather than a fix-up.
chcon u:object_r:system_security_cacerts_file:s0 "$MOD"/cert/*.0 2>/dev/null || true

# The hosts pin is a bind mount over /system/etc/hosts, so it keeps the label of
# the file it shadows -- otherwise netd's reader is denied by SELinux.
cp "$STAGE/hosts" /data/adb/windstock-hosts
chmod 644 /data/adb/windstock-hosts
chown 0:0 /data/adb/windstock-hosts
LBL=$(ls -Z /system/etc/hosts 2>/dev/null | awk '{print $1}')
chcon "${LBL:-u:object_r:system_file:s0}" /data/adb/windstock-hosts 2>/dev/null || true

echo "=== installed"
ls -lZ /data/adb/windstock-hosts
ls -lR "$MOD"
echo "=== pins"
tail -8 /data/adb/windstock-hosts
echo "=== note: no module system/ tree -- post-fs-data.sh makes the mounts itself"
