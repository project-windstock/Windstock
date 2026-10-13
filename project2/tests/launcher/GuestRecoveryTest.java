package com.zettabridge.launcher;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Host-side tests for the stability layer, run by run_stability_tests.sh with plain {@code java} -
 * no device, no emulator, no Android SDK.
 *
 * <p>What is worth testing without a phone is exactly what a phone cannot show you: the decision
 * table (a wrong answer is a game that vanishes or a restart loop), the restart arithmetic that has
 * to survive this process being restarted itself, and the one property the whole design rests on -
 * that a file lock is released by the death of the process holding it. The last one is tested for
 * real: a second JVM takes the lease and is killed, and this process must then be able to take it.
 * A simulated "the other process died" would prove nothing, since the property being relied on is
 * the kernel's, not Java's.
 */
public final class GuestRecoveryTest {
    private static int checked;

    private static void check(boolean condition, String message) {
        checked++;
        if (!condition) throw new AssertionError(message);
    }

    private static GuestRecovery.Facts facts(boolean alive, boolean cleanExit, boolean windowWasUp,
                                             boolean everAlive, long sinceWatchStartMs, int restarts) {
        GuestRecovery.Facts facts = new GuestRecovery.Facts();
        facts.alive = alive;
        facts.cleanExit = cleanExit;
        facts.windowWasUp = windowWasUp;
        facts.everAlive = everAlive;
        facts.sinceWatchStartMs = sinceWatchStartMs;
        facts.restarts = restarts;
        facts.plugin = "com.example.game";
        return facts;
    }

    private static void testDecisionTable() {
        GuestRecovery.Decision d = GuestRecovery.decide(facts(true, false, true, true, 60_000, 0));
        check(d.action == GuestRecovery.Action.WAIT, "a running guest is left alone, was " + d.action);
        check(d.delayMs == 0, "waiting costs no delay");

        d = GuestRecovery.decide(facts(false, false, true, false, 1_000, 0));
        check(d.action == GuestRecovery.Action.WAIT, "a guest that has not started yet is a wait");

        d = GuestRecovery.decide(facts(false, false, true, false, GuestRecovery.START_GRACE_MS + 1, 0));
        check(d.action == GuestRecovery.Action.STOP,
                "a guest that never started is a launch failure, not a crash: " + d.action);

        d = GuestRecovery.decide(facts(false, true, true, true, 60_000, 0));
        check(d.action == GuestRecovery.Action.STOP, "a deliberate exit is not reopened");

        d = GuestRecovery.decide(facts(false, false, false, true, 60_000, 0));
        check(d.action == GuestRecovery.Action.STOP,
                "a death with no window on screen is not reopened: " + d.action);

        d = GuestRecovery.decide(facts(false, false, true, true, 60_000, 0));
        check(d.action == GuestRecovery.Action.RESTART, "a death mid-game is reopened: " + d.action);
        check(d.delayMs == 0, "the first reopening is immediate");

        d = GuestRecovery.decide(facts(false, false, true, true, 60_000, 1));
        check(d.action == GuestRecovery.Action.RESTART && d.delayMs == 2_000,
                "the second reopening waits, was " + d);

        d = GuestRecovery.decide(facts(false, false, true, true, 60_000, 2));
        check(d.action == GuestRecovery.Action.RESTART && d.delayMs == 5_000,
                "the third reopening waits longer, was " + d);

        d = GuestRecovery.decide(facts(false, false, true, true, 60_000, GuestRecovery.MAX_RESTARTS));
        check(d.action == GuestRecovery.Action.GIVE_UP, "the cap ends the loop, was " + d.action);
        check(d.reason.contains(String.valueOf(GuestRecovery.MAX_RESTARTS)),
                "the give-up reason says how often it stopped: " + d.reason);

        // The order of the rules matters as much as the rules: a clean exit recorded by a previous
        // run must not turn into a reopening just because a window later looked like it was up.
        d = GuestRecovery.decide(facts(false, true, true, true, 0, 0));
        check(d.action == GuestRecovery.Action.STOP, "a deliberate exit outranks a window in progress");
    }

    private static void testHistory() {
        GuestRecovery.History history = new GuestRecovery.History();
        long t0 = 1_000_000_000_000L;
        check(history.count(t0) == 0, "a fresh history is empty");
        check(history.encode(history.timestamps(t0)).isEmpty(), "an empty history encodes empty");

        history.record(t0);
        history.record(t0 + 60_000);
        check(history.count(t0 + 60_000) == 2, "two restarts are in the window");

        String encoded = GuestRecovery.History.encode(history.timestamps(t0 + 60_000));
        List<Long> decoded = GuestRecovery.History.decode(encoded);
        check(decoded.size() == 2 && decoded.get(0) == t0 && decoded.get(1) == t0 + 60_000,
                "the history survives being written down and read back: " + encoded);

        check(GuestRecovery.History.decode("").isEmpty(), "empty text decodes to nothing");
        check(GuestRecovery.History.decode("not-a-number,5").size() == 1,
                "a corrupt entry is dropped rather than ending the watch");
        check(GuestRecovery.History.decode(null).isEmpty(), "missing text decodes to nothing");

        // The window's edge is inclusive: a restart exactly WINDOW_MS ago still counts, which is the
        // conservative reading (it can only ever give up one restart early, never loop).
        check(history.count(t0 + 60_000 + GuestRecovery.WINDOW_MS) == 1,
                "a restart exactly at the window's edge still counts");

        // Past the window the restarts are gone, so an all-day session can recover more than the cap.
        long late = t0 + GuestRecovery.WINDOW_MS * 2 + 120_000;
        check(history.count(late) == 0, "restarts outside the window do not count any more");
        check(history.timestamps(late).isEmpty(), "and are not carried in the encoded history");

        GuestRecovery.History restored = new GuestRecovery.History();
        restored.restore(GuestRecovery.History.decode(encoded), t0 + 60_000);
        check(restored.count(t0 + 60_000) == 2,
                "a watch that starts after a launcher restart remembers the reopenings already made");
        restored.restore(GuestRecovery.History.decode(encoded), late);
        check(restored.count(late) == 0, "and forgets the ones that have aged out");
    }

