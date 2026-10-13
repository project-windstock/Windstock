package com.zettabridge.launcher;

import android.content.Context;
import android.os.Process;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;

/**
 * What the launcher process can learn about the :guest process from outside it.
 *
 * <p>Two questions decide whether a dead guest should be restarted, and neither can be answered
 * from the launcher's own memory:
 * <ol>
 *   <li><b>was it alive?</b> - {@link GuestLease}, a lock the guest holds and its death releases;</li>
 *   <li><b>was a game on screen?</b> - this class. A phase file written at each meaningful step
 *       (started, plugin loaded, window resumed, window gone, closed). Without it, a guest killed
 *       while the user was looking at the home screen - or one the user swiped away - is
 *       indistinguishable from one that died mid-game, and a supervisor acting on that would drag
 *       people back into a game they deliberately left.</li>
 * </ol>
 *
 * <p>The phase is written to a file rather than a field because the process that needs it is the
 * one that outlives the guest, and because a write at each of five lifecycle events costs nothing:
 * the atomically renamed file is a few dozen bytes, written on transitions only, never in a loop,
 * and never on a frame.
 *
 * <p>This is a stability mechanism, so it ships in the release build: the launcher process uses it
 * to decide whether to reopen a game that stopped. Everything is best effort - a phone whose
 * internal storage refuses these writes loses recovery, not the game.
 */
final class GuestHeartbeat {
    private static final String TAG = "Translator";
    static final String PHASE_FILE = "guest-phase.txt";

    /** The process started; nothing is loaded yet. */
    static final String STARTING = "starting";
    /** The runtime is installed and a plugin was loaded, but no window of it has resumed. */
    static final String LOADED = "loaded";
    /** A plugin window is on screen: the user is looking at the game. */
    static final String VISIBLE = "visible";
    /** Windows exist but none is resumed - the game is behind something, or the task is going away. */
    static final String BACKGROUND = "background";
    /** The plugin finished its last window itself: a deliberate close, not a crash. */
    static final String CLOSED = "closed";

    private GuestHeartbeat() {}

    /** The guest's private directory; the same one the lease lives in. */
    static File dir(Context context) {
        try {
            return context.getFilesDir();
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * Begins a run: takes the lease and forgets the previous run's exit marker. Called from the
     * :guest process as it starts and again on every launch, because a process that closed one game
     * can be handed another without being restarted.
     *
     * @return true when this process now holds the lease
     */
    static boolean begin(Context context) {
        File dir = dir(context);
        boolean held = GuestLease.acquire(dir, "pid " + Process.myPid() + " :guest");
        if (!held) Log.w(TAG, "the :guest process could not take its lease; the launcher cannot tell a crash from an exit");
        phase(context, STARTING, null);
        return held;
    }

    /** Records the phase, with the plugin and pid so a reader does not have to guess whose it is. */
    static void phase(Context context, String phase, String plugin) {
        File dir = dir(context);
        if (dir == null) return;
        File file = new File(dir, PHASE_FILE);
        File temp = new File(dir, PHASE_FILE + ".new");
        String line = phase + " " + System.currentTimeMillis() + " pid=" + Process.myPid()
                + (plugin == null ? "" : " plugin=" + plugin) + "\n";
        try (FileOutputStream out = new FileOutputStream(temp)) {
            out.write(line.getBytes(StandardCharsets.UTF_8));
        } catch (IOException | RuntimeException e) {
            Log.w(TAG, "cannot record the guest phase: " + e);
            return;
        }
        try {
            // Rename so a reader never sees a half-written phase: the launcher may read this the
            // instant the process dies, which is exactly when a partial write is possible.
            Files.move(temp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException | RuntimeException e) {
            Log.w(TAG, "cannot publish the guest phase: " + e);
        }
    }

    /** The last phase written by the guest, or null when it never wrote one. */
    static String phase(Context context) {
        File dir = dir(context);
        if (dir == null) return null;
        try {
            String text = new String(Files.readAllBytes(new File(dir, PHASE_FILE).toPath()),
                    StandardCharsets.UTF_8).trim();
            if (text.isEmpty()) return null;
            int space = text.indexOf(' ');
            return space < 0 ? text : text.substring(0, space);
        } catch (IOException | RuntimeException e) {
            return null;
        }
    }

    /**
     * True when the phase says a window of the plugin was up or was on its way up - which is the
     * only situation in which a death is worth a restart. A missing phase (the process died before
     * it could write one) counts as a window in progress: the user was watching a task that
     * vanished.
     */
    static boolean windowWasUp(Context context) {
        String phase = phase(context);
        if (phase == null) return true;
        return STARTING.equals(phase) || LOADED.equals(phase) || VISIBLE.equals(phase);
    }

    /** The guest left on purpose: give up the lease and say so. See {@link GuestLease#markExit}. */
    static void cleanExit(Context context, String reason) {
        phase(context, CLOSED, null);
        GuestLease.markExit(dir(context), reason);
    }

    /** True when the running guest recorded a deliberate exit. */
    static boolean cleanExitRecorded(Context context) {
        return GuestLease.exitReason(dir(context)) != null;
    }

    /** The deliberate exit's own words, for the log and the recovery screen. */
    static String exitReason(Context context) {
        return GuestLease.exitReason(dir(context));
    }
}
