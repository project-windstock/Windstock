package com.zettabridge.launcher;

import android.content.Context;
import android.util.Log;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Properties;

/**
 * The one app this launcher build bundles, as a build-time choice rather than a runtime one.
 *
 * <p>The APK travels inside an encrypted container asset (see {@link Payloads} and
 * {@code build/pack_payloads.py}) and is fed to
 * {@link PluginStore#importApk(Context, InputStream, String)} on first use, so a fresh install
 * needs neither the file picker nor an {@code adb push}. Everything downstream is the ordinary
 * import path: the same native-library extraction, the same in-place ELF fixups, the same
 * read-only publication. Nothing differs from a hand import - not even the switches: a translated
 * (32-bit) guest gets whatever the settings screen's new-import defaults say, which means
 * {@code preciseFaults} on unless the user turned it off, because Unity's null checks resume from
 * the faulting instruction and the app is not written to start without it.
 *
 * <p>The descriptor is optional. Without it this is the stock launcher that imports whatever APK the
 * user picks. It lives in the same container as the APK, so nothing on the shipped APK's asset list
 * says what the bundled app is.
 */
final class SeedPlugin {
    /**
     * Where the bundled APK sits inside the container. The name is only an index entry inside the
     * encrypted payload - it never appears in the shipped APK - but it is kept as the plain path so
     * the container stays readable to whatever stages the client (see build/pack_payloads.py).
     */
    private static final String APK_ENTRY = "app/base.apk";
    private static final String TAG = "Translator";

    private SeedPlugin() {}

    /** Parses a descriptor read out of the payload's metadata block. */
    private static Properties parse(String meta) {
        if (meta == null) return null;
        Properties properties = new Properties();
        try {
            properties.load(new ByteArrayInputStream(meta.getBytes(StandardCharsets.UTF_8)));
        } catch (IOException e) {
            return null;
        }
        return properties.getProperty("package") != null ? properties : null;
    }

    /** The bundled app's descriptor, or null when this build bundles no app. */
    static Properties read(Context context) {
        try (Payloads.Container payload = Payloads.open(context, Keys.CLIENT_ASSET)) {
            return parse(payload.meta);
        } catch (IOException e) {
            return null;
        }
    }

    /** The record the bundled app was imported into, or null when it is not installed. */
    static PluginRecord installed(Context context) {
        Properties properties = read(context);
        if (properties == null) return null;
        return PluginRecord.load(new File(PluginStore.root(context), properties.getProperty("package")));
    }

    /**
     * Installs the bundled app when it is not installed yet, returning its record either way (null
     * only when this build bundles no app). Cheap when already installed: it decrypts the the
     * payload's metadata block and stops there. Synchronized because two refreshes can race, and the
     * first call copies and fixes a whole APK, so the caller must keep it off the UI thread.
     */
    static synchronized PluginRecord installIfMissing(Context context) throws IOException {
        try (Payloads.Container payload = Payloads.open(context, Keys.CLIENT_ASSET)) {
            Properties properties = parse(payload.meta);
            if (properties == null) return null;
            String packageName = properties.getProperty("package");
            File installed = new File(PluginStore.root(context), packageName);
            if (!installed.isDirectory()) {
                Payloads.Entry bundled = payload.entry(APK_ENTRY);
                if (bundled == null) throw new IOException("this build carries no bundled client");
                try (InputStream apk = payload.open(bundled)) {
                    PluginStore.importApk(context, apk, properties.getProperty("label", packageName));
                }
            }
            reconcile(context);
            return PluginRecord.load(installed);
        }
    }

    /**
     * Puts the bundled app's name from the descriptor onto its imported record, so the library
     * shows the name this build was asked for instead of the client's own.
     *
     * <p>The package field is what this exists for: the bundled client calls itself
     * {@code com.nianticlabs.pokemongo.classic}, and an earlier build of this wrapper imported it
     * under that name. Reconciling here rather than at import time means an app that is already
     * installed is renamed too, with no re-import and no loss of its data directory.
     *
     * @return true when the record changed, so the caller can redraw the list.
     */
    static synchronized boolean reconcile(Context context) {
        Properties properties = read(context);
        if (properties == null) return false;
        PluginRecord record = PluginRecord.load(
                new File(PluginStore.root(context), properties.getProperty("package")));
        if (record == null) return false;
        String label = properties.getProperty("label");
        String display = properties.getProperty("displayPackage");
        boolean changed = false;
        if (label != null && !label.isEmpty() && !label.equals(record.label)) {
            record.label = label;
            changed = true;
        }
        if (display != null && !display.isEmpty() && !display.equals(record.displayPackage)) {
            record.displayPackage = display;
            changed = true;
        }
        if (!changed) return false;
        try {
            record.save();
        } catch (IOException e) {
            // The name is cosmetic: a record that cannot be rewritten keeps the client's own name.
            Log.w(TAG, "cannot rename the bundled app: " + e);
            return false;
        }
        return true;
    }
}
