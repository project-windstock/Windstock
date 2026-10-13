package com.zettabridge.launcher;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.Application;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import android.os.Debug;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.system.Os;
import android.system.OsConstants;
import android.util.Log;
import android.view.View;
import android.view.ViewTreeObserver;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.WeakHashMap;

/**
 * Debug-variant build of {@link DebugLog}: everything, in the app log.
 *
 * <p>This file is only compiled into the debuggable variant (see build-debug.sh): the release
 * variant compiles the no-op copy at app/src/release/java. The two never meet - the variant source
 * set supplies the one class {@code main} expects - so every call site in the launcher that is
 * silent in the release build narrates here.
 *
 * <p>It can be switched off (Settings, Logging, "Detailed app log"; default on). Off means: no log
 * file, no per-event write and flush, no read of the multi-megabyte runtime report every three
 * seconds and no watchdog tick - everything this class does that a running game can feel. Errors
 * still reach logcat and {@code zb-errors.txt}, which are written by {@link Diagnostics}, not here.
 *
 * <p>Where the log goes, and why more than one place:
 * <ul>
 *   <li>logcat, tag {@code Translator} - when the ROM keeps third-party logs;</li>
 *   <li>{@code filesDir/zb-app-log.txt} on external app storage
 *       ({@code /sdcard/Android/data/com.chucny.translator.bridge/files/zb-app-log.txt}) - the
 *       primary copy, because some ROMs (OxygenOS) drop third-party logcat entirely, and because a
 *       file survives the process that wrote it: the tail of the log is what explains a crash that
 *       killed the logger as well. Each line is written with its own {@code write()} syscall, so a
 *       SIGKILL or a native crash cannot lose what was already logged;</li>
 *   <li>the clipboard - the last lines plus the crash, copied when a window is lost, so a phone
 *       with no browser and a ROM with no usable logcat can still hand the evidence to someone;</li>
 *   <li>{@code zb-errors.txt} and {@code zb-runtime-report.txt} next to it, written by
 *       {@link Diagnostics} and by the native runtime (unchanged).</li>
 * </ul>
 *
 * <p>Why the window closed is assembled from four sources, in this order of authority:
 * <ol>
 *   <li>an uncaught exception in this process - a Java crash takes every window the process owns;</li>
 *   <li>this process dying with nothing Java-side: the native runtime report (the :guest process
 *       writes it itself), and the launcher's watchdog, which notices the :guest pid disappearing;</li>
 *   <li>the activity's own state at destroy time - {@code isFinishing()} (the app closed it),
 *       {@code isChangingConfigurations()} (the system rebuilt it), neither (the system tore it
 *       down);</li>
 *   <li>memory pressure and process state around the event, for a low-memory kill that leaves no
 *       trace of its own.</li>
 * </ol>
 */
final class DebugLog {
    private static final String TAG = "Translator";
    // Named by Diagnostics, which is also what reads it back for the settings screen's "copy the
    // debug app log" action, so the two can never drift apart.
    private static final String FILE_NAME = Diagnostics.APP_LOG_NAME;
    private static final long MAX_BYTES = 8L * 1024 * 1024;
    /** Nothing this logger writes may cost the game anything: one record is bounded to this. */
    private static final int MAX_RECORD_CHARS = 4000;
    /**
     * The part of the runtime report worth logging: the facts that say what the guest did and how it
     * ended. The per-second counters (native-calls, egl-swaps, gl-calls) are deliberately not here -
     * they change on every poll, which would write a log line every few seconds for a whole game
     * session. They are read where they matter instead: when a window closes or stays black.
     */
    private static final String[] REPORT_KEEP_PREFIXES = {
            "zettabridge-runtime-report", "plugin:", "proxy-loads:", "proxy-failures:", "proxy-loaded:",
            "jni-onload-calls:", "jni-onload:", "registered-natives:", "unimplemented-host-calls:",
            "unimplemented-distinct:", "first-unimplemented:", "guest-exit:", "signal-restart:",
            "signal-", "fault-", "egl-choose", "egl-context", "egl-surface", "egl-first-error:",
            "gl-first-call:", "gl-first-error:", "gl-last-error:",
            "jni-runtime-start:", "jni-loading:", "jni-native-activity:",
            "jni-native-activity-callbacks:", "jni-native-activity-last:", "jni-looper-real:",
            "jni-input-attach:", "jni-configuration-asset-manager:",
    };
    private static final int TAIL_LINES = 400;
    private static final long CRASH_WINDOW_MS = 5000;
    private static final long REPORT_POLL_MS = 3000;
    private static final long WATCHDOG_MS = 1000;

