package com.zettabridge.launcher;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.net.Uri;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

import com.zettabridge.core.ZBridge;

/** Imported plugins on disk. Import is staged and published only after every native fixup succeeds. */
final class PluginStore {
    static final int ARCHIVE_FLAGS = PackageManager.GET_ACTIVITIES | PackageManager.GET_PROVIDERS
            | PackageManager.GET_SERVICES | PackageManager.GET_RECEIVERS | PackageManager.GET_META_DATA;

    private PluginStore() {}

    static File root(Context c) {
        return new File(c.getFilesDir(), "plugins");
    }

    static List<PluginRecord> list(Context c) {
        List<PluginRecord> out = new ArrayList<>();
        File[] dirs = root(c).listFiles();
        if (dirs == null) return out;
        for (File dir : dirs) {
            PluginRecord r = dir.isDirectory() ? PluginRecord.load(dir) : null;
            if (r != null) out.add(r);
        }
        out.sort((a, b) -> a.label.compareToIgnoreCase(b.label));
        return out;
    }

    static PluginRecord find(Context c, String packageName) {
        if (packageName == null || packageName.contains("/")) return null;
        return PluginRecord.load(new File(root(c), packageName));
    }

    /** Application info of an imported APK, with paths pointing at our copy. */
    static ApplicationInfo applicationInfo(PackageInfo info, PluginRecord r) {
        ApplicationInfo ai = info.applicationInfo;
        ai.sourceDir = r.apk().getPath();
        ai.publicSourceDir = ai.sourceDir;
        ai.nativeLibraryDir = r.libDir().getPath();
        ai.dataDir = r.dataDir().getPath();
        return ai;
    }

    static PluginRecord importApk(Context c, Uri uri) throws IOException {
        File tmp = File.createTempFile("translator-import-", ".apk", c.getCacheDir());
        try (InputStream in = c.getContentResolver().openInputStream(uri)) {
            if (in == null) throw new IOException("cannot open " + uri);
            copy(in, tmp);
        }
        try {
            return importApk(c, tmp, null);
        } finally {
            tmp.delete();
        }
    }

    /**
     * Imports an APK from an already-open stream, closing it. `fallbackLabel` names the app when
     * its own package carries no usable label, which is what a bundled APK needs: the name it
     * ships under is the one this build was asked for, not necessarily the original's.
     */
    static PluginRecord importApk(Context c, InputStream source, String fallbackLabel) throws IOException {
        File tmp = File.createTempFile("translator-import-", ".apk", c.getCacheDir());
        try {
            try (InputStream in = source) {
                copy(in, tmp);
            }
            return importApk(c, tmp, fallbackLabel);
        } finally {
            tmp.delete();
        }
    }

    private static PluginRecord importApk(Context c, File tmp, String fallbackLabel) throws IOException {
        PackageManager pm = c.getPackageManager();
        PackageInfo info = pm.getPackageArchiveInfo(tmp.getPath(), ARCHIVE_FLAGS);
        if (info == null || info.applicationInfo == null) {
            tmp.delete();
            throw new IOException("not an installable APK (split bundles are not supported)");
        }
        if (info.packageName.equals(c.getPackageName()) || info.packageName.contains("/")) {
            tmp.delete();
            throw new IOException("refusing to import " + info.packageName);
        }

        File plugins = root(c);
        if (!plugins.isDirectory() && !plugins.mkdirs()) throw new IOException("cannot create " + plugins);
        String labelFallback = fallbackLabel != null ? fallbackLabel : info.packageName;
        File target = new File(plugins, info.packageName);
        File staging = new File(plugins, "." + info.packageName + ".importing");
        PluginFiles.deleteRecursive(staging);
        if (!staging.mkdirs()) throw new IOException("cannot create " + staging);
        try {
            PluginRecord r = new PluginRecord(staging);
            File apk = r.apk();
            try (InputStream in = new java.io.FileInputStream(tmp)) {
                PluginFiles.copyAtomic(in, apk);
            }
            // Android 14+ refuses to load dex code from writable files.
            if (!apk.setReadOnly()) throw new IOException("cannot make APK read-only: " + apk);

            r.packageName = info.packageName;
            r.versionName = info.versionName;
            ApplicationInfo ai = applicationInfo(info, r);
            r.targetSdk = ai.targetSdkVersion;
            r.abis.addAll(PluginFiles.scanAbis(apk));
            r.selectedAbi = PluginFiles.selectAbi(r.abis);
            if (r.selectedAbi != null) {
                final boolean translated = r.isTranslated();
                // What a translated app is given is the settings screen's "Precise memory faults"
                // default, which is on: Unity's null checks resume from the exact faulting
                // instruction, and imprecise mode reports the end of the translated block instead.
                // It is applied to the record by applyImportDefaults below (which is where every
                // other new-import default is applied too) and can be turned off per app from the
                // library's long-press menu, so an app that only needs the speed can have it.
                if (translated) RuntimeBundle.install(c);
                PluginFiles.extractLibraries(apk, r.libDir(), r.selectedAbi, library -> {
                    if (!translated) return;
                    String result = ZBridge.fixGuestLibrary(library.getPath());
                    if (result == null || result.startsWith("skipped:")) {
                        throw new IOException("cannot prepare " + library.getName() + ": " + result);
                    }
                });
            } else if (!r.libDir().mkdirs()) {
                throw new IOException("cannot create " + r.libDir());
            }
            CharSequence label = ai.loadLabel(pm);
            r.label = label != null && label.length() > 0 ? label.toString() : labelFallback;
            saveIcon(ai.loadIcon(pm), r.iconFile());
            try {
                r.launcherActivity = ManifestReader.findLauncherActivity(pm.getResourcesForApplication(ai), r.packageName);
            } catch (PackageManager.NameNotFoundException e) {
                r.launcherActivity = null;
            }
            // What the settings screen says a newly imported app starts with. After the record is
            // built and before it is written, so an import that fails leaves nothing behind, and
            // after the precise-faults pinning above, which this cannot override.
            LauncherSettings.applyImportDefaults(c, r);
            // Completion marker: never write this until extraction and all in-place fixups pass.
            r.save();
            PluginFiles.replaceDirectoryKeeping(staging, target, "data");
            PluginRecord published = PluginRecord.load(target);
            if (published == null) throw new IOException("cannot read published metadata for " + info.packageName);
            return published;
        } finally {
            PluginFiles.deleteRecursive(staging);
        }
    }

    static void delete(PluginRecord r) {
        PluginFiles.deleteRecursive(r.dir);
    }

    private static void saveIcon(Drawable d, File out) {
        if (d == null) return;
        int size = 192;
        Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bmp);
        d.setBounds(0, 0, size, size);
        d.draw(canvas);
        try (OutputStream o = new FileOutputStream(out)) {
            bmp.compress(Bitmap.CompressFormat.PNG, 100, o);
        } catch (IOException ignored) {
            // no icon is fine
        }
    }

    private static void copy(InputStream in, File out) throws IOException {
        try (OutputStream o = new FileOutputStream(out)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
        }
    }

}
