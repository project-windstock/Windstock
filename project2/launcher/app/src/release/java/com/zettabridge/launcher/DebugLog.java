package com.zettabridge.launcher;

import android.app.Activity;
import android.app.Application;
import android.content.Context;

/**
 * Diagnostic seam between the app and its build variant.
 *
 * <p>The release variant - this file - does nothing at all: the calls below are empty on purpose,
 * they exist so the shipping APK keeps no narration and no second log file. The debug variant
 * ({@code app/src/debug/java/com/zettabridge/launcher/DebugLog.java}) replaces this class with one
 * that logs the whole run to logcat <em>and</em> to
 * {@code /sdcard/Android/data/com.chucny.translator.bridge/files/zb-app-log.txt}, and that writes
 * the reason a window closed or a process died. Same class name, same package - the variant source
 * set wins over {@code main}, so neither build compiles the other's copy.
 *
 * <p>Do not add a body here. Anything this file does happens in the obfuscated build too.
 */
final class DebugLog {

    private DebugLog() {}

    /**
     * False in this variant: there is nothing to switch, because the log is compiled out rather
     * than switched off. The settings screen reads this to say so instead of offering a button
     * that cannot do anything.
     */
    static boolean available() {
        return false;
    }

    /** Always false here: this build writes no app log. */
    static boolean enabled(Context context) {
        return false;
    }

    /** Stored anyway, so the value is there if this phone is ever handed the debuggable build. */
    static void setEnabled(Context context, boolean enabled) {
        LauncherSettings.appLog(context, enabled);
    }

    /** Called once per process, from {@link ZbApplication#onCreate}. */
    static void install(Application app) {
    }

    /**
     * Called for every activity the runtime hands over: the plugin activities the guest
     * instrumentation instantiates, the manifest stubs, and the launcher's own screens.
     */
    static void attach(Activity activity) {
    }

    /**
     * Called when an activity is destroyed, from both processes. The debug copy explains why: a
     * deliberate {@code finish()}, a configuration rebuild, or the system tearing the window down -
     * which is where a crash or a low-memory kill shows up.
     */
    static void windowGone(Activity activity, String trigger) {
    }

    /** A one-line narration of something the runtime just did. */
    static void note(String message) {
    }

    /** An exception that was handled, or that ended a launch. */
    static void failure(String what, Throwable t) {
    }

    /** Called where the runtime ends the :guest process on purpose, so that is not read as a crash. */
    static void noteGuestKill() {
    }
}