    private static final Object LOCK = new Object();
    private static final ArrayDeque<String> tail = new ArrayDeque<>();
    /** Windows that have actually drawn a frame. A window that never does is the black-window case. */
    private static final Set<Activity> drew = Collections.newSetFromMap(new WeakHashMap<Activity, Boolean>());

    private static FileOutputStream stream;
    private static File file;
    private static Context context;
    private static String process = "?";
    private static boolean guest;
    private static Handler main;
    private static String lastReport;
    private static int guestPid = -1;
    private static String guestPlugin;
    private static long lastCrashAt;
    private static String lastCrash = "none";
    private static long startedAt;

    /**
     * True when this log is off for the next process to start, or already off in this one. Read by
     * every entry point: a disabled log must cost a branch, not a file write, so the switch is a
     * volatile field checked before anything else happens.
     */
    private static volatile boolean off;

    private DebugLog() {}

    // -------------------------------------------------------------- switches

    /** Only the debuggable build has a log to switch; the release variant answers false here. */
    static boolean available() {
        return true;
    }

    /** Whether the detailed app log is on, as the settings screen last left it (default: on). */
    static boolean enabled(Context app) {
        return LauncherSettings.appLog(app);
    }

    /**
     * Turns the log on or off, now and for the next process. Switching it off releases the file and
     * makes every writer below a no-op, which is what makes a long session on a slow device cheaper:
     * no per-event write and flush, no report read every few seconds, no watchdog tick. Switching it
     * back on resumes the same file from this process, so it takes effect without a restart.
     */
    static void setEnabled(Context app, boolean enabled) {
        LauncherSettings.appLog(app, enabled);
        if (enabled) {
            off = false;
            if (stream == null && context != null) open(context);
            write("NOTE  the detailed app log was switched back on");
            return;
        }
        write("NOTE  the detailed app log was switched off: errors still reach logcat, the rest of "
                + "this session is not narrated");
        off = true;
        close();
    }

    /** Releases the log file, so a session that logs nothing holds no file handle either. */
    private static void close() {
        synchronized (LOCK) {
            if (stream == null) return;
            try {
                stream.flush();
                stream.close();
            } catch (IOException e) {
                Log.w(TAG, "cannot close the debug log: " + e);
            }
            stream = null;
        }
    }

    // ---------------------------------------------------------------- install

