package com.zettabridge.launcher;

/**
 * Which uncaught exceptions the :guest process survives.
 *
 * <p>Why this exists at all. A translated game runs real plugin Java inside the launcher's own
 * :guest process, and on Android an uncaught exception in *any* thread kills that process - which
 * takes the host window, the loaded runtime and the game with it. The plugin code, meanwhile, is
 * 2016-2020 Java running against a modern framework it was never tested on: its network, location
 * and support-library threads throw for reasons that have nothing to do with the game (a paused
 * GMS client answering DEVELOPER_ERROR, an API the platform now refuses). Those exceptions are,
 * from the game's point of view, noise on a thread it does not depend on - and today that noise is
 * fatal. That is the single most common way this launcher's window disappears with nothing on
 * screen to explain it.
 *
 * <p>So the policy distinguishes what the exception *is* from where it happened:
 * <ul>
 *   <li><b>a non-main thread that dies</b> - the process survives. A plugin thread failing is a
 *       degraded feature, not a dead game; the game keeps rendering, and everything about the
 *       exception is still recorded in full;</li>
 *   <li><b>the launcher's own process</b> - never swallowed. A crash in the process that owns the
 *       library, the routing and the failure screens must behave normally;</li>
 *   <li><b>the main thread</b> - not swallowed here either. An exception that has escaped the main
 *       looper means the looper is already unwinding, and a process kept alive in that state is a
 *       window nobody can interact with - a worse outcome than a clean death that the supervisor
 *       reopens a second later. {@link MainLooperGuard} is the one place that does swallow
 *       main-thread exceptions, and only for the library that is known to throw them, because it
 *       re-enters the loop and so leaves the process usable;</li>
 *   <li><b>a process that keeps throwing</b> - after {@link #MAX_SWALLOWED} exceptions the game is
 *       not being saved by staying up; the process is handed to the normal handler, and the
 *       supervisor's restart is the better answer than a zombie holding the runtime.</li>
 * </ul>
 *
 * <p>No Android dependency on purpose: this is the policy, and the host test exercises the whole
 * table without a device.
 */
final class CrashPolicy {
    private CrashPolicy() {}

    /**
     * How many uncaught exceptions a :guest process may survive. A plugin whose library is missing
     * can throw once per retry, forever; past this many the process is not limping, it is broken.
     */
    static final int MAX_SWALLOWED = 25;

    /**
     * Whether the process should keep running after this uncaught exception.
     *
     * @param guest        the exception happened in the :guest process
     * @param mainThread   it happened on the main thread
     * @param swallowed    how many exceptions this process has already survived
     */
    static boolean survives(boolean guest, boolean mainThread, int swallowed) {
        if (!guest) return false;
        if (mainThread) return false;
        return swallowed < MAX_SWALLOWED;
    }

    /** One line for the log: what the decision was, and what it costs. */
    static String explain(boolean guest, boolean mainThread, int swallowed, boolean survives) {
        if (survives) {
            return "the :guest process stays up: the thread that threw is not the main thread, and"
                    + " the game does not depend on it. The exception is recorded in full;"
                    + " " + (MAX_SWALLOWED - swallowed - 1) + " more like it would end the process.";
        }
        if (!guest) {
            return "the launcher's own process: handled normally, so the failure screen and the"
                    + " library behave as they always do.";
        }
        if (mainThread) {
            return "the main thread threw, so its looper is already unwinding; the process dies"
                    + " cleanly and the supervisor reopens the game instead of leaving a window"
                    + " nobody can interact with.";
        }
        return "this process has already survived " + swallowed + " uncaught exceptions"
                + " (limit " + MAX_SWALLOWED + "): it is not being saved by staying up, so it is"
                + " handed to the normal handler and the supervisor reopens it instead.";
    }
}
