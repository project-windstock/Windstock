package com.zettabridge.launcher;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/**
 * The decision a dead :guest process gets: reopen the game, or leave it alone.
 *
 * <p>Deliberately free of Android: this is the part of recovery that must be right, and the only way
 * to check it without a phone is to call it directly (tests/launcher covers the whole table). The
 * policy is written out as data - the states below are mutually exclusive and ordered, so the first
 * one that matches is the answer, and the reason travels with the decision so a log line and the
 * screen a user sees cannot disagree about what happened.
 *
 * <p>The shape of the problem: a process that dies takes its window with it and leaves no exception,
 * no stack trace and no callback. The launcher sees only that the lease lapsed. Three outcomes have
 * to be told apart, with a restart being the expensive and intrusive one:
 * <ul>
 *   <li>the guest is fine, or has not started yet - wait;</li>
 *   <li>the guest is gone and the user was watching a game - reopen it, but a bounded number of
 *       times, because a plugin that dies for a real reason dies again a second later, and a
 *       supervisor that keeps trying turns one crash into an endless launch loop;</li>
 *   <li>the guest is gone and the user was not watching - do nothing. This is the case a naive
 *       watchdog gets wrong: it drags people back into a game they closed, or into one the system
 *       reclaimed while they were elsewhere.</li>
 * </ul>
 */
final class GuestRecovery {
    private GuestRecovery() {}

    /** What to do next. */
    enum Action {
        /** Nothing happens now; the supervisor keeps watching. */
        WAIT,
        /** The guest is gone and a window was on screen: open the plugin again. */
        RESTART,
        /** Nothing is wrong, or nothing can be done: stop watching. */
        STOP,
        /** It died too often: stop watching and say so on screen. */
        GIVE_UP
    }

    /**
     * How long a launch is given before its absence is treated as a death. The first launch of a
     * fresh install imports the bundled app's APK before any process of the plugin exists, which can
     * take far longer than this; the cost of guessing wrong is one run without supervision (the
     * launch path itself reports that kind of failure), which is why this stays short rather than
     * covering the worst case.
     */
    static final long START_GRACE_MS = 20_000;

    /** Restarts are counted in a sliding window, so an all-day session can recover more than three times. */
    static final long WINDOW_MS = 10 * 60_000;

    /** How many reopenings one window of time may contain before the user is asked instead. */
    static final int MAX_RESTARTS = 3;

    /** Backoff by restart number: the first is immediate, the next ones leave the system room. */
    private static final long[] BACKOFF_MS = {0, 2_000, 5_000};

    /** The facts, gathered by the supervisor in the launcher process. */
    static final class Facts {
        /** The :guest process is running (process list, or the lease it holds). */
        boolean alive;
        /** The guest wrote an exit marker for this run: it left on purpose. */
        boolean cleanExit;
        /** A plugin window was up or coming up when it ended. */
        boolean windowWasUp;
        /** The guest has been seen alive at least once since watching began. */
        boolean everAlive;
        /** How long the supervisor has been watching. */
        long sinceWatchStartMs;
        /** Restarts made inside the window. */
        int restarts;
        /** The plugin this watch belongs to, for the message. */
        String plugin;
    }

    /** The answer, with the reason and the delay a caller needs to carry it out. */
    static final class Decision {
        final Action action;
        final long delayMs;
        final String reason;

        Decision(Action action, long delayMs, String reason) {
            this.action = action;
            this.delayMs = delayMs;
            this.reason = reason;
        }

        @Override
        public String toString() {
            return action + (delayMs > 0 ? " in " + delayMs + " ms" : "") + ": " + reason;
        }
    }

    private static Decision decide(Action action, String reason) {
        return new Decision(action, 0, reason);
    }

    /** The order of these tests is the policy; see the class comment. */
    static Decision decide(Facts facts) {
        if (facts.alive) {
            return decide(Action.WAIT, "the :guest process is running");
        }
        if (!facts.everAlive && facts.sinceWatchStartMs < START_GRACE_MS) {
            return decide(Action.WAIT, "the :guest process has not started yet");
        }
        if (!facts.everAlive) {
            return decide(Action.STOP, "the :guest process never started in " + START_GRACE_MS
                    + " ms; that is a launch failure, which the launch path reports itself");
        }
        if (facts.cleanExit) {
            return decide(Action.STOP, "the app closed its last window itself");
        }
        if (!facts.windowWasUp) {
            return decide(Action.STOP, "no game window was on screen when the process ended"
                    + " (the game was in the background, or the task was removed)");
        }
        if (facts.restarts >= MAX_RESTARTS) {
            return decide(Action.GIVE_UP, "the app stopped " + facts.restarts
                    + " times in " + (WINDOW_MS / 60_000) + " minutes, so it is not going to stay open"
                    + " by itself; the user has to decide");
        }
        long delay = BACKOFF_MS[Math.min(facts.restarts, BACKOFF_MS.length - 1)];
        return new Decision(Action.RESTART, delay, "the app stopped while its window was"
                + " on screen (restart " + (facts.restarts + 1) + " of " + MAX_RESTARTS + ")");
    }

    /**
     * When the restarts happened, so the cap survives the launcher process being restarted itself -
     * which happens routinely, because the system is free to reclaim an app whose game is playing in
     * another process of the same app. Kept as text for SharedPreferences; pure on purpose, so the
     * window arithmetic is covered by the same host test as the table above.
     */
    static final class History {
        private final ArrayDeque<Long> restarts = new ArrayDeque<>();

        /** Records a restart made now, and drops the ones that have fallen out of the window. */
        void record(long nowMs) {
            restarts.addLast(nowMs);
            prune(nowMs);
        }

        /** How many restarts are inside the window. */
        int count(long nowMs) {
            prune(nowMs);
            return restarts.size();
        }

        /** A restart that already happened when this process started, e.g. after a load. */
        void restore(List<Long> whenMs, long nowMs) {
            restarts.clear();
            for (Long when : whenMs) {
                if (when != null) restarts.addLast(when);
            }
            prune(nowMs);
        }

        /** The timestamps still inside the window, oldest first. */
        List<Long> timestamps(long nowMs) {
            prune(nowMs);
            return new ArrayList<>(restarts);
        }

        private void prune(long nowMs) {
            while (!restarts.isEmpty() && nowMs - restarts.peekFirst() > WINDOW_MS) {
                restarts.removeFirst();
            }
        }

        /** Comma-separated epoch milliseconds; anything unreadable decodes to an empty history. */
        static String encode(List<Long> whenMs) {
            StringBuilder out = new StringBuilder();
            for (Long when : whenMs) {
                if (when == null) continue;
                if (out.length() > 0) out.append(',');
                out.append(when);
            }
            return out.toString();
        }

        static List<Long> decode(String text) {
            List<Long> when = new ArrayList<>();
            if (text == null) return when;
            for (String part : text.split(",")) {
                String trimmed = part.trim();
                if (trimmed.isEmpty()) continue;
                try {
                    when.add(Long.parseLong(trimmed));
                } catch (NumberFormatException ignored) {
                    // A corrupt entry is dropped; the worst case is one restart more than the cap.
                }
            }
            return when;
        }

        /** A wall clock that cannot go backwards, for the arithmetic above. */
        static long now() {
            return System.currentTimeMillis();
        }
    }
}
