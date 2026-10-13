package com.zettabridge.launcher;

import android.content.Context;
import android.os.Looper;
import android.util.Log;

import java.util.concurrent.atomic.AtomicInteger;

/**
 * Applies {@link CrashPolicy} to uncaught exceptions, in both processes, and records every one of
 * them before anything else happens.
 *
 * <p>Installed last of the per-process setup, so it sits at the top of the handler chain: whatever
 * the debug build and the framework installed below it still sees everything this guard passes
 * down, and nothing this guard swallows reaches them. The chain is the mechanism - when the policy
 * says the process must not die, this handler simply returns, which is what stops the default
 * handler (the one that calls {@code Process.killProcess}) from ever running.
 *
 * <p>Recording comes first and unconditionally: an exception that is survived is exactly the kind
 * that would otherwise leave no trace anywhere, since nothing crashes and nothing is shown.
 *
 * <p>The guard's own body never throws. A handler that fails while handling a crash turns a
 * survivable failure into a fatal one, and it would be doing so in the one place where the app has
 * no options left.
 */
final class CrashGuard {
    private static final String TAG = "Translator";

    private static final AtomicInteger swallowed = new AtomicInteger();

    private CrashGuard() {}

    /** How many uncaught exceptions this process has survived, for the log and the policy. */
    static int swallowedCount() {
        return swallowed.get();
    }

    /**
     * Installs the handler. Call once per process, from {@link ZbApplication#onCreate}.
     *
     * @param guest true when this is the :guest process, where a game is running
     */
    static void install(Context context, boolean guest) {
        final Context app = context.getApplicationContext() != null ? context.getApplicationContext() : context;
        final Thread.UncaughtExceptionHandler previous = Thread.getDefaultUncaughtExceptionHandler();
        try {
            Thread.setDefaultUncaughtExceptionHandler((thread, t) -> {
                boolean fatal = true;
                try {
                    boolean mainThread = Looper.getMainLooper() != null
                            && Looper.getMainLooper().getThread() == thread;
                    boolean survives = CrashPolicy.survives(guest, mainThread, swallowed.get());
                    // Recorded first, and always: a survived exception leaves no other trace.
                    Diagnostics.recordUncaught(app, thread, t);
                    Log.e(TAG, "uncaught in " + thread.getName() + ": " + CrashPolicy.explain(
                            guest, mainThread, swallowed.get(), survives), t);
                    fatal = !survives;
                    if (survives) {
                        swallowed.incrementAndGet();
                        DebugLog.failure("uncaught exception survived: " + thread.getName()
                                + " threw, and the game keeps running", t);
                    }
                } catch (Throwable inner) {
                    // Cannot even decide: fall through to the normal handler, which is the safest
                    // of the two remaining options.
                    Log.e(TAG, "crash guard failed while handling a crash", inner);
                    fatal = true;
                }
                if (fatal && previous != null) previous.uncaughtException(thread, t);
            });
        } catch (Throwable t) {
            Log.e(TAG, "cannot install the crash guard", t);
        }
    }
}
