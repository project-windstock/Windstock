package com.zettabridge.launcher;

import android.content.Intent;
import android.content.res.AssetManager;
import android.content.res.Resources;
import android.content.res.XmlResourceParser;

import org.xmlpull.v1.XmlPullParser;
import org.xmlpull.v1.XmlPullParserException;

import java.io.IOException;

/**
 * Finds the MAIN/LAUNCHER activity of an APK that is not installed. PackageManager's archive
 * parsing drops intent filters, so the binary manifest is read through the public
 * AssetManager.openXmlResourceParser, trying each asset cookie until the package matches
 * (cookie 1 is usually framework-res, whose manifest must be skipped).
 */
final class ManifestReader {
    private static final String ANDROID_NS = "http://schemas.android.com/apk/res/android";
    private static final int MAX_COOKIES = 64;

    private ManifestReader() {}

    static String findLauncherActivity(Resources res, String packageName) {
        AssetManager assets = res.getAssets();
        for (int cookie = 1; cookie <= MAX_COOKIES; cookie++) {
            XmlResourceParser parser;
            try {
                parser = assets.openXmlResourceParser(cookie, "AndroidManifest.xml");
            } catch (IOException | RuntimeException e) {
                continue;
            }
            try {
                Result result = scan(parser, packageName);
                if (result.matched) return result.launcher;
            } catch (XmlPullParserException | IOException e) {
                // not a readable manifest for this cookie
            } finally {
                parser.close();
            }
        }
        return null;
    }

    private static final class Result {
        boolean matched;
        String launcher;
    }

    private static Result scan(XmlResourceParser p, String packageName) throws XmlPullParserException, IOException {
        Result result = new Result();
        String component = null;
        boolean inFilter = false;
        boolean main = false;
        boolean launcher = false;
        for (int event = p.getEventType(); event != XmlPullParser.END_DOCUMENT; event = p.next()) {
            if (event == XmlPullParser.START_TAG) {
                String tag = p.getName();
                if (tag.equals("manifest")) {
                    if (!packageName.equals(p.getAttributeValue(null, "package"))) return result;
                    result.matched = true;
                } else if (tag.equals("activity")) {
                    component = resolve(packageName, p.getAttributeValue(ANDROID_NS, "name"));
                } else if (tag.equals("activity-alias")) {
                    component = resolve(packageName, p.getAttributeValue(ANDROID_NS, "targetActivity"));
                } else if (tag.equals("intent-filter") && component != null) {
                    inFilter = true;
                    main = false;
                    launcher = false;
                } else if (inFilter && tag.equals("action")) {
                    main |= Intent.ACTION_MAIN.equals(p.getAttributeValue(ANDROID_NS, "name"));
                } else if (inFilter && tag.equals("category")) {
                    launcher |= Intent.CATEGORY_LAUNCHER.equals(p.getAttributeValue(ANDROID_NS, "name"));
                }
            } else if (event == XmlPullParser.END_TAG) {
                String tag = p.getName();
                if (tag.equals("intent-filter") && inFilter) {
                    inFilter = false;
                    if (main && launcher) {
                        result.launcher = component;
                        return result;
                    }
                } else if (tag.equals("activity") || tag.equals("activity-alias")) {
                    component = null;
                }
            }
        }
        return result;
    }

    private static String resolve(String packageName, String name) {
        if (name == null) return null;
        if (name.startsWith(".")) return packageName + name;
        if (!name.contains(".")) return packageName + "." + name;
        return name;
    }
}
