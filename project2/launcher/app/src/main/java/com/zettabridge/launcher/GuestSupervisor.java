package com.zettabridge.launcher;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

/**
 * Watches the :guest process and reopens the game when it dies with a window on screen.
 *
 * <p>Why a service in the launcher process. A game that dies by native fault, by {@code SIGKILL} or
 * by an out-of-memory kill runs no Java on the way out: there is no callback, no exception and no
 * window left to explain anything, so nothing inside the guest can react. What still exists is this
 * process - and the task it belongs to, which is the one the game was in - so the recovery has to
 * live here, deliberately outside the thing that dies. It is a started {@link Service} rather than a
 * thread of the library screen for one reason: a thread in a process with no components is a thread
 * the system is free to reclaim, and the launcher's own process legitimately has no visible activity
 * while a game is fullscreen.
 *
 * <p>What it costs: one thread that sleeps {@value #POLL_MS} ms between two reads - a process-list
 * query and, if that says nothing, a file-lock probe - and a byte or two written on each plugin
 * window transition. It never runs anything on the main thread except the two activity starts, and
 * it does nothing at all to a running guest: no polling of the guest, no JNI, no report. (An earlier
 * diagnostic build polled the bridge from this process; calling into the runtime that owns the
 * guest's threads is what crashed the game then, and nothing here goes near that path.)
 *
 * <p>The decision itself is {@link GuestRecovery} - pure, and covered by a host test - so this class
 * is only the machinery: gather the facts, act on the answer, and leave a record either way.
 */
public class GuestSupervisor extends Service {
    private static final String TAG = "Translator";
    /** Which plugin this watch belongs to. */
    static final String EXTRA_PACKAGE = "com.chucny.translator.bridge.SUPERVISED";
    private static final String PREFS = "translator.watch";
    private static final String KEY_RESTARTS = "restarts";
    /** Frequent enough to feel immediate when it matters - and free when it does not. */
    private static final long POLL_MS = 500;
    /**
     * The shortest gap between two reopenings, whatever the backoff says. Without it, a guest that
     * dies during its own start-up would burn the whole allowance inside two seconds and end at the
     * give-up screen, where a half-second pause and one more attempt would have opened the game.
     */
    private static final long MIN_GAP_MS = 2_000;

    /**
     * Starts watching, for the plugin about to be opened. Called from the launcher's own screen
     * while it is visible, which is also why a plain {@code startService} is allowed here: a
     * background start is refused from Android 8 on, but this one comes from an activity the user is
     * looking at. Failure to start is not fatal - the game runs without a net.
     */
    static void watch(Context context, String packageName) {
        if (packageName == null) return;
        try {
            context.startService(new Intent(context, GuestSupervisor.class).putExtra(EXTRA_PACKAGE, packageName));
        } catch (Throwable t) {
            Log.w(TAG, "cannot start the guest supervisor: " + t);
        }
    }

    /** Stops watching. Called where the launcher ends the guest on purpose, so it is not a crash. */
    static void stop(Context context) {
        try {
            context.stopService(new Intent(context, GuestSupervisor.class));
        } catch (Throwable t) {
            Log.w(TAG, "cannot stop the guest supervisor: " + t);
        }
    }

    private volatile boolean running;
    private Thread thread;
    private Handler main;
    private String plugin;
    private long watchStartMs;
    private boolean everAlive;
    private boolean restartIssued;
    /** Wall-clock-independent instant before which no reopening may happen. */
    private long restartNotBeforeMs;
    private GuestRecovery.History history;

    @Override
    public void onCreate() {
        super.onCreate();
        main = new Handler(Looper.getMainLooper());
        history = new GuestRecovery.History();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String requested = intent != null ? intent.getStringExtra(EXTRA_PACKAGE) : null;
        if (requested == null) {
            DebugLog.note("guest supervisor started with no plugin name: nothing to watch");
            stopSelf();
            return START_NOT_STICKY;
        }
        plugin = requested;
        // A new watch: this launch gets its own grace period and its own answer about being alive.
        watchStartMs = SystemClock.elapsedRealtime();
        everAlive = false;
        restartIssued = false;
        restartNotBeforeMs = 0;
        long now = GuestRecovery.History.now();
        history.restore(GuestRecovery.History.decode(prefs().getString(KEY_RESTARTS, "")), now);
        DebugLog.note("guest supervisor watching " + plugin + " (" + history.count(now) + " of "
                + GuestRecovery.MAX_RESTARTS + " reopenings already used in this window)");
        start();
        return START_NOT_STICKY;
    }

    /**
     * The user removed the task: the game is not wanted any more, so no death that follows it is
     * worth reopening. This is the one signal that is never wrong about intent.
     */
    @Override
    public void onTaskRemoved(Intent rootIntent) {
        DebugLog.note("the task was removed while the supervisor was watching " + plugin
                + ": not reopening it");
        Diagnostics.recovery(this, "the task was removed, so a later :guest death is not being"
                + " reopened (this is the user closing the app, not a crash)");
        stopSelf();
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        stop();
        super.onDestroy();
    }

    private synchronized void start() {
        if (thread != null) return;
        running = true;
        thread = new Thread(this::watchLoop, "TranslatorGuestSupervisor");
        thread.setDaemon(true);
        thread.start();
    }

    private synchronized void stop() {
        running = false;
        if (thread != null) {
            thread.interrupt();
            thread = null;
        }
    }

