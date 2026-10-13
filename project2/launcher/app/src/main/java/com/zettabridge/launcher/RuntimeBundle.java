package com.zettabridge.launcher;

import android.content.Context;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * Installs the generated arm32 runtime into the launcher's private files directory.
 *
 * <p>The files travel inside one encrypted container asset (see {@link Payloads} and
 * {@code build/pack_payloads.py}), so the APK holds no readable runtime and no runtime file names.
 * What lands on disk keeps the layout the native side looks up - {@code filesDir/zb/...}, with the
 * names the loader resolves by string - because that layout is fixed in libzbridge.so, not here.
 */
final class RuntimeBundle {
    private static final String MARKER = ".bundle-version";

    private RuntimeBundle() {}

    static synchronized void install(Context context) throws IOException {
        File target = new File(context.getFilesDir(), "zb");
        try (Payloads.Container payload = Payloads.open(context, Keys.RUNTIME_ASSET)) {
            String version = payload.meta.trim();
            File marker = new File(target, MARKER);
            if (!version.isEmpty() && marker.isFile()
                    && version.equals(new String(Files.readAllBytes(marker.toPath()),
                            StandardCharsets.US_ASCII).trim())
                    && requiredFilesPresent(target)) {
                return;
            }

            File staging = new File(context.getFilesDir(), "zb.installing");
            PluginFiles.deleteRecursive(staging);
            if (!staging.mkdirs()) {
                throw new IOException("cannot create runtime staging directory " + staging);
            }
            boolean complete = false;
            try {
                for (Payloads.Entry entry : payload.entries) {
                    String relative = entry.name;
                    if (!PluginFiles.safeRelativePath(relative)) {
                        throw new IOException("unsafe runtime entry: " + relative);
                    }
                    File out = new File(staging, relative);
                    try (InputStream in = payload.open(entry)) {
                        PluginFiles.copyAtomic(in, out);
                    }
                    if (!out.setReadOnly()) {
                        throw new IOException("cannot make runtime file read-only: " + out);
                    }
                    if (relative.equals("guest/zbhost") || relative.equals("sysroot/system/bin/linker")) {
                        if (!out.setExecutable(true, true)) {
                            throw new IOException("cannot make runtime file executable: " + out);
                        }
                    }
                }
                PluginFiles.copyAtomic(
                        new java.io.ByteArrayInputStream((version + "\n").getBytes(StandardCharsets.US_ASCII)),
                        new File(staging, MARKER));
                if (!requiredFilesPresent(staging)) {
                    throw new IOException("generated runtime bundle is incomplete");
                }
                PluginFiles.replaceDirectory(staging, target);
                complete = true;
            } finally {
                if (!complete) PluginFiles.deleteRecursive(staging);
            }
        }
    }

    static File proxyLibrary(Context context) {
        return new File(context.getFilesDir(), "zb/host/libzbproxy.so");
    }

    private static boolean requiredFilesPresent(File root) {
        return new File(root, "sysroot/system/bin/linker").isFile()
                && new File(root, "guest/zbhost").isFile()
                && new File(root, "guest/lib/libzbcompat.so").isFile()
                && new File(root, "guest/lib/libzbjni.so").isFile()
                && new File(root, "guest/lib/libGLESv2.so").isFile()
                && new File(root, "guest/lib/libEGL.so").isFile()
                && new File(root, "guest/lib/libandroid.so").isFile()
                && new File(root, "guest/lib/libjnigraphics.so").isFile()
                && new File(root, "host/libzbproxy.so").isFile();
    }
}
