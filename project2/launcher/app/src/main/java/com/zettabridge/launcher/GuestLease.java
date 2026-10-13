package com.zettabridge.launcher;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * The :guest process's liveness lease, and the record of a deliberate exit.
 *
 * <p>Why a file lock and not a flag. A plugin that dies must be tellable apart from a plugin that
 * exited, and the only event the launcher can trust is the kernel's: a POSIX advisory lock
 * ({@link FileLock}, which is {@code fcntl(F_SETLK)} on this platform) is owned by the *process*,
 * so the kernel drops it the instant the process ends - however it ended, including a native crash
 * or a {@code SIGKILL} that runs no Java at all. A flag file would instead have to be un-set by
 * code that may never run, and a stale flag is exactly the state that makes a watchdog miss the one
 * crash it exists for.
 *
 * <p>So the two facts the launcher needs are separated:
 * <ul>
 *   <li><b>is the :guest process still there?</b> - {@link #isHeld(File)}: can this process take the
 *       lease? It cannot while the guest holds it, and it can the moment the guest is gone;</li>
 *   <li><b>did it leave on purpose?</b> - {@link #markExit(File, String)}, written by the guest
 *       itself when the plugin closes its last window. That is what says "do not restart this": a
 *       game the user closed is not a crash, and a supervisor that cannot tell them apart turns
 *       every normal exit into a restart loop.</li>
 * </ul>
 *
 * <p>{@link #acquire(File, String)} retries for two seconds, and that bound is deliberate: it runs on
 * the :guest process's main thread, at the start of a launch. The launcher's probe takes the lock
 * for microseconds at a time, so the first attempt almost always wins and the retries exist for
 * correctness rather than probability - but a launch that does start while a previous, still-dying
 * guest process holds the lease must not turn into a multi-second stall on the thread that is about
 * to build the game's window. Two seconds of 100 ms attempts covers the race it is there for; the
 * process list remains the launcher's second opinion, so a lease that could not be taken delays
 * recovery at worst, and never produces a false death.
 *
 * <p>Everything here is best effort and never throws: a lease that cannot be taken costs the guest
 * nothing but the launcher's ability to notice its death, which must never be a reason for a game
 * not to start. The class has no Android dependency on purpose, so the lease's real property - a
 * lock released by the death of the process holding it - is covered by a host test that kills a
 * second JVM and watches the lock go (see tests/launcher).
 */
final class GuestLease {
    /** Held for as long as the :guest process lives; its contents are only a human-readable note. */
    static final String LEASE_FILE = "guest.lease";
    /** Written by the guest when the plugin closes itself, so the death is not read as a crash. */
    static final String EXIT_FILE = "guest-exit.txt";

    private static final int ATTEMPTS = 20;
    private static final long RETRY_MS = 100;

    /** The channel is kept open for the process's life: closing it would release the lock. */
    private static RandomAccessFile heldFile;
    private static FileLock heldLock;

    private GuestLease() {}

    /**
     * Takes the lease for the calling :guest process. Idempotent: a process that already holds it
     * gets it back without touching the file. Once the lease is held, the previous run's exit marker
     * is cleared (and not before): a new run is about to begin, and a stale "it closed itself" would
     * stop the launcher from noticing this run's death. A process that never gets the lease is
     * therefore left with a stale marker - the direction that loses one recovery instead of
     * inventing one.
     *
     * @return true when this process holds the lease
     */
    static synchronized boolean acquire(File dir, String note) {
        if (heldLock != null && heldLock.isValid()) return true;
        if (dir == null) return false;
        File file = new File(dir, LEASE_FILE);
        for (int attempt = 0; attempt < ATTEMPTS; attempt++) {
            RandomAccessFile raf = null;
            try {
                raf = new RandomAccessFile(file, "rw");
                FileLock lock;
                try {
                    lock = raf.getChannel().tryLock();
                } catch (OverlappingFileLockException e) {
                    lock = null;  // this same process is looking at it: nothing to take
                }
                if (lock != null) {
                    raf.setLength(0);
                    raf.write((note == null ? "" : note).getBytes(StandardCharsets.UTF_8));
                    heldFile = raf;
                    heldLock = lock;
                    clearExit(dir);
                    return true;
                }
                raf.close();
            } catch (IOException e) {
                // The lease is a convenience; a file system that refuses it must not stop a launch.
                if (raf != null) close(raf);
                return false;
            }
            sleep(RETRY_MS);
        }
        return false;
    }

    /**
     * True while some process - necessarily the :guest process, since only it takes this lease -
     * holds it. Called from the launcher process, whose own probe takes and immediately releases the
     * lock, so the answer is about the other process.
     */
    static boolean isHeld(File dir) {
        if (dir == null) return false;
        File file = new File(dir, LEASE_FILE);
        if (!file.isFile()) return false;
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            FileLock lock;
            try {
                lock = raf.getChannel().tryLock();
            } catch (OverlappingFileLockException e) {
                return true;  // this process holds it: the caller is the guest itself
            }
            if (lock == null) return true;
            lock.release();
            return false;
        } catch (IOException e) {
            // An unreadable lease file is not evidence of a live guest; the caller decides what to
            // do with a "not held" answer, and it has a second opinion (the process list).
            return false;
        }
    }

    /** Releases the lease, if this process holds it. Called on a deliberate exit. */
    static synchronized void release() {
        try {
            if (heldLock != null) heldLock.release();
        } catch (IOException ignored) {
            // The lock goes away with the process anyway.
        }
        if (heldFile != null) close(heldFile);
        heldLock = null;
        heldFile = null;
    }

    /**
     * Records that the guest left on purpose and gives up the lease. Called only when the plugin's
     * own last window is destroyed because the plugin finished it - never on a configuration change
     * (a rotation destroys and recreates the activity) and never on a plain pause (the game is still
     * in this process, on screen next).
     *
     * <p>The bytes are flushed before the lock is dropped, so a launcher that sees the lease free
     * cannot read the marker before it is on disk.
     */
    static void markExit(File dir, String reason) {
        if (dir != null) {
            File file = new File(dir, EXIT_FILE);
            try (FileOutputStream out = new FileOutputStream(file)) {
                out.write((System.currentTimeMillis() + " " + reason + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();
                out.getFD().sync();
            } catch (IOException ignored) {
                // Without the marker the launcher may offer a restart after a deliberate close. That
                // is a superfluous offer, not a broken game, so it is not worth failing a launch for.
            }
        }
        release();
    }

    /** The reason recorded by {@link #markExit(File, String)}, or null when the guest never said. */
    static String exitReason(File dir) {
        if (dir == null) return null;
        try {
            byte[] bytes = Files.readAllBytes(new File(dir, EXIT_FILE).toPath());
            String text = new String(bytes, StandardCharsets.UTF_8).trim();
            return text.isEmpty() ? null : text;
        } catch (IOException | RuntimeException e) {
            return null;
        }
    }

    /** Forgets the previous run's exit marker. Called as a run begins. */
    static void clearExit(File dir) {
        if (dir == null) return;
        try {
            Files.deleteIfExists(new File(dir, EXIT_FILE).toPath());
        } catch (IOException | RuntimeException ignored) {
            // A marker that cannot be cleared may cause one unnecessary restart offer at most.
        }
    }

    private static void close(RandomAccessFile raf) {
        try {
            raf.close();
        } catch (IOException ignored) {
            // nothing left to do about it
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