    private static void testCrashPolicy() {
        check(CrashPolicy.survives(true, false, 0),
                "a worker thread throwing in the guest does not end the game");
        check(CrashPolicy.survives(true, false, CrashPolicy.MAX_SWALLOWED - 1),
                "the last survivable exception is still survived");
        check(!CrashPolicy.survives(true, false, CrashPolicy.MAX_SWALLOWED),
                "past the limit the process is handed over instead of kept");
        check(!CrashPolicy.survives(true, true, 0),
                "a main-thread crash is never swallowed here: its looper is already unwinding");
        check(!CrashPolicy.survives(false, false, 0),
                "the launcher's own process behaves normally");
        for (boolean guest : new boolean[] {true, false}) {
            for (boolean main : new boolean[] {true, false}) {
                for (int swallowed : new int[] {0, CrashPolicy.MAX_SWALLOWED}) {
                    boolean survives = CrashPolicy.survives(guest, main, swallowed);
                    String why = CrashPolicy.explain(guest, main, swallowed, survives);
                    check(why != null && why.length() > 20, "every decision is explained: " + why);
                }
            }
        }
    }

    private static void testLease(File dir) throws Exception {
        // A deliberate exit is recorded, read back, and forgotten at the start of the next run.
        GuestLease.markExit(dir, "the test said so");
        check(GuestLease.exitReason(dir) != null, "an exit marker is readable");
        check(GuestLease.exitReason(dir).contains("the test said so"), "the marker carries its reason");
        check(GuestLease.acquire(dir, "this JVM"), "the lease is free once the guest gave it up");
        check(GuestLease.exitReason(dir) == null, "starting a new run forgets the previous exit");
        check(GuestLease.isHeld(dir), "a held lease reads as held, even from the process holding it");
        GuestLease.release();
        check(!GuestLease.isHeld(dir), "a released lease reads as free");
        GuestLease.markExit(dir, "again");
        check(GuestLease.exitReason(dir).contains("again"), "markExit records without holding the lock");
        GuestLease.clearExit(dir);
        check(GuestLease.exitReason(dir) == null, "the exit marker can be cleared");

        // The property the whole design rests on: the lock belongs to the process, so its death
        // releases it. A second JVM takes the lease; this one must see it held; killing that JVM
        // must make it free - with no cooperation from the dead process at all.
        Process child = startHolder(dir);
        try {
            check("HOLDING".equals(readLine(child)), "the second process took the lease");
            check(GuestLease.isHeld(dir), "another process holding the lease reads as held");
            child.destroy();
            check(child.waitFor(10, TimeUnit.SECONDS), "the second process is gone");
            long deadline = System.currentTimeMillis() + 5_000;
            while (GuestLease.isHeld(dir) && System.currentTimeMillis() < deadline) Thread.sleep(50);
            check(!GuestLease.isHeld(dir),
                    "the lease died with the process that held it, which is what lets the launcher"
                            + " tell a crash from an exit");
        } finally {
            if (child.isAlive()) child.destroyForcibly();
        }
    }

    private static Process startHolder(File dir) throws Exception {
        File java = javaBinary();
        ProcessBuilder builder = new ProcessBuilder(java.getAbsolutePath(), "-cp",
                System.getProperty("java.class.path"), LeaseHolder.class.getName(), dir.getAbsolutePath());
        builder.redirectErrorStream(true);
        return builder.start();
    }

    private static String readLine(Process process) throws Exception {
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
            return reader.readLine();
        }
    }

    /** The JVM running this test, so the child cannot be a different one than the tests were built for. */
    private static File javaBinary() {
        File home = new File(System.getProperty("java.home"), "bin");
        for (String name : new String[] {"java", "java.exe"}) {
            File candidate = new File(home, name);
            if (candidate.isFile()) return candidate;
        }
        throw new AssertionError("no java binary under " + home);
    }

    public static void main(String[] args) throws Exception {
        File dir = Files.createTempDirectory("zb-stability-").toFile();
        try {
            testDecisionTable();
            testHistory();
            testCrashPolicy();
            testLease(dir);
        } finally {
            GuestLease.release();
            deleteRecursive(dir);
        }
        System.out.println("GuestRecoveryTest PASS (" + checked + " checks)");
    }

    private static void deleteRecursive(File file) {
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) deleteRecursive(child);
        }
        //noinspection ResultOfMethodCallIgnored
        file.delete();
    }
}