    static void install(Application app) {
        try {
            context = app;
            process = ZbApplication.processName();
            // Off is a decision, not a failure: record it once and leave every hook uninstalled, so
            // a process that is not being watched pays nothing for the watching.
            if (!enabled(app)) {
                off = true;
                Log.i(TAG, "the detailed app log is off for " + process
                        + "; errors still reach logcat and zb-errors.txt");
                return;
            }
            guest = process.endsWith(ZbApplication.GUEST_SUFFIX);
            main = new Handler(Looper.getMainLooper());
            startedAt = SystemClock.elapsedRealtime();
            open(app);
            banner(app);
            Thread.setDefaultUncaughtExceptionHandler(DebugLog::uncaught);
            app.registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
                @Override
                public void onActivityCreated(Activity a, Bundle state) {
                    note("activity created" + (state != null ? " (restored from saved state)" : "") + ": " + name(a));
                    attach(a);
                }

                @Override
                public void onActivityStarted(Activity a) {
                    event(a, "started");
                }

                @Override
                public void onActivityResumed(Activity a) {
                    event(a, "resumed - this is the window on screen now");
                }

                @Override
                public void onActivityPaused(Activity a) {
                    event(a, "paused - something else is in front of it, or it is on its way out");
                }

                @Override
                public void onActivityStopped(Activity a) {
                    event(a, "stopped - no longer visible");
                }

                @Override
                public void onActivitySaveInstanceState(Activity a, Bundle state) {
                    event(a, "saving state (the framework expects to restore this window)");
                }

                @Override
                public void onActivityDestroyed(Activity a) {
                    windowGone(a, "Application.ActivityLifecycleCallbacks");
                }
            });
            if (guest) {
                main.post(reportPoller);
            } else {
                main.post(watchdog);
            }
        } catch (Throwable t) {
            // A diagnostic must never be the thing that breaks the app.
            Log.e(TAG, "debug log install failed", t);
        }
    }

    private static void banner(Context app) {
        write("================ Translator debug log opened ================");
        write("process        : " + process + "   pid " + Process.myPid() + "   uid " + Process.myUid()
                + (guest ? "   (the :guest process: the runtime and one plugin live here)" : "   (the launcher process: UI and routing)"));
        write("package        : " + app.getPackageName());
        try {
            android.content.pm.PackageInfo info = app.getPackageManager().getPackageInfo(app.getPackageName(), 0);
            write("version        : " + info.versionName + " (" + info.versionCode + ")");
            write("applicationInfo: debuggable=" + ((info.applicationInfo.flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0)
                    + " targetSdk=" + info.applicationInfo.targetSdkVersion
                    + " minSdk=" + (Build.VERSION.SDK_INT >= 24 ? info.applicationInfo.minSdkVersion : "?")
                    + " dataDir=" + info.applicationInfo.dataDir);
        } catch (Throwable t) {
            write("version        : unknown (" + t + ")");
        }
        write("device         : " + Build.MANUFACTURER + " " + Build.MODEL + "  Android " + Build.VERSION.RELEASE
                + " (API " + Build.VERSION.SDK_INT + ")  abi=" + Build.SUPPORTED_ABIS[0]
                + "  rom=" + Build.DISPLAY);
        // The facts that differ between two phones running the same APK, and the ones worth
        // comparing first when one of them shows a black window and the other does not.
        write("device detail  : soc=" + soc() + "  abilist=" + String.join(",", Build.SUPPORTED_ABIS)
                + "  32-bit-abis=" + (Build.SUPPORTED_32_BIT_ABIS.length == 0 ? "none (this device runs no arm32 app code)"
                        : String.join(",", Build.SUPPORTED_32_BIT_ABIS))
                + "  pageSize=" + pageSize() + " bytes"
                + "  gles=" + gles()
                + "  hwui=" + System.getProperty("ro.hardware.egl"));
        write("debug          : debuggerAttached=" + Debug.isDebuggerConnected()
                + " waitingForDebugger=" + Debug.waitingForDebugger()
                + " nativeHeapAllocated=" + Debug.getNativeHeapAllocatedSize()
                + " nativeHeapSize=" + Debug.getNativeHeapSize());
        write("java memory    : max=" + mb(Runtime.getRuntime().maxMemory())
                + " total=" + mb(Runtime.getRuntime().totalMemory())
                + " free=" + mb(Runtime.getRuntime().freeMemory()));
        write("system memory  : " + memory());
        write("threads        : main=" + Looper.getMainLooper().getThread().getName()
                + " isMainThread=" + (Looper.myLooper() == Looper.getMainLooper())
                + " caller=" + Thread.currentThread().getName());
        if (guest) {
            write("guest runtime  : installed=" + GuestRuntime.get().isInstalled()
                    + " reportFile=" + Diagnostics.runtimeReportFile(app));
        } else {
            write("plugins        : " + pluginList(app));
            write("guest now      : " + guestState(app));
        }
        write("log file       : " + (file != null ? file.getAbsolutePath() : "unavailable (external storage?)"));
        write("what follows   : every window event, every launch decision, the runtime report whenever it "
                + "changes, memory around every close, and the reason for each close");
    }

    private static String pluginList(Context app) {
        try {
            List<PluginRecord> records = PluginStore.list(app);
            if (records.isEmpty()) return "none imported";
            StringBuilder out = new StringBuilder();
            for (PluginRecord r : records) {
                out.append("\n                 - ").append(r.label).append("  ").append(r.packageName)
                        .append("  ").append(r.status());
            }
            return out.toString();
        } catch (Throwable t) {
            return "cannot read the store: " + t;
        }
    }

    // -------------------------------------------------------------- accidents

    private static String soc() {
        try {
            if (Build.VERSION.SDK_INT >= 31) return Build.SOC_MODEL + "/" + Build.SOC_MANUFACTURER;
        } catch (Throwable ignored) {
            // not all ROMs answer
        }
        return Build.HARDWARE;
    }

    /**
     * The kernel page size. Android 15 and later can run a 16 KB page size kernel, where a guest
     * image or a linker build assuming 4 KB segments fails to map - the failure mode is a window
     * that opens black and is gone a second later, with nothing on the Java side.
     */
    private static long pageSize() {
        try {
            return Os.sysconf(OsConstants._SC_PAGESIZE);
        } catch (Throwable t) {
            return -1;
        }
    }

    private static String gles() {
        try {
            ActivityManager manager = context.getSystemService(ActivityManager.class);
            android.content.pm.ConfigurationInfo info = manager.getDeviceConfigurationInfo();
            return info.getGlEsVersion();
        } catch (Throwable t) {
            return "unknown";
        }
    }

    private static void uncaught(Thread thread, Throwable t) {
        lastCrashAt = SystemClock.elapsedRealtime();
        lastCrash = t.getClass().getName() + (t.getMessage() != null ? ": " + t.getMessage() : "");
        write("XXXXXXXXXXXXXXXX FATAL: uncaught exception XXXXXXXXXXXXXXXX");
        write("FATAL  thread \"" + thread.getName() + "\" (id " + thread.getId() + ") threw " + lastCrash);
        write("FATAL  cause: " + cause(t));
        write("FATAL  stack:\n" + Log.getStackTraceString(t));
        write("FATAL  what this means: the process " + process + " (pid " + Process.myPid() + ") is being "
                + "killed, so every window it owned is gone. If this log is from the :guest process, the "
                + "window that just closed was the plugin's or a stub's, and the launcher process is still "
                + "alive and about to show \"Cannot launch\" or return to the library.");
        summary("after the fatal exception above");
        clip("FATAL " + lastCrash + "\n" + Log.getStackTraceString(t));
        Thread.UncaughtExceptionHandler previous = installedBefore;
        if (previous != null) previous.uncaughtException(thread, t);
    }

    private static Thread.UncaughtExceptionHandler installedBefore;

    /** The message a native crash report carries, for a fault that happened inside a translated block. */
    private static String cause(Throwable t) {
        StringBuilder out = new StringBuilder();
        out.append("Java exception, first frame ");
        StackTraceElement[] stack = t.getStackTrace();
        out.append(stack.length > 0 ? stack[0].toString() : "unknown");
        Throwable root = t;
        while (root.getCause() != null && root.getCause() != root) root = root.getCause();
        if (root != t) out.append("; root cause ").append(root);
        return out.toString();
    }

    /** The :guest process disappearing is the usual reason a game window closes. */
    private static final Runnable watchdog = new Runnable() {
        @Override
        public void run() {
            if (off) {
                main.postDelayed(this, WATCHDOG_MS);
                return;
            }
            try {
                watchGuest();
            } catch (Throwable t) {
                write("WATCHDOG  " + t);
            }
            main.postDelayed(this, WATCHDOG_MS);
        }
    };

    private static void watchGuest() {
        int pid = -1;
        String plugin = null;
        try {
            ActivityManager manager = context.getSystemService(ActivityManager.class);
            List<ActivityManager.RunningAppProcessInfo> running =
                    manager != null ? manager.getRunningAppProcesses() : null;
            if (running != null) {
                for (ActivityManager.RunningAppProcessInfo info : running) {
                    if (info.processName != null && info.processName.endsWith(ZbApplication.GUEST_SUFFIX)) {
                        pid = info.pid;
                        break;
                    }
                }
            }
        } catch (Throwable t) {
            return;
        }
        if (pid != guestPid) {
            if (pid == -1) {
                write("WATCHDOG  the :guest process (pid " + guestPid + ") is gone."
                        + "\nWHAT THIS MEANS: whatever the guest was showing is closed. Either the plugin "
                        + "exited by itself, or the system killed the process, or it crashed."
                        + "\nWHY: " + guestDeathReason()
                        + "\nLAST GUEST ACCOUNT: " + oneLine(readReportTail()));
                summary("the :guest process went away");
            } else {
                write("WATCHDOG  :guest process started, pid " + pid);
                lastReport = null;
            }
            guestPid = pid;
        }
        if (pid != -1) {
            String now = null;
            try {
                now = ActivePlugin.of(context);
            } catch (Throwable ignored) {
                // the store may not be readable yet
            }
            if (now != null && !now.equals(guestPlugin)) {
                guestPlugin = now;
                write("WATCHDOG  the :guest process holds plugin " + (now.isEmpty() ? "<unknown>" : now));
            }
        }
    }

    /**
     * Distinguishes the three ways a :guest process ends, from what this process can still see: a
     * native/crash report on disk, a plugin that asked to finish, or nothing at all - which is a
     * kill outside the app (low memory, force stop, or a native fault that left no report).
     */
    private static String guestDeathReason() {
        String report = readReportTail();
        if (report != null && report.toLowerCase(Locale.US).contains("signal")) {
            return "the guest runtime recorded a SIGNAL (native fault) - see the runtime report below";
        }
        if (PluginsGoingAway.get()) {
            return "the launcher itself ended it (switching plugins, or reinstalling the bundled app)";
        }
        if (report != null && !report.isEmpty()) {
            return "no signal recorded in the runtime report; most likely the system killed the process "
                    + "(low memory / background) or the guest runtime called exit";
        }
        return "nothing was recorded - the process was killed from outside the app (low memory, force "
                + "stop) or died before the runtime wrote anything";
    }

    /** Set by {@link ActivePlugin#endGuestProcess}: an intentional kill is not a crash. */
    static void noteGuestKill() {
        PluginsGoingAway.mark();
    }

    /** A single flag shared with ActivePlugin, so an intentional kill is not reported as a crash. */
    static final class PluginsGoingAway {
        private static volatile boolean goingAway;

        static void mark() {
            goingAway = true;
            if (main != null) {
                main.postDelayed(() -> goingAway = false, 5000);
            }
        }

        static boolean get() {
            return goingAway;
        }
    }

    /**
     * The guest writes its own report to disk (the runtime persists it; see
     * {@code ZBridge.setReportFile}). This process only ever reads that file - never
     * {@code ZBridge.runtimeReport()}, because calling into the bridge from a Java thread while the
     * guest is running means entering the runtime that owns the guest threads, for nothing but a
     * log line. That is what turned a diagnostic build into a broken game once; do not put it back.
     *
     * <p>Only the stable lines are logged ({@link #REPORT_KEEP_PREFIXES}): the report also carries
     * per-second thread and counter dumps, which change constantly and would otherwise write a
     * copy of the whole report, several kilobytes at a time, forever.
     */
    private static final Runnable reportPoller = new Runnable() {
        @Override
        public void run() {
            // While the log is off this must not read and parse a multi-megabyte report: that read is
            // the largest thing this class does to a running game, and it is the reason the switch
            // exists. The poll is kept scheduled so switching the log back on resumes it.
            if (off) {
                main.postDelayed(this, REPORT_POLL_MS);
                return;
            }
            try {
                String digest = digest(readReport());
                if (digest != null && !digest.equals(lastReport)) {
                    lastReport = digest;
                    write("RUNTIME REPORT (the runtime's own account, as it changes):\n" + digest);
                }
            } catch (Throwable t) {
                write("REPORT  cannot read the runtime report: " + t);
            }
            main.postDelayed(this, REPORT_POLL_MS);
        }
    };

    private static String readReport() {
        try {
            String report = Diagnostics.readRuntimeReport(context);
            return report == null || report.isEmpty() ? lastFullReport : report;
        } catch (Throwable t) {
            return lastFullReport;
        }
    }

    /** The stable facts of a report: the lines that say what the guest did, and how it ended. */
    private static String digest(String report) {
        if (report == null) return null;
        lastFullReport = report;
        StringBuilder out = new StringBuilder();
        for (String line : report.split("\n")) {
            String trimmed = line.trim();
            for (String prefix : REPORT_KEEP_PREFIXES) {
                if (trimmed.startsWith(prefix)) {
                    out.append("    ").append(trimmed.length() > 300 ? trimmed.substring(0, 300) + "..." : trimmed)
                            .append('\n');
                    break;
                }
            }
        }
        return out.length() == 0 ? null : out.toString();
    }

    private static String lastFullReport;

    private static String readReportTail() {
        try {
            String report = readReport();
            if (report == null) return null;
            String[] lines = report.split("\n");
            int from = Math.max(0, lines.length - 12);
            StringBuilder out = new StringBuilder();
            for (int i = from; i < lines.length; i++) out.append(lines[i]).append('\n');
            return out.toString();
        } catch (Throwable t) {
            return null;
        }
    }

    // ----------------------------------------------------------------- windows

    static void attach(Activity activity) {
        try {
            write("WINDOW  attaching " + name(activity) + "  intent=" + activity.getIntent()
                    + "  task=" + activity.getTaskId() + "  process=" + process);
            final String label = name(activity);
            final View decor = activity.getWindow() != null ? activity.getWindow().getDecorView() : null;
            if (decor != null) {
                decor.getViewTreeObserver().addOnDrawListener(new ViewTreeObserver.OnDrawListener() {
                    private boolean first = true;

                    @Override
                    public void onDraw() {
                        if (!first) return;
                        first = false;
                        drew.add(activity);
                        write("WINDOW  " + label + " drew its first frame " + decor.getWidth() + "x"
                                + decor.getHeight() + " (the window is really on screen)");
                    }
                });
            }
            for (long delay : new long[]{0, 400, 1500, 5000}) {
                main.postDelayed(() -> focus(label, activity), delay);
            }
            // A window that is on screen and still has not drawn is the "black window" case: the
            // surface exists, nothing ever painted it. The renderer's own counters say whether the
            // guest got as far as a GL context and a swap.
            for (long delay : new long[]{1200, 2500, 5000}) {
                main.postDelayed(() -> blackCheck(label, activity, delay), delay);
            }
        } catch (Throwable t) {
            write("WINDOW  cannot watch " + name(activity) + ": " + t);
        }
    }

    /**
     * Called after a window has been on screen for a while. If nothing was drawn, this is where the
     * black window is recorded, together with the renderer's counters - the difference between "no
     * surface at all", "surface but no GL context" and "GL running, nothing presented" is what
     * separates a guest that never started from one whose renderer is stuck or faulting.
     */
    private static void blackCheck(String label, Activity activity, long afterMs) {
        try {
            if (drew.contains(activity)) return;
            if (activity.isFinishing() || activity.isDestroyed()) return;
            View decor = activity.getWindow() != null ? activity.getWindow().getDecorView() : null;
            if (decor == null || !decor.isAttachedToWindow()) return;
            StringBuilder out = new StringBuilder();
            out.append("BLACK WINDOW: ").append(label).append(" has been attached for ~").append(afterMs)
                    .append(" ms and has not drawn a single frame.")
                    .append("\n    why this matters : the surface is on screen and empty. Nothing Java-side ")
                    .append("has thrown, so the guest never presented anything.")
                    .append("\n    state            : ").append(state(activity))
                    .append("\n    memory           : ").append(memory());
            String evidence = renderEvidence();
            out.append("\n RENDERER SAYS    : ").append(evidence);
            write(out.toString());
        } catch (Throwable t) {
            write("BLACK WINDOW check for " + label + " failed: " + t);
        }
    }

    /** The renderer lines of the runtime report: how far the guest got towards a frame. */
    private static String renderEvidence() {
        try {
            String report = readReport();
            if (report == null) return "no runtime report yet (the guest runtime has not written one)";
            StringBuilder out = new StringBuilder();
            for (String line : report.split("\n")) {
                String trimmed = line.trim();
                if (trimmed.startsWith("gl-") || trimmed.startsWith("egl-")
                        || trimmed.startsWith("guest-exit:") || trimmed.startsWith("jni-runtime-start:")
                        || trimmed.startsWith("jni-loading:") || trimmed.startsWith("jni-native-activity")
                        || trimmed.startsWith("proxy-") || trimmed.startsWith("signal")
                        || trimmed.startsWith("fault")) {
                    out.append("\n        ").append(trimmed.length() > 300 ? trimmed.substring(0, 300) + "..." : trimmed);
                }
            }
            return out.length() == 0 ? "the report has no renderer lines yet" : out.toString();
        } catch (Throwable t) {
            return "cannot read the runtime report: " + t;
        }
    }

    private static void focus(String label, Activity activity) {
        try {
            write("WINDOW  " + label + " focus=" + activity.hasWindowFocus()
                    + " finishing=" + activity.isFinishing()
                    + " destroyed=" + activity.isDestroyed()
                    + " changingConfigurations=" + activity.isChangingConfigurations());
        } catch (Throwable t) {
            write("WINDOW  " + label + " state unreadable: " + t);
        }
    }

    private static void event(Activity activity, String what) {
        write("WINDOW  " + name(activity) + " " + what + "  [" + state(activity) + "]");
    }

    /**
     * The reason a window is gone, said in full. Called from the guest instrumentation for plugin
     * and stub activities, and from the lifecycle callbacks for the launcher's own screens.
     */
    static void windowGone(Activity activity, String trigger) {
        try {
            String reason;
            if (activity.isChangingConfigurations()) {
                reason = "the system is rebuilding it for a configuration change (rotation, theme, "
                        + "density, locale). Normal: a new instance follows, this is not a crash.";
            } else if (activity.isFinishing()) {
                reason = "something called finish() on it - a deliberate close (the plugin finished "
                        + "itself, the launcher routed away, or Back closed it).";
            } else {
                reason = "the SYSTEM tore it down: not a finish() and not a configuration change, so "
                        + "the process is going away (killed, task removed, or destroyed for memory). "
                        + "Look at the line above this one and at the runtime report for the trigger.";
            }
            StringBuilder out = new StringBuilder();
            out.append("WINDOW CLOSED: ").append(name(activity)).append('\n');
            out.append("    trigger     : ").append(trigger).append('\n');
            out.append("    why         : ").append(reason).append('\n');
            out.append("    state       : ").append(state(activity)).append('\n');
            out.append("    crash       : ").append(crashNearby()).append('\n');
            out.append("    plugin      : ").append(pluginState()).append('\n');
            out.append("    drew a frame: ").append(drew.contains(activity) ? "yes" : "NO - it was black for its whole life").append('\n');
            out.append("    memory      : ").append(memory());
            if (!drew.contains(activity)) {
                out.append("\n    CAUSE       : the window never drew anything, so whatever closed it had nothing to ")
                        .append("show. Renderer evidence:");
                String evidence = renderEvidence();
                if (evidence.startsWith("\n")) {
                    out.append("\n        ").append(evidence.trim());
                } else {
                    out.append(" ").append(evidence);
                }
            }
            long crashAt = lastCrashAt;
            long age = SystemClock.elapsedRealtime() - crashAt;
            if (crashAt > 0 && age <= CRASH_WINDOW_MS) {
                out.append("\n    CAUSE       : this window died with the uncaught exception logged ")
                        .append(age).append(" ms earlier (").append(lastCrash).append(')');
                clip("window closed because of " + lastCrash);
            }
            if (guest) {
                String report = readReportTail();
                if (report != null && !report.isEmpty()) {
                    out.append("\n    guest runtime report tail:\n").append(indent(report));
                }
            }
            write(out.toString());
        } catch (Throwable t) {
            write("WINDOW  close of " + name(activity) + " not explained: " + t);
        }
    }

    private static String crashNearby() {
        if (lastCrashAt == 0) return "none recorded in this process (" + process + ")";
        long age = SystemClock.elapsedRealtime() - lastCrashAt;
        return lastCrash + " (" + age + " ms ago" + (age <= CRASH_WINDOW_MS ? ", this is the cause" : "") + ")";
    }

    private static String state(Activity activity) {
        StringBuilder out = new StringBuilder();
        out.append("process=").append(process).append(" pid=").append(Process.myPid());
        try {
            out.append(" task=").append(activity.getTaskId());
            out.append(" finishing=").append(activity.isFinishing());
            out.append(" destroyed=").append(activity.isDestroyed());
            out.append(" changingConfig=").append(activity.isChangingConfigurations());
            out.append(" focus=").append(activity.hasWindowFocus());
            if (activity.getWindow() != null) {
                View decor = activity.getWindow().getDecorView();
                out.append(" windowVisibility=").append(decor != null ? decor.getWindowVisibility() : -1);
                out.append(" attached=").append(decor != null && decor.isAttachedToWindow());
            }
        } catch (Throwable t) {
            out.append(" (state partly unreadable: ").append(t).append(')');
        }
        return out.toString();
    }

    private static String pluginState() {
        try {
            if (!guest) {
                String active = ActivePlugin.of(context);
                return "launcher process; :guest holds "
                        + (active == null ? "no plugin (no guest process running)" : (active.isEmpty() ? "<unknown>" : active));
            }
            LoadedPlugin current = GuestRuntime.get().current();
            return "guest process; installed=" + GuestRuntime.get().isInstalled()
                    + " plugin=" + (current != null ? current.packageName : "none loaded")
                    + " liveActivities=" + GuestRuntime.get().isRunning(current != null ? current.packageName : "");
        } catch (Throwable t) {
            return "unavailable: " + t;
        }
    }

    private static String memory() {
        StringBuilder out = new StringBuilder();
        try {
            out.append("java max=").append(mb(Runtime.getRuntime().maxMemory()))
                    .append(" used=").append(mb(Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()))
                    .append(" native=").append(Debug.getNativeHeapAllocatedSize());
            ActivityManager manager = context.getSystemService(ActivityManager.class);
            if (manager != null) {
                ActivityManager.MemoryInfo info = new ActivityManager.MemoryInfo();
                manager.getMemoryInfo(info);
                out.append(" systemAvail=").append(mb(info.availMem))
                        .append(" systemTotal=").append(mb(info.totalMem))
                        .append(" lowMemory=").append(info.lowMemory)
                        .append(" threshold=").append(mb(info.threshold))
                        .append(" memoryClass=").append(manager.getMemoryClass())
                        .append(" lowRamDevice=").append(manager.isLowRamDevice());
            }
        } catch (Throwable t) {
            out.append(" (unreadable: ").append(t).append(')');
        }
        return out.toString();
    }

    // ------------------------------------------------------------------- entry

    static void note(String message) {
        write("NOTE  " + message);
    }

    static void failure(String what, Throwable t) {
        write("ERROR  " + what + ": " + t + "\n" + Log.getStackTraceString(t));
        clip("ERROR " + what + ": " + t);
    }

    /** The closing block: the state of everything, for the moment the app is falling over. */
    private static void summary(String after) {
        StringBuilder out = new StringBuilder();
        out.append("======== SUMMARY ").append(after).append(" ========\n");
        out.append("process     : ").append(process).append(" pid ").append(Process.myPid())
                .append(" uptime ").append(SystemClock.elapsedRealtime() - startedAt).append(" ms\n");
        out.append("last crash  : ").append(crashNearby()).append('\n');
        out.append("plugin      : ").append(pluginState()).append('\n');
        out.append("guest       : ").append(guestState(context)).append('\n');
        out.append("memory      : ").append(memory()).append('\n');
        String report = readReportTail();
        if (report != null && !report.trim().isEmpty()) {
            out.append("guest says  :\n").append(indent(report));
        } else {
            out.append("guest says  : nothing recorded yet\n");
        }
        out.append("full log    : ").append(file != null ? file.getAbsolutePath() : "unavailable");
        write(out.toString());
    }

    private static String guestState(Context app) {
        try {
            return ActivePlugin.guestIsRunning(app) ? "running (pid " + guestPid + ")" : "not running";
        } catch (Throwable t) {
            return "unknown (" + t + ")";
        }
    }

    // --------------------------------------------------------------------- io

    private static void open(Context app) {
        File dir = app.getExternalFilesDir(null);
        if (dir == null) {
            dir = app.getFilesDir();
            Log.w(TAG, "no external files dir: the debug log goes to internal storage");
        }
        file = new File(dir, FILE_NAME);
        try {
            if (file.length() > MAX_BYTES) {
                File old = new File(dir, FILE_NAME + ".1");
                //noinspection ResultOfMethodCallIgnored
                old.delete();
                //noinspection ResultOfMethodCallIgnored
                file.renameTo(old);
            }
            stream = new FileOutputStream(file, true);
        } catch (IOException e) {
            Log.e(TAG, "cannot open " + file, e);
        }
    }

    private static void write(String message) {
        if (main == null || off) return;
        // A record is bounded, so one pathological message (a huge report, a stack of exceptions)
        // can never turn into a multi-megabyte write on the thread that is playing the game.
        if (message.length() > MAX_RECORD_CHARS) {
            message = message.substring(0, MAX_RECORD_CHARS) + "... [" + message.length()
                    + " chars, truncated to " + MAX_RECORD_CHARS + "]";
        }
        String stamped = stamp() + " " + message;
        synchronized (LOCK) {
            // Logcat first: it survives the file being unavailable, and the ROM may keep it.
            Log.i(TAG, stamped);
            tail.addLast(stamped);
            while (tail.size() > TAIL_LINES) tail.removeFirst();
            if (stream == null) return;
            try {
                // No buffering: one write per line means the kernel has the bytes even if a native
                // crash or a SIGKILL follows immediately.
                stream.write((stamped + "\n").getBytes(StandardCharsets.UTF_8));
                stream.flush();
            } catch (IOException e) {
                Log.w(TAG, "debug log write failed: " + e);
            }
        }
    }

    private static String stamp() {
        String time = new SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(new Date());
        String thread = Thread.currentThread().getName();
        return time + " " + process + "/" + thread + "[" + Process.myPid() + "]";
    }

    private static String name(Activity activity) {
        if (activity == null) return "null";
        String simple = activity.getClass().getName();
        return simple + "@" + Integer.toHexString(System.identityHashCode(activity));
    }

    private static String mb(long bytes) {
        return String.format(Locale.US, "%.1fMB", bytes / 1048576.0);
    }

    private static String oneLine(String text) {
        if (text == null) return "nothing recorded";
        return text.replace('\n', ' ').replaceAll("\\s+", " ").trim();
    }

    private static String indent(String text) {
        StringBuilder out = new StringBuilder();
        for (String line : text.split("\n")) out.append("        ").append(line).append('\n');
        return out.toString();
    }

    /** Last resort export for a phone whose logcat is unusable. Best effort by design. */
    private static void clip(String what) {
        try {
            if (off) return;
            ClipboardManager cm = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return;
            StringBuilder out = new StringBuilder(what).append("\n\n--- last ");
            out.append(tail.size()).append(" log lines ---\n");
            synchronized (LOCK) {
                for (String line : tail) out.append(line).append('\n');
            }
            cm.setPrimaryClip(ClipData.newPlainText("Chucny's Translator debug log", out.toString()));
        } catch (Throwable t) {
            Log.w(TAG, "cannot copy the debug log to the clipboard: " + t);
        }
    }

    // The one hook that has to be installed before any other handler, so the chain stays intact.
    static {
        try {
            installedBefore = Thread.getDefaultUncaughtExceptionHandler();
        } catch (Throwable ignored) {
            installedBefore = null;
        }
    }
}
