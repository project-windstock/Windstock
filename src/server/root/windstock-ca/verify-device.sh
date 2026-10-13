#!/system/bin/sh
# On-device half of ../verify.sh. Answers three questions, because each one has
# been the difference between "the game logs in" and "unable to authenticate":
#
#   1. Is the CA in both live trust stores?
#   2. Do APP processes -- not just root -- see those stores?
#   3. Are the Niantic hosts pinned in the file the resolver actually reads?
#
# An f2fs mount of our copy over a store directory means OURS; the stock store is
# ext4 straight from the conscrypt APEX image.
APEX=/apex/com.android.conscrypt/cacerts
SYS=/system/etc/security/cacerts
HOSTS=/system/etc/hosts

state() {   # $1 pid, $2 path -> OURS / stock / no-line
    line=$(grep " $2 " /proc/$1/mountinfo 2>/dev/null | head -1)
    case "$line" in
        *f2fs*) echo OURS ;;
        "")     echo no-line ;;
        *)      echo stock ;;
    esac
}

echo "=== stores as root"
echo "  apex=$(state $$ $APEX)  sys=$(state $$ $SYS)"
echo "  apex entries: $(ls $APEX 2>/dev/null | wc -l)   sys entries: $(ls $SYS 2>/dev/null | wc -l)"

echo "=== stores as apps see them"
for p in $(ps -A -o PID,NAME | grep -Ei 'zygote|systemui|pokemongo|gnirehtet|corp\.bridge' | awk '{print $1}'); do
    echo "  pid=$p $(cat /proc/$p/comm 2>/dev/null) apex=$(state $p $APEX) sys=$(state $p $SYS)"
done

echo "=== hosts pins"
grep -c "Windstock private server" $HOSTS 2>/dev/null | sed 's/^/  windstock block: /'
grep -E "nianticlabs|pokemon\.com|zendesk" $HOSTS 2>/dev/null | sed 's/^/  /'