    /**
     * The loop. Everything it does is wrapped: a supervisor that throws is a supervisor that stops
     * watching, and it must never be the reason a game is left dead on screen.
     */
    private void watchLoop() {
        while (running) {
            try {
                tick();
            } catch (Throwable t) {
                Log.w(TAG, "guest supervisor tick failed", t);
                Diagnostics.recovery(this, "the guest supervisor failed a check: " + t);
            }
            try {
                Thread.sleep(POLL_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    private void tick() {
        long elapsed = SystemClock.elapsedRealtime();
        boolean alive = ActivePlugin.guestIsRunning(this) || GuestLease.isHeld(GuestHeartbeat.dir(this));

        if (alive) {
            everAlive = true;
            if (restartIssued) {
                restartIssued = false;
                restartNotBeforeMs = 0;
                DebugLog.note("the reopened " + plugin + " is running again");
            }
        }

        GuestRecovery.Facts facts = new GuestRecovery.Facts();
        facts.alive = alive;
        facts.cleanExit = GuestHeartbeat.cleanExitRecorded(this);
        facts.windowWasUp = GuestHeartbeat.windowWasUp(this);
        facts.everAlive = everAlive;
        facts.sinceWatchStartMs = elapsed - watchStartMs;
        facts.restarts = history.count(GuestRecovery.History.now());
        facts.plugin = plugin;

        GuestRecovery.Decision decision = GuestRecovery.decide(facts);
        switch (decision.action) {
            case WAIT:
                return;
            case STOP:
                DebugLog.note("guest supervisor stopping: " + decision.reason);
                Diagnostics.recovery(this, "stopped watching " + plugin + ": " + decision.reason);
                stopSelf();
                return;
            case GIVE_UP:
                Diagnostics.recovery(this, "gave up on " + plugin + ": " + decision.reason);
                DebugLog.failure("gave up reopening " + plugin, new IllegalStateException(decision.reason));
                tell("The app keeps stopping", plugin + " stopped " + facts.restarts + " times in "
                        + (GuestRecovery.WINDOW_MS / 60_000) + " minutes and could not be kept open."
                        + " Reopening it again would almost certainly stop again, so it is not being"
                        + " reopened automatically.\n\nWhy it stopped last time: " + deathEvidence(this)
                        + "\n\nThe reason it is now stopped is written to files/zb-errors.txt.");
                stopSelf();
                return;
            case RESTART:
                // The gate, not a sleep: the loop keeps checking, so a guest that comes back early
                // is noticed at once, and a repeat restart waits out its backoff without blocking
                // the thread that would have seen the recovery.
                if (restartNotBeforeMs != 0 && elapsed < restartNotBeforeMs) return;
                reopen(decision);
                return;
            default:
                return;
        }
    }

    private void reopen(GuestRecovery.Decision decision) {
        long now = GuestRecovery.History.now();
        history.record(now);
        prefs().edit().putString(KEY_RESTARTS, GuestRecovery.History.encode(history.timestamps(now))).apply();
        restartIssued = true;
        restartNotBeforeMs = SystemClock.elapsedRealtime() + Math.max(decision.delayMs, MIN_GAP_MS);
        DebugLog.failure("reopening " + plugin + " after it stopped", new IllegalStateException(decision.reason));
        Diagnostics.recovery(this, "reopening " + plugin + ": " + decision.reason
                + "\n  how it ended: " + deathEvidence(this));
        final String target = plugin;
        main.post(() -> {
            try {
                startActivity(PluginSwitchActivity.intent(this, target));
            } catch (Throwable t) {
                // A background activity start can be refused by the platform (Android 10+). If that
                // happens the game cannot be reopened from here at all, and the honest fallback is
                // the launcher's own library screen, which the user opens from the home screen.
                Log.w(TAG, "cannot reopen " + target + " automatically", t);
                DebugLog.failure("cannot reopen " + target + " automatically; the user has to open it", t);
                Diagnostics.recovery(this, "cannot reopen " + target + " automatically: " + t
                        + " (a background activity launch may have been refused; open the app from"
                        + " the library)");
            }
        });
    }

    /** Shows a screen in this process - it has a window, and it stays until the user acts. */
    private void tell(String title, String detail) {
        final String target = plugin;
        main.post(() -> {
            try {
                startActivity(LaunchFailureActivity.intent(this, target, title, detail, false, false));
            } catch (Throwable t) {
                Log.w(TAG, "cannot show the recovery screen", t);
            }
        });
    }

    /**
     * What is known about how the guest ended, for the record: the phase it was in, whether it left a
     * deliberate-exit marker, and the runtime's own account if it wrote one. Read from files only -
     * never by asking the runtime, which is the mistake this codebase made once already.
     */
    static String deathEvidence(Context context) {
        StringBuilder out = new StringBuilder();
        String phase = GuestHeartbeat.phase(context);
        out.append("phase ").append(phase == null ? "unknown (no phase recorded)" : phase);
        String exit = GuestHeartbeat.exitReason(context);
        out.append(exit == null ? "; no deliberate exit recorded" : "; deliberate exit: " + exit);
        String report = Diagnostics.readRuntimeReport(context);
        if (report == null || report.isEmpty()) {
            out.append("; the runtime wrote no report");
            return out.toString();
        }
        String exitLine = null;
        for (String line : report.split("\n")) {
            String trimmed = line.trim();
            if (trimmed.startsWith("guest-exit:") || trimmed.startsWith("signal")) exitLine = trimmed;
        }
        out.append(exitLine == null ? "; the runtime report has no exit line"
                : "; the runtime recorded \"" + exitLine + "\"");
        return out.toString();
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }
}
