#!/bin/sh
# Drives the app's own Light/Dark toggle and reports the colours it painted.
set -e
cd /c/Users/ottojak/Downloads/work/windstock-site/project2

dump() {
  MSYS_NO_PATHCONV=1 adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  MSYS_NO_PATHCONV=1 adb shell cat /sdcard/ui.xml > /tmp/screen.xml
}

# The clickable row that contains a given label; the label's own bounds are inside it.
row_centre() {
  tr '<' '\n<' < /tmp/screen.xml | grep "text=\"$1\"" | head -1 \
    | grep -oE '\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]' | head -1 | sed 's/\]\[/,/' | tr -dc '0-9,'
}


sample() {
  MSYS_NO_PATHCONV=1 adb exec-out screencap -p > shot-theme.png
  py -3 build/sample_ui_colours.py shot-theme.png
}

echo "== fresh start =="
adb shell am force-stop com.chucny.translator.bridge
adb shell am start -n com.chucny.translator.bridge/com.zettabridge.launcher.LibraryActivity >/dev/null 2>&1
sleep 5
adb shell input tap 886 2180   # the Settings section of the bar
sleep 4
for i in 1 2 3 4; do adb shell input swipe 540 700 540 1700 200; sleep 1; done
dump
echo "screen: $(tr '<' '\n<' < /tmp/screen.xml | grep -oE 'text="(Theme|Light|Dark|Settings)"' | tr '\n' ' ')"
echo "stored: $(adb shell run-as com.chucny.translator.bridge cat shared_prefs/launcher-settings.xml 2>/dev/null | grep -o 'skin">[A-Z]*')"
sample

for scheme in Dark Light; do
  dump
  b=$(row_centre "$scheme")
  if [ -z "$b" ]; then echo "!! $scheme row not on screen"; exit 1; fi
  x0=${b%%,*}; rest=${b#*,}; y0=${rest%%,*}; rest=${rest#*,}; x1=${rest%%,*}; y1=${rest##*,}
  echo "== tap $scheme at ($(( (x0 + x1) / 2 )),$(( (y0 + y1) / 2 ))) =="
  adb shell input tap $(( (x0 + x1) / 2 )) $(( (y0 + y1) / 2 ))
  sleep 5
  echo "stored: $(adb shell run-as com.chucny.translator.bridge cat shared_prefs/launcher-settings.xml 2>/dev/null | grep -o 'skin">[A-Z]*')"
  sample
done
