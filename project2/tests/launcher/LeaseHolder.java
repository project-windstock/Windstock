package com.zettabridge.launcher;

import java.io.File;

/**
 * A second process that takes the guest lease and holds it until it is killed.
 *
 * <p>Started by {@link GuestRecoveryTest} to test the one property the launcher's crash detection
 * depends on and that cannot be simulated in-process: the kernel releases the lock when the process
 * holding it dies. Prints {@code HOLDING} once the lease is taken, then waits to be killed.
 */
public final class LeaseHolder {
    private LeaseHolder() {}

    public static void main(String[] args) throws Exception {
        File dir = new File(args[0]);
        boolean held = GuestLease.acquire(dir, "the test's second process");
        System.out.println(held ? "HOLDING" : "REFUSED");
        System.out.flush();
        if (!held) return;
        Thread.sleep(300_000);
    }
}
