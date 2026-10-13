package com.zettabridge.launcher;

import android.os.Build;
import android.util.Log;

import org.lsposed.hiddenapibypass.HiddenApiBypass;

/**
 * Lifts hidden API enforcement for this process. The :guest hooks touch ActivityThread,
 * Instrumentation.execStartActivity and ContextWrapper/ContextThemeWrapper/Window fields.
 * Meta-reflection stopped working in Android 11, so this uses LSPosed's HiddenApiBypass
 * (Unsafe-based, no root, works through Android 16). Most members used here are on the
 * "unsupported" greylist anyway, so a failure is logged and we carry on.
 */
final class HiddenApi {
    private static final String TAG = "Translator";
    private static boolean done;

    private HiddenApi() {}

    static synchronized void exemptAll() {
        if (done) return;
        done = true;
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return;  // no enforcement before 9
        try {
            boolean ok = HiddenApiBypass.addHiddenApiExemptions("L");
            Log.i(TAG, "hidden API exemptions installed: " + ok);
        } catch (Throwable t) {
            Log.w(TAG, "hidden API bypass failed; greylisted members may still work", t);
        }
    }
}
