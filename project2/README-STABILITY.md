# Stability: keeping the heavy process alive, and reopening it when it dies

A translated client runs inside the launcher's `:guest` process. That process holds the runtime, the
plugin's own Java, Unity's Java player and the window the user is looking at, and on Android an
uncaught exception in *any* thread of it kills all of that at once. A plugin written for Android 7-9
running on Android 15 throws often - a paused Play services client answering `DEVELOPER_ERROR`, a
location API the platform now refuses, a thread pool that outlived its activity - and none of it is
about the game. Today each of those is fatal, which is what "the game opens and closes" usually is.

This layer does four things about that, and nothing else:

| file | what it does | where it runs |
|---|---|---|
| `GuestLease.java` | the liveness lease: a file lock the `:guest` process holds, which the kernel releases when it dies. Also the deliberate-exit marker. | both |
| `GuestHeartbeat.java` | the phase journal (`starting`/`loaded`/`visible`/`background`/`closed`) the launcher reads to know whether a window was on screen | `:guest` writes, launcher reads |
| `CrashPolicy.java` | which uncaught exceptions the process survives (pure decision) | `:guest` |
| `CrashGuard.java` | applies that policy and records every uncaught exception first | both processes |
| `GuestRecovery.java` | the decision a dead guest gets: reopen, wait, or stop (pure decision) | launcher |
| `GuestSupervisor.java` | watches the lease and reopens the game; stops itself when doing so would be wrong | launcher process, as a service |

It is part of the **shipping build**, not the debuggable one. `build/check_debug_build.py` proves it:
the recovery layer's string literals must be in *both* dexes and its `<service>` in *both* packaged
manifests, because R8 renaming a class the manifest instantiates, or shrinking one away, would each
look fine from one side only.

## What is guaranteed, and what is not

Guaranteed by this layer:

* **A worker thread in the `:guest` process may throw without ending the game.** Up to 25 uncaught
  exceptions per process are recorded in full and survived. Past that the process is handed to the
  normal handler, because a process throwing that fast is not being saved by staying up;
* **A death with a window on screen is noticed within about a second and the game is reopened** -
  three times in any ten minutes, with a 2 s/5 s backoff, and the count survives this process being
  restarted itself (it is in `SharedPreferences`);
* **Nothing reopens a game the user closed.** A plugin that finishes its last window writes an exit
  marker, a game that is not in the foreground logs a non-visible phase, and a removed task stops the
  watch outright (`Service.onTaskRemoved`). Those three are the difference between a watchdog and a
  thing that drags people back into a game they left;
* **Every decision is written down**, in `zb-errors.txt`, next to the reason the guest died (its phase
  and the runtime's own exit line, read from files - never by asking the runtime, which is what broke
  the game in an earlier diagnostic build).

Not guaranteed, and not claimed:

* **A native fault is not prevented.** A `SIGSEGV` inside a translated block, a `SIGKILL` or a
  low-memory kill runs no Java at all; nothing in an APK can intercept those. They are *recovered*
  (reopened, bounded, recorded), not prevented. `ZBridge.setPreciseFaults` and the runtime's fault
  handling are where that would change, and that is runtime work, not launcher work;
* **A main-thread exception still ends the process.** `MainLooperGuard` swallows the one class of
  those that is known to happen (Play services) because it re-enters the loop and leaves the process
  usable; an arbitrary main-thread crash leaves a looper that is already unwinding, and a window
  nobody can interact with is worse than a clean death and a reopening;
* **Reopening restores the game, not the session.** The runtime is process-lifetime: the reopened
  process loads the plugin from scratch. A game with server-side state comes back where it was; a game
  with local unsaved progress does not, and no launcher can change that;
* **A slow first launch runs unsupervised.** The watch gives up if no guest process appears within 20
  seconds (`GuestRecovery.START_GRACE_MS`); the first launch after a fresh install imports the bundled
  APK before any plugin process exists and can exceed that. The launch path reports that class of
  failure itself, so nothing is silent - one run simply has no net;
* **The automatic reopening is an activity start from a service.** From Android 10 on, a background
  activity launch can be refused by the platform. Measured, it is not refused here: a `SIGKILL` of
  the `:guest` process with the game on screen was followed by the new process starting 1.0 s later
  and its window drawing its first frame 4.1 s after that (OPPO CPH2669, Android 16; the run is
  written out under "Verifying it" below). When a platform does refuse it, the reopening is logged
  as such and the user opens the app from the library; the failure is recorded, not hidden;
* **The system stops the supervisor when the app is in the background** (background service limits).
  A guest killed while the app is not in front is therefore not reopened - which is the intended
  behaviour anyway, and the phase rule would have stopped it in most of those cases regardless.

