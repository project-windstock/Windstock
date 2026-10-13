package com.zettabridge.launcher;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

/**
 * Keeps the :guest main thread alive when a bundled Google Play Services client library throws.
 *
 * A plugin runs under the launcher's package and signature, so Google Play services answers its
 * old client libraries (sign-in, games, ads) with DEVELOPER_ERROR, and some of them throw
 * IllegalStateException on the main looper, which kills the game. Those services cannot work for
 * a plugin anyway (accounts are a non-goal), so such exceptions are recorded and swallowed.
 * Everything else is rethrown unchanged.
 */
final class MainLooperGuard {
    private static final String TAG = "Translator";
    private static final String[] SWALLOWED_PREFIXES = {"com.google.android.gms.", "com.google.ads."};

    private MainLooperGuard() {}

    static void install(Context context) {
        final Context app = context.getApplicationContext() != null ? context.getApplicationContext() : context;
        new Handler(Looper.getMainLooper()).post(() -> {
            // Re-enter the loop from inside a message: an exception thrown by a later message now
            // unwinds to here instead of ActivityThread.main.
            while (true) {
                try {
                    Looper.loop();
                    return;  // the looper quit
                } catch (Throwable t) {
                    if (!swallowed(t)) throw t;
                    DebugLog.failure("swallowed a Google Play services exception instead of letting it kill the game", t);
                    Diagnostics.report(app, "swallowed a Google Play services exception on the main thread", t, false);
                    Log.w(TAG, "main looper guard: continuing after " + t);
                }
            }
        });
    }

    private static boolean swallowed(Throwable t) {
        StackTraceElement[] stack = t.getStackTrace();
        if (stack.length == 0) return false;
        String thrower = stack[0].getClassName();
        for (String prefix : SWALLOWED_PREFIXES) {
            if (thrower.startsWith(prefix)) return true;
        }
        return false;
    }
}
