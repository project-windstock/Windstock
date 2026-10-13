package com.zettabridge.launcher;

import android.app.ActivityManager;
import android.app.Application;
import android.content.Context;
import android.content.res.Configuration;
import android.os.Build;
import android.util.Log;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * Per-process setup: the watchdogs in the :guest process, the crash guard in both.
 *
 * <p>Everything here runs before any plugin code and must therefore never throw. A failure in this
 * class is not a failed launch with a screen on it - it is a process that dies while Android is
 * still starting it, which is the least diagnosable way an app can fail.
 */
public class ZbApplication extends Application {
    static final String GUEST_SUFFIX = ":guest";

    @Override
    public void onCreate() {
        super.onCreate();
        // No-op in the release build; in the debuggable one this opens the app log for this process
        // and records why a window or a process ends (see app/src/debug/.../DebugLog.java).
        DebugLog.install(this);
        boolean guest = isGuest();
        // Last of the handlers, on purpose: it owns the chain, so it decides whether a crash is
        // fatal before the framework's own handler - the one that kills the process - sees it. Every
        // uncaught exception in this process is recorded here first, in both processes.
        CrashGuard.install(this, guest);
        if (!guest) return;
        MainLooperGuard.install(this);
        // Before any plugin code runs: a :guest process that dies silently must still leave
        // its runtime report on disk. On by default; the settings screen's "Persist the runtime
        // report" switch turns it off for a session that would rather not write the file. The
        // decision is stored by the launcher process and read here, in a process that is one launch
        // old at most, so it is always the value the user last chose.
        if (LauncherSettings.persistRuntimeReport(this)) Diagnostics.startRuntimeReport(this);
        // The lease this process will hold for as long as it lives, which is how the launcher
        // process tells a crash from an exit. Taken first, because from here on a death has to be
        // attributable to something.
        GuestHeartbeat.begin(this);
        // Nothing in this process may die before a plugin has had its chance. A runtime bundle
        // that cannot be extracted, or framework reflection that a future release refuses, used
        // to escape here and take the :guest process with it at startup - which is what an app
        // that "sometimes just closes" looks like. The launch path checks isInstalled() and
        // hands the reason to the failure screen instead.
        try {
            GuestRuntime.get().install(this);
        } catch (Throwable t) {
            DebugLog.failure("installing the guest runtime failed; no plugin can start in this "
                    + "process, and the launch path will offer a restart", t);
            Diagnostics.report(this, "cannot install the guest runtime", t, false);
        }
        // 'loaded' rather than 'starting' once the runtime is in: what the launcher reads this for is
        // whether a window was on screen when a death happened, and this is the state a plugin is
        // about to be loaded from.
        GuestHeartbeat.phase(this, GuestRuntime.get().isInstalled()
                ? GuestHeartbeat.LOADED : GuestHeartbeat.STARTING, null);
    }

    /**
     * Memory pressure, recorded rather than acted on. There is nothing this process can usefully give
     * back - the guest's memory belongs to the plugin and to the runtime, and both are native - so
     * the value of these callbacks is the trail they leave: a process killed for memory writes
     * nothing, and these are the last thing that does.
     */
    @Override
    public void onTrimMemory(int level) {
        super.onTrimMemory(level);
        try {
            // The light levels arrive constantly while the app is in the background and say nothing.
            if (level >= TRIM_MEMORY_RUNNING_LOW) Diagnostics.noteMemory(this, level);
        } catch (Throwable ignored) {
            // never add to the pressure it is reporting on
        }
    }

    @Override
    public void onLowMemory() {
        super.onLowMemory();
        try {
            Diagnostics.noteMemory(this, -1);
        } catch (Throwable ignored) {
            // see above
        }
    }

    @Override
    public void onConfigurationChanged(Configuration configuration) {
        super.onConfigurationChanged(configuration);
        // Recorded because a rotation is the most common reason an activity is destroyed and
        // recreated, and a reader of the log has to be able to tell that apart from a crash.
        try {
            DebugLog.note("configuration changed: " + configuration);
        } catch (Throwable ignored) {
            // the log is a convenience
        }
    }

    /**
     * Ends the :guest process if this app has one running, and says whether there was one to end.
     *
     * <p>Killing a process of our own uid is allowed without a permission, and it is the only way a
     * running guest is actually stopped: the guest runtime is process-lifetime, so a plugin change
     * takes effect in the next :guest process and not in the running one. Both the library (before
     * an import or a delete) and the settings screen (as an action of its own) need it.
     */
    static boolean stopGuestProcess(Context context) {
        ActivityManager manager = context.getSystemService(ActivityManager.class);
        if (manager == null || manager.getRunningAppProcesses() == null) return false;
        boolean stopped = false;
        for (ActivityManager.RunningAppProcessInfo process : manager.getRunningAppProcesses()) {
            if (process.processName.endsWith(GUEST_SUFFIX)) {
                android.os.Process.killProcess(process.pid);
                stopped = true;
            }
        }
        return stopped;
    }

    static boolean isGuest() {
        return processName().endsWith(GUEST_SUFFIX);
    }

    static String processName() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            try {
                return Application.getProcessName();
            } catch (Throwable t) {
                Log.w("Translator", "cannot read this process's name: " + t);
            }
        }
        try (FileInputStream in = new FileInputStream("/proc/self/cmdline")) {
            byte[] buf = new byte[256];
            int n = in.read(buf);
            int end = 0;
            while (end < n && buf[end] != 0) end++;
            return new String(buf, 0, end, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return "";
        }
    }
}