Also worth knowing: `android:largeHeap="true"` was added to the manifest for the `:guest` process's
Java heap. It raises a cap; it does not make the native heap bigger and it does not remove
`OutOfMemoryError`, it makes it less likely.

## The decision table

`GuestRecovery.decide` is the whole policy, in this order (the first match wins). It is pure Java,
which is why it is the part covered by tests that need no device:

| state | answer |
|---|---|
| the lease is held, or the process list says it is running | wait |
| not seen alive yet, watching for less than 20 s | wait |
| not seen alive after 20 s | stop - that is a launch failure, and the launch path reports it |
| it left an exit marker | stop - it closed itself |
| it died while no window was on screen | stop - the user was not looking at it |
| it already stopped `MAX_RESTARTS` (3) times in the window | give up, and say so on screen |
| otherwise | reopen it, waiting `0` / `2 s` / `5 s` by restart number |

## Verifying it

### On the host (no device, no emulator)

```sh
sh project2/tests/launcher/run_stability_tests.sh
```

50 checks over the decision table, the restart arithmetic (including that the history survives being
written down), the crash policy, and the lease. The lease test is the only one that proves something a
unit test normally cannot: it starts a **second JVM**, has it take the lease, kills it, and requires
this process to be able to take the lease afterwards - with no cooperation from the dead process. That
is the property the entire design rests on, and the reason the lease is a kernel lock and not a flag.

### On a device

The debug build (`README-DEBUG-BUILD.md`) narrates every step. Find the `:guest` pid and kill it
exactly the way a native crash does - `SIGKILL`, no Java, nothing recorded by the guest itself:

```sh
adb shell ps -A | grep 'bridge:guest'                  # the pid to kill
adb shell run-as com.chucny.translator.bridge kill -9 <pid>   # debuggable build only
```

Expect: the game is back on screen within about a second, and

```sh
adb pull /sdcard/Android/data/com.chucny.translator.bridge/files/zb-errors.txt
```

contains a `RECOVERY reopening ...` line with the guest's phase and the runtime's exit line. Three
kills inside ten minutes must end at "The app keeps stopping" instead, and a fourth must not restart.

The negative tests matter as much as that one:

```sh
# 1. Open the game, press Home, then let the system reclaim it: nothing may be reopened.
adb shell input keyevent KEYCODE_HOME; adb shell am kill com.chucny.translator.bridge
# 2. Open the game and close it from inside (or with Back): nothing may be reopened.
# 3. Open the game, kill the process, then swipe the task away before the restart lands.
```

What Android did to the process, when the reason matters, is in the event log - this is also the
quickest way to answer "was this the platform, or the app":

```sh
adb logcat -b events -d 'am_kill:V' 'am_proc_died:V' 'am_crash:V' 'am_anr:V' '*:S'
```

`am_kill` with `reason=EXCESSIVE_CPU`/`ANR`, `am_crash`, and a missing `am_proc_died` at the moment
the window went away each tell a different story.

### Measured: OPPO CPH2669, Android 16 (API 36), arm64, debug build

Every case below was run on the device, in the order it is written; the times are the device's own
log clock. The launch was the real one - `LibraryActivity`, then a tap on the game's row.

| case | what was done | what happened |
|---|---|---|
| crash with the game on screen | `run-as ... kill -9 <:guest pid>`, phase `visible` | death noticed in 1.1 s, new `:guest` process 1.0 s later, its window drew its first frame 4.1 s after that. `reopening ... (restart 1 of 3)`, then `the reopened ... is running again` |
| the same, again | kill 2 and 3 inside the window | `restart 2 of 3`, then `restart 3 of 3`, backoff 0 s / 2 s / 5 s |
| the fourth death | kill 4 | `gave up on ...: the app stopped 3 times in 10 minutes` and **"The app keeps stopping"** (LaunchFailureActivity) on screen; no fifth process was started, and `translator.watch.xml` still holds exactly 3 timestamps |
| killed in the background | Home, then `kill -9` | `guest supervisor stopping: no game window was on screen when the process ended`; nothing was reopened and no restart was recorded |
| task removed | the app's task was removed while watching | `the task was removed while the supervisor was watching ..., not reopening it` - `onTaskRemoved` stops the watch outright |
| platform kill | the system killed the guest (reason `remove task`, `am_kill` in the event log) | treated exactly like any other death: phase read, decision taken, recorded |

What the run also showed, and what the numbers above are worth knowing for:

* **The reopen is an activity start from a service, and Android 16 here allows it.** That was the
  last unmeasured claim in this document, and it holds: the game came back on screen by itself, with
  no user action, while the app had no visible window of its own at the moment of the death;
