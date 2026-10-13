package com.zettabridge.launcher;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Process;
import android.util.Log;

import com.zettabridge.core.ZBridge;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Error reports that do not depend on logcat: some ROMs (OxygenOS) drop third-party app logs.
 * The full stack trace goes to the clipboard and to
 * /sdcard/Android/data/com.zettabridge.launcher/files/zb-errors.txt (appended).
 *
 * <p>The translator's own runtime report lands next to it as zb-runtime-report.txt, rewritten by
 * the :guest process whenever it changes, so it survives a :guest process that dies silently.
 */
final class Diagnostics {
    private static final String TAG = "Translator";
    private static final String REPORT_NAME = "zb-runtime-report.txt";
    private static final String ERRORS_NAME = "zb-errors.txt";
    /**
     * The debuggable build's app log, written here by the debug source set's DebugLog (which names
     * this constant rather than its own literal). The release build never writes one, so reading it
     * there simply finds nothing.
     */
    static final String APP_LOG_NAME = "zb-app-log.txt";
    /**
     * The error file is capped, and it is the only file this app appends to forever. A game in a
     * crash-restart cycle would otherwise grow it without bound, and a full app-storage partition on
     * a phone that never gets its cache cleared stops the app from writing anything at all - a
     * stability cure that turns into a disk problem is worse than no file.
     */
    private static final long ERRORS_MAX_BYTES = 1024L * 1024;

    private Diagnostics() {}

    /** The runtime report file, or null when external storage is unavailable. */
    static File runtimeReportFile(Context context) {
        File dir = context.getExternalFilesDir(null);
        return dir == null ? null : new File(dir, REPORT_NAME);
    }

    /**
     * Makes the :guest process persist its runtime report. Called once, before plugin code runs;
     * failures are not fatal, the run simply leaves no report behind.
     */
    static void startRuntimeReport(Context context) {
        File file = runtimeReportFile(context);
        if (file == null) {
            Log.w(TAG, "no external files dir: the runtime report is not persisted");
            return;
        }
        try {
            if (!ZBridge.setReportFile(file.getAbsolutePath())) {
                Log.w(TAG, "cannot persist the runtime report to " + file);
            }
        } catch (Throwable t) {
            Log.w(TAG, "cannot start the runtime report: " + t);
        }
    }

    /** The last run report, or null when no run has written one. */
    static String readRuntimeReport(Context context) {
        File file = runtimeReportFile(context);
        if (file == null || !file.isFile()) return null;
        try {
            String text = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
            return text.isEmpty() ? null : text;
        } catch (IOException | RuntimeException e) {
            Log.w(TAG, "cannot read the runtime report: " + e);
            return null;
        }
    }

    /** The error log's contents, or null when nothing has been recorded in it yet. */
    static String readErrors(Context context) {
        return read(context, ERRORS_NAME, 0);
    }

    /**
     * The debuggable build's app log, or null when this build does not write one. Only the newest
     * part is returned: the file rotates at 8 MB, and its end is the part that explains an ending.
     */
    static String readAppLog(Context context) {
        return read(context, APP_LOG_NAME, 400_000);
    }

    /** Reads one of this app's own files out of external storage, or null when it is not there. */
    private static String read(Context context, String name, int tailChars) {
        File dir = context.getExternalFilesDir(null);
        if (dir == null) return null;
        File file = new File(dir, name);
        if (!file.isFile()) return null;
        try {
            String text = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
            if (text.isEmpty()) return null;
            if (tailChars > 0 && text.length() > tailChars) {
                // Start at a line boundary, so the copy begins with a whole record.
                int from = text.length() - tailChars;
                int newline = text.indexOf('\n', from);
                text = "(older lines dropped)\n" + text.substring(newline >= 0 ? newline + 1 : from);
            }
            return text;
        } catch (IOException | RuntimeException e) {
            Log.w(TAG, "cannot read " + name + ": " + e);
            return null;
        }
    }

    /** Copies text to the clipboard under `label`; false when the clipboard is unavailable. */
    static boolean copy(Context context, String label, String text) {
        try {
            ClipboardManager cm = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return false;
            cm.setPrimaryClip(ClipData.newPlainText(label, text));
            return true;
        } catch (RuntimeException e) {
            Log.w(TAG, "cannot copy to the clipboard: " + e);
            return false;
        }
    }

