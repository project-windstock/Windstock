#!/bin/bash
# Builds "Windstock Regions.app" next to this script: double-click it to open
# the region page (region_app.py). The app just starts the Python server in the
# background and opens the browser; Quit on the page stops it.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/Windstock Regions.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Windstock Regions</string>
  <key>CFBundleIdentifier</key><string>org.windstock.regions</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/run" <<RUN
#!/bin/bash
cd "$HERE"
exec /usr/bin/python3 "$HERE/region_app.py" >> "$HERE/_osm_cache/region_app.log" 2>&1
RUN
chmod +x "$APP/Contents/MacOS/run"
mkdir -p "$HERE/_osm_cache"
echo "built $APP"