* **the `:guest` process also dies on its own** in this environment - occasionally, with no signal
  and no exit recorded in the runtime's report. The layer treats that exactly like the `SIGKILL`
  above (which is the point: it cannot tell them apart, and does not have to), and it is why the
  allowance and the give-up screen exist;
* **two guest processes can be started after one death.** Seen once: a `PluginSwitchActivity` created
  in the launcher process 0.1 s after the watchdog reported the death started a fresh `:guest`
  process, and the supervisor's own reopening for that death followed 15 s later. The supervisor did
  not double-start - it saw the new process alive and waited - and the user-visible outcome was
  correct, but the extra launch wastes a process start and delays the reopening. Unfixed and
  unexplained; it is the first thing to look at next.

### What the device run changed in the code

One defect, which no host test could have caught and which the phone showed within minutes:
**the phase journal never left `loaded`.** With the game on screen the phase file still said
`loaded`,

```sh
adb shell run-as com.chucny.translator.bridge cat files/guest-phase.txt
# loaded 1791825482258 pid=32274 plugin=com.nianticlabs.pokemongo.classic   <- while topResumedActivity was the game
```

because the phases were driven from `Application.registerActivityLifecycleCallbacks`, which is the
obvious API and does not work for plugin activities: `GuestRuntime.prepareActivity` hands each plugin
activity the *plugin's* Application, so `getApplication().dispatchActivityResumed()` reaches an
object nothing registered on. Measured consequence, before the fix: a game killed while the user was
on the home screen was read as `windowWasUp` and **reopened** - the exact behaviour this document
promises cannot happen. The writes now come from `GuestInstrumentation.callActivityOnResume` /
`callActivityOnPause`, which the framework calls for every activity in the process, and the same
scenario now stops the watch with `no game window was on screen when the process ended`.

## A crash that was not the guest's fault: the Android 16 ServiceConnection overload

Android 16 added a third method to `android.content.ServiceConnection`,
`onServiceConnected(ComponentName, IBinder, IBinderSession)`, as a **default** method, and the
platform now dispatches a bound connection through it. A translated plugin implements that
interface in its own native code, and what it hands to `bindService` is a
`java.lang.reflect.Proxy` whose handler forwards every call into the guest - so the new overload
arrives at a guest that has never had it, and the guest dies throwing from inside its own handler.
Measured on the Pixel 8 (Android 16, Pokemon GO 0.29), on every launch, ~8 s after the first frame:

```
java.lang.NoSuchMethodError: public default void android.content.ServiceConnection.onServiceConnected(
    android.content.ComponentName, android.os.IBinder, android.app.IBinderSession)
  at bitter.jnibridge.JNIBridge.invoke(Native Method)
  at android.app.LoadedApk$ServiceDispatcher.doConnected(LoadedApk.java:2420)
```

It is fixed in the launcher, not in the guest: `ServiceConnectionCompat.wrap` substitutes an adapter
for the plugin's connection as it is passed to `bindService` (through `PluginContext`, which is the
base context of every plugin activity and of the plugin Application - the only reason the launcher
can see the call at all). The adapter implements the two methods the plugin was built with and
**inherits** the newer ones, so the platform's three-argument call runs the interface's own default
body, which calls `onServiceConnected(name, service)`, which reaches the plugin. The guest is never
asked for a method it does not have, and nothing about the plugin's connection changes.

Two consequences worth knowing:

* it is not device-specific. `IBinderSession` is an API 36 addition, so every Android 16+ device
  fails identically, and on older platforms the adapter is a plain pass-through (the interface has
  only the two methods there) - one code path for all versions;
* only a connection that goes through a plugin context is adapted. The debug log prints
  `bindService: <class> is a plugin connection` for each one it adapted, so a connection that was
  missed is visible rather than silent. Measured on the Pixel 8: three per launch - the guest's own
  `$Proxy3`, the in-app-billing provider's connection and Google Play services' internal one.

## Tunables

| constant | where | meaning |
|---|---|---|
| `MAX_SWALLOWED = 25` | `CrashPolicy` | uncaught worker-thread exceptions a `:guest` process may survive |
| `START_GRACE_MS = 20_000` | `GuestRecovery` | how long a launch has before its absence is a death |
| `MAX_RESTARTS = 3` | `GuestRecovery` | reopenings allowed inside one window |
| `WINDOW_MS = 10 min` | `GuestRecovery` | the sliding window the cap is counted in |
| `POLL_MS = 500` | `GuestSupervisor` | the watch interval |
| `MIN_GAP_MS = 2 s` | `GuestSupervisor` | the shortest gap between two reopenings, whatever the backoff says |
| `ATTEMPTS × RETRY_MS = 2 s` | `GuestLease` | how long the guest retries for the lease, on its main thread |
