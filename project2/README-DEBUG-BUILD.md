# The debuggable build, and what its app log says

`project2/build-debug.sh` builds the same launcher as `build-obfuscated.sh`, for diagnosis instead
of hiding:

```sh
sh project2/build-debug.sh          # ~15 s, no R8, no dex rewrite, no re-pack
adb install -r project2/translator-debug.apk
```

| | obfuscated build | debuggable build |
|---|---|---|
| file | `project2/pokemon-go-emulated-obfuscated.apk` | `project2/translator-debug.apk` |
| what it is | R8-renamed dex, debug info stripped, real names gone | real names, real line numbers, `android:debuggable="true"` |
| app log | none (the logging seam is a no-op) | everything, to logcat **and** a file on disk |
| applicationId | `com.chucny.translator.bridge` | the same (see below) |
| signer | `~/.android/debug.keystore` (or the release key if `~/.zettabridge/signing.properties` exists) | the same key, so `adb install -r` upgrades in place |

The applicationId is deliberately **not** suffixed. The debug APK installs over the obfuscated one
as an ordinary upgrade, so the imported plugin, its extracted arm32 libraries and everything under
`/data/user/0/com.chucny.translator.bridge` survive - which is the point: the failure being
investigated stays reproducible instead of starting from an empty library.

`build/check_debug_build.py` gates the artifact, and `build-debug.sh` runs it:

* `android:debuggable=true` is read out of the packaged manifest's binary XML (aapt2 stores
  framework attributes as resource ids, so the word "debuggable" is not in the file at all);
* the verbose logger is really in the dex - its string literals only exist in the debug source set;
* the obfuscated APK next to it carries none of those markers, i.e. the release build gained no
  narration from this.

## Where the log is

```
/sdcard/Android/data/com.chucny.translator.bridge/files/zb-app-log.txt         the whole run
/sdcard/Android/data/com.chucny.translator.bridge/files/zb-runtime-report.txt  the runtime's own account
/sdcard/Android/data/com.chucny.translator.bridge/files/zb-errors.txt          handled errors and launches
```

```sh
adb pull /sdcard/Android/data/com.chucny.translator.bridge/files/zb-app-log.txt
```

`zb-app-log.txt` is the copy to read: it is created mode 0660 by the app and `adb` can pull it. The
runtime's own report is written mode 0600 by the native side and cannot be pulled without root
(`run-as` cannot reach external storage either - measured), so the app log carries it instead: every
change to the report is logged as a digest, and a window that closes or stays black gets the
renderer's lines (`egl-*`, `gl-*`, `proxy-*`, `jni-native-activity-*`, `guest-exit`) verbatim. There
is no need to read the report file to see what the guest did; it is all in the app log.

There is more than one destination on purpose. Some ROMs (OxygenOS) drop third-party logcat output,
so the file is the primary copy; it is written with one `write()` syscall per line, so a SIGKILL or
a native crash cannot lose what was already logged; and the last lines plus the crash are copied to
**the clipboard** when a window is lost, so a phone with no browser and no usable logcat can still
hand the evidence over. Records are capped at 4 000 characters and the file rotates at 8 MB, so the
logger cannot itself become the thing that breaks the game.

Logcat carries the same lines under the tag `Translator`:

```sh
adb logcat -s Translator
```

## What it says about a window that closed

Every window event is logged, and every close carries its reason:

| line | meaning |
|---|---|
| `WINDOW ... resumed - this is the window on screen now` | the window really came up |
| `WINDOW ... drew its first frame WxH` | the surface is on screen **and** painted |
| `BLACK WINDOW: ... has not drawn a single frame` | the surface is up and empty: nothing ever painted it |
| `WINDOW CLOSED: ... / why: something called finish()` | the app or launcher closed it deliberately |
| `WINDOW CLOSED: ... / why: the system is rebuilding it for a configuration change` | rotation/theme, not a failure |
| `WINDOW CLOSED: ... / why: the SYSTEM tore it down` | not a finish and not a config change: process teardown, a kill, or memory |
| `WINDOW CLOSED: ... / CAUSE: this window died with the uncaught exception logged N ms earlier` | a Java crash took the process and its window |
| `FATAL: uncaught exception ... what this means: the process ... is being killed` | full stack of a crash, in the :guest or the launcher process |
| `WATCHDOG the :guest process (pid N) is gone` | the guest died; the reason line distinguishes an intentional kill, a recorded signal, and a kill from outside the app |
| `RUNTIME REPORT (the runtime's own account, as it changes)` | the runtime's stable facts: plugin, proxy loads, JNI, EGL/GL, guest exit |