    /** Records an error; copies it to the clipboard when copy is true. Returns the report text. */
    static String report(Context context, String what, Throwable t, boolean copy) {
        String stamp = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date());
        String text = stamp + " " + what + "\n" + Log.getStackTraceString(t);
        Log.e(TAG, what, t);
        // In the debuggable build this also lands in zb-app-log.txt and, for the last lines, on the
        // clipboard, so one file carries the whole story of a failed launch.
        DebugLog.failure(what, t);
        appendToFile(context, text);
        // The automatic clipboard copy is a setting (Reliability, on by default); a caller that asks
        // for it explicitly is honoured only while it is on.
        if (copy && LauncherSettings.copyFailuresToClipboard(context)) {
            copy(context, "Translator error", text);
        }
        return text;
    }

    /**
     * Records an uncaught exception, from {@link CrashGuard} - which decides whether the process
     * survives it. Called before that decision and unconditionally, because an exception that is
     * survived crashes nothing and would otherwise never be written down anywhere.
     */
    static void recordUncaught(Context context, Thread thread, Throwable t) {
        try {
            appendToFile(context, new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date())
                    + " uncaught in thread " + thread.getName() + " (process " + ZbApplication.processName()
                    + ", pid " + Process.myPid() + ")"
                    + "\n" + Log.getStackTraceString(t));
        } catch (Throwable ignored) {
            // never mask the original crash
        }
        try {
            DebugLog.failure("uncaught in thread " + thread.getName() + " (process "
                    + ZbApplication.processName() + ", pid " + Process.myPid() + ")", t);
        } catch (Throwable ignored) {
            // a diagnostic must not add to the failure it is describing
        }
    }

    /**
     * Records a stability event - a watched death, a reopening, a give-up - so the shipping build
     * leaves a trail of what it did to the game while the game was not running.
     */
    static void recovery(Context context, String what) {
        appendToFile(context, new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date())
                + " RECOVERY process=" + ZbApplication.processName() + " " + what);
        try {
            DebugLog.note(what);
        } catch (Throwable ignored) {
            // see above
        }
    }

    /**
     * Records memory pressure. A process killed for memory leaves nothing behind, so the trims the
     * framework sends on the way there are the only trace a low-memory kill ever has.
     */
    static void noteMemory(Context context, int level) {
        appendToFile(context, new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date())
                + " MEMORY process=" + ZbApplication.processName() + " pid=" + Process.myPid()
                + " trimLevel=" + level + " javaUsed=" + used() + " systemAvailable=" + available(context));
    }

    private static long used() {
        Runtime runtime = Runtime.getRuntime();
        return runtime.totalMemory() - runtime.freeMemory();
    }

    private static String available(Context context) {
        try {
            android.app.ActivityManager manager = context.getSystemService(android.app.ActivityManager.class);
            if (manager == null) return "unknown";
            android.app.ActivityManager.MemoryInfo info = new android.app.ActivityManager.MemoryInfo();
            manager.getMemoryInfo(info);
            return info.availMem + (info.lowMemory ? " (system says low)" : "");
        } catch (Throwable t) {
            return "unknown";
        }
    }

    private static void appendToFile(Context context, String text) {
        try {
            File dir = context.getExternalFilesDir(null);
            if (dir == null) return;
            File file = new File(dir, ERRORS_NAME);
            if (file.length() > ERRORS_MAX_BYTES) rotate(dir, file);
            try (FileOutputStream out = new FileOutputStream(file, true)) {
                out.write((text + "\n").getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception e) {
            Log.w(TAG, "cannot write the error file: " + e);
        }
    }

    /** Keeps the most recent half of a file that has outgrown its cap; the older copy is replaced. */
    private static void rotate(File dir, File file) {
        try {
            byte[] bytes = Files.readAllBytes(file.toPath());
            int keep = (int) (ERRORS_MAX_BYTES / 2);
            if (bytes.length <= keep) return;
            int from = bytes.length - keep;
            // Start at a line boundary so the surviving half begins with a complete record.
            while (from < bytes.length && bytes[from] != '\n') from++;
            try (FileOutputStream out = new FileOutputStream(file)) {
                out.write(bytes, from, bytes.length - from);
            }
        } catch (Exception e) {
            Log.w(TAG, "cannot rotate the error file: " + e);
        }
    }
}
