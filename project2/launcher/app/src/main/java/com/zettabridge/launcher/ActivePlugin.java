package com.zettabridge.launcher;

import android.app.ActivityManager;
import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Which plugin the :guest process currently holds, readable from the launcher process.
 *
 * The guest runtime is process-lifetime, so one :guest process serves one plugin, and the
 * launcher must know which one before it routes a launch. The guest writes the name here as it
 * loads; the record counts only while that process is alive, because a dead process leaves it
 * behind.
 */
final class ActivePlugin {
    private static final String TAG = "Translator";
    private static final String FILE = "active-plugin";

    private ActivePlugin() {}

    /** Called in :guest once a plugin is loaded. */
    static void record(Context context, String packageName) {
        File file = new File(context.getFilesDir(), FILE);
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(packageName.getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            Log.w(TAG, "cannot record the active plugin: " + e);
        }
    }

    /**
     * The plugin the live :guest process holds, or null if no guest process is running. A guest
     * process with no record counts as holding an unknown plugin, which is never a match.
     */
    static String of(Context context) {
        if (!guestIsRunning(context)) return null;
        File file = new File(context.getFilesDir(), FILE);
        try (RandomAccessFile in = new RandomAccessFile(file, "r")) {
            byte[] bytes = new byte[(int) Math.min(in.length(), 512)];
            in.readFully(bytes);
            String name = new String(bytes, StandardCharsets.UTF_8).trim();
            return name.isEmpty() ? "" : name;
        } catch (IOException e) {
            return "";  // running, but unknown: not equal to any package name
        }
    }

    static boolean guestIsRunning(Context context) {
        try {
            ActivityManager manager = context.getSystemService(ActivityManager.class);
            List<ActivityManager.RunningAppProcessInfo> running =
                    manager != null ? manager.getRunningAppProcesses() : null;
            if (running == null) return false;
            for (ActivityManager.RunningAppProcessInfo info : running) {
                if (info.processName != null && info.processName.endsWith(ZbApplication.GUEST_SUFFIX)) return true;
            }
            return false;
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** Ends the :guest process, whatever plugin it holds. */
    static void endGuestProcess(Context context) {
        // Stop watching first: this death is the launcher's own doing, and a supervisor that saw it
        // would read it as the crash it exists for and reopen the game that is being closed.
        GuestSupervisor.stop(context);
        try {
            ActivityManager manager = context.getSystemService(ActivityManager.class);
            List<ActivityManager.RunningAppProcessInfo> running =
                    manager != null ? manager.getRunningAppProcesses() : null;
            if (running == null) return;
            for (ActivityManager.RunningAppProcessInfo info : running) {
                if (info.processName != null && info.processName.endsWith(ZbApplication.GUEST_SUFFIX)) {
                    Log.i(TAG, "ending the guest process " + info.pid + " to switch plugins");
                    // Tell the debug log this death is intentional, so the watchdog does not read it
                    // as a crash.
                    DebugLog.noteGuestKill();
                    DebugLog.note("killing the :guest process pid " + info.pid + " (" + info.processName
                            + ") to switch plugins");
                    android.os.Process.killProcess(info.pid);
                }
            }
        } catch (RuntimeException e) {
            Log.w(TAG, "cannot end the guest process: " + e);
        }
    }
}