The `BLACK WINDOW` check reports the renderer's own counters, which is what separates the three ways
a black window happens: no EGL surface at all (the guest never got as far as a context), a surface
but no swap (the renderer is stuck), or swaps happening while nothing is on screen (a composition
problem, not the guest's).

## What it does about a window that closes at once

A launched window closing immediately is the worst failure to debug, so it is neither silent nor
unrecoverable:

* **The failure is visible.** The :guest entry point is declared `NoDisplay`, and the framework kills
  any such activity that has not finished by the end of onResume ("did not call finish() prior to
  onResume() completing" - measured on device), so the reason cannot be shown there. Instead it is
  handed to `LaunchFailureActivity`, which has a real window in the launcher process and **stays**:
  the exception, the bridge's last failure, the plugin's state in the library, and a `Try again`
  button. `Stubs` does the same for the other half of this failure - a plugin screen that could not
  be created at all, where the placeholder used to toast and vanish.
* **What can be repaired is repaired.** An app that is simply not imported yet (a fresh install, or
  the first launch racing the library's own seeding of the bundled app) is imported off the main
  thread and the launch is retried.
* **A transient failure is retried once**, in a fresh `:guest` process, which is the only kind of
  retry the process-lifetime runtime allows.
* **The retries are bounded** (one per app per minute, recorded outside the process) so a
  permanently missing app shows its reason instead of restarting forever.
* **Nothing is killed** behind the user's back any more: the old path killed the :guest process 2 s
  after a bridge error, which is indistinguishable from a crash.
* **Nothing in the launch path may take the process down**: the runtime install, the routing
  activity and the guest entry point all contain their failures, report them, and hand over to the
  failure screen.

## Reading a real failure: the Pixel 8 report

A release run on a Pixel 8 (Android 15/16, Mali) ended with the window black and gone a second
later. Its `zb-runtime-report.txt` said:

```
proxy-loads: 1      proxy-failures: 0
jni-onload: libmain.so ok jni=0x00010006
registered-natives: 39
guest-exit: (none)
native-calls: nativeAddVSyncTime=109 nativeRecreateGfxState=3 invoke=3 load=1
              nativeInitWWW=1 initJni=1 nativeForwardEventsToDalvik=1 nativeRender=1 total=124
```

Read from the bottom up: the guest started and loaded the plugin through the proxy (no proxy
failures), Unity's Java player ran, it recreated its graphics state three times, rendered **once**,
and then quit - while **not one `egl-*` or `gl-*` line appears anywhere in the report**, where a
working run on the Adreno device has `egl-choose`, `egl-context`, `egl-surface: created`,
`egl-swaps: 3`, `gl-calls: 9630` and `gl-first-call`. No EGL activity at all means the guest never
reached a GL context: the failure is in the window/surface handoff between the host activity's
window and the guest's `ANativeActivity`, before anything was rendered. That is the thing to fix
next, and it is a *runtime-side* handoff, not the launcher's.

On the same phone, the debuggable build reports that handoff explicitly: `WINDOW attaching ...`
for the stub, `newActivity ... -> plugin activity ...` for the Unity activity, and either
`drew its first frame` or `BLACK WINDOW: ...` with the EGL/GL lines above. Pull the log after one
failed launch and the two candidate readings are already distinguished:

* no `jni-native-activity: ... running` line -> the guest's native activity was never handed the
  host window;
* that line present with no `egl-*` lines -> the window arrived but no EGL surface could be made
  from it (driver/format side).

A launch can be triggered without touching the screen, which is what the commands above use. Note
that `am start -n pkg/.Activity` does **not** expand the shorthand here: the manifest namespace is
`com.zettabridge.launcher` while the applicationId is `com.chucny.translator.bridge`, so the fully
qualified class has to be spelled out.

## Checking the build

```sh
py project2/build/check_debug_build.py project2/translator-debug.apk
```

It prints the debuggable flag, the logger markers found in the dex, and the same markers in the
obfuscated build (which must be zero), and exits non-zero when any of that is wrong. It also checks
the stability layer (`README-STABILITY.md`) in *both* APKs - three of its string literals in each
dex and its `<service>` in each packaged manifest - because that layer is behaviour, not narration,
and a build that lost it would still pass every check above.

## What the log says about the stability layer

The shipping build watches the `:guest` process and reopens the game when it dies with a window on
screen. This build narrates that too, so a reopening can be told apart from a launch that simply
happened again:

* `NOTE  guest supervisor watching <plugin> (N of 3 reopenings already used in this window)`;
* `ERROR  reopening <plugin> after it stopped`, with the reason and then how it ended - the guest's
  phase, whether it left a deliberate-exit marker, and the runtime's own exit line;
* `NOTE  the reopened <plugin> is running again` when the game is back, or
  `ERROR  cannot reopen <plugin> automatically; the user has to open it` when the platform refused
  the activity start (Android 10+ background-activity-launch restrictions). Measured on an Oppo
  CPH2669 / Android 16, the start is **not** refused: the first of the two lines above is the one a
  kill produces, and the game was back on screen by itself (see `README-STABILITY.md`, "Measured");
* `ERROR  gave up reopening <plugin>` when the budget is spent, which is also the moment the user
  gets the "The app keeps stopping" screen.

To produce one on purpose, kill the guest the way a native crash does - `SIGKILL`, no Java, nothing
recorded by the guest itself:

```sh
adb shell ps -A | grep 'bridge:guest'                              # the pid to kill
adb shell run-as com.chucny.translator.bridge kill -9 <pid>        # debuggable build only
```

The negative cases are worth as much: closing the game from inside, pressing Home and letting the
system reclaim it, or swiping the task away must each leave the log with `guest supervisor
stopping: ...` rather than a reopening.
