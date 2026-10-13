# project2 - the obfuscated "Translator" build

`project2/` is a **copy** of the launcher module plus the scripts that turn it into a hardened APK.
Nothing under `ZettaBridge-main/` is edited: this folder builds from its own `launcher/` source tree,
its own staged runtime under `build/launcher`, and the bundled client under `build/bundled-assets`.

```
sh project2/build-obfuscated.sh          # -> project2/pokemon-go-emulated-obfuscated.apk
```

The script runs six steps and stops at the first failure:

1. `strip-runtime-binaries.sh` - removes every name the shipped runtime does not need (below).
2. `build/pack_payloads.py` - packs the runtime and the bundled client into two encrypted containers,
   stages the bridge library under a generated name, and writes the generated `Keys` class with those
   names and the payload key. Names and key are fresh on every build.
3. `build/make_dictionary.py` - writes a fresh 4096-word identifier dictionary, so two builds of the
   same source do not produce the same symbol names either.
4. Gradle `assembleRelease` - R8 with `proguard-rules.pro`, resource shrinking, `debugSymbolLevel
   NONE`. Offline: the toolchain under `toolchain/` is read, nothing is downloaded.
5. `build/strip_dex_debug.py` + `zipalign` + `apksigner` - erases the debug metadata R8 refuses to
   drop (every line number, every source-file reference) from the packaged dex, re-aligns the archive
   the repack unaligned, and re-signs it with the key Gradle used.
6. `build/verify_payloads.py` - decrypts both containers out of the **packaged APK** with the
   generated key and compares every entry against what was staged. A payload that does not read back
   byte-for-byte would install a launcher that cannot start its own runtime, so this fails the build.
7. `build/audit_apk.py` - fails on a mapping file, a symbol table, DWARF, a compiler banner, a
   surviving line number or source-file reference, a payload that is not opaque, an entry name that
   gives its contents away, or a decoy layer that is missing, flattened or over its byte budget.

Steps 6 and 7 are the point: every claim below is the output of those two programs reading the
shipped artifact, not an assumption about the build.

## What an unzip shows now

```
META-INF/com/android/build/gradle/app-metadata.properties
META-INF/version-control-info.textproto
classes.dex
lib/arm64-v8a/libmdb41xhph.so        <- the arm64 bridge, under a name generated for this build
assets/qr9gd2040nsk.bin              <- the bundled client APK, encrypted (60,091,309 bytes)
assets/srj8qtby1qxs.bin              <- the 32-bit runtime bundle, encrypted (1,649,691 bytes)
AndroidManifest.xml
res/zt.png
resources.arsc
```

Those are the **9 entries the app uses**, and the only ones that mean anything: 1655 more files fill
`assets/` with noise and bait (the decoy layer, below), taking the archive to 1664 entries. Before
this layer existed the listing carried
`assets/app/base.apk` (61 MB, uncompressed and readable), `assets/zb/**` with the runtime under its
real names (`zbhost`, `libzbproxy.so`, the bionic sysroot, `zb-files.txt`, `zb-version.txt`), and
`lib/arm64-v8a/libzbridge.so`. Unzip-and-lift-the-translator worked because there was nothing in the
way. Now the bulk of the APK is ciphertext, the names say nothing, and the file names a reader would
look for are not in the archive at all.

Measured: **94.8% of the archive's bytes cannot be read without the app's key** (62,530,183 of
65,956,457 bytes). What stays readable is what the platform itself must read: `classes.dex`,
`AndroidManifest.xml`, `resources.arsc`, `res/zt.png`, the two `META-INF` files and the one native
library that ART has to `dlopen`.

## Payload containers

`build/pack_payloads.py` writes both containers (`launcher/Payloads.java` reads them):

```
[0,16)   IV, in the clear (an IV is not a secret)
[16,..)  AES-128-CTR ciphertext of
             u32 version = 1
             u32 metaLen, meta bytes        runtime: bundle version; client: the descriptor text
             u32 count
             count x { u16 nameLen, name, u64 dataOff, u32 storedLen, u32 rawLen, u8 flags }
             the entry data at those offsets, relative to the start of the ciphertext
         flags bit 0 = the entry is zlib-deflated
```

Each entry is **deflated first and then encrypted**, which is what keeps the size honest: aapt cannot
compress the result any further, but the payload was compressed before it was hidden. Net effect of
this whole layer: the APK went from 67,582,548 to **66,418,525 bytes**, about 1.1 MB *smaller* (the
runtime container is 1,649,691 bytes where aapt needed 2,612,410 for the same files, and the client
container is 1.3 MB smaller than the stored APK it replaces).

The per-build randomisation covers the container names, the key and the bridge library's name, all
generated into `build/payloads/src/com/zettabridge/launcher/Keys.java` - a build artifact the Gradle
file adds as a source directory, not a file in this repository. Two builds of the same source share
no name and no key, so a published "recipe" for one APK is useless for the next.

## The decoy layer

`build/pack_payloads.py` also fills `assets/` with files and directories that mean nothing, so that
"unzip and take the interesting file" stops being a listing-and-look job. Measured on the shipped
APK, by walking its own ZIP records:

| | |
|---|---|
| decoy files | **1655** (the audit fails the build below 1500) |
| directories they live in | **1357**, nested 2-6 levels deep (the unpacked APK holds 1365 directories in total) |
| content | 794,667 bytes: 139,307 bytes of noise in 1609 files plus **655,360 bytes of bait in 46** |
| what the layer costs the APK | **1,264,772 bytes (1235 kB)** - content plus the ZIP records of 1655 entries whose nested gibberish paths are long |
| extensions | `.bin .dat .db .idx .json .map .pkg .sig .cfg .ko .xml`, and `.so` |
| names | 10-32 characters, no vowels, no words: `00a3vo9o36j23y`, `ft1yah0369t8luqzatwlqp7g`, or two or three build words joined by underscores, `frame_shader_packet`, `profiler_device_esv`, `thread_packet_0uieyd` |

### Bait: the decoys that lie

Most decoys claim nothing about themselves. Forty-six of them do, because "find the `.so`", "find the
dex" and "find the container" are how an unwrapping attempt actually starts:

| | |
|---|---|
| headers | 8 look like an ELF (7 `ET_DYN` AArch64, 1 ARM), 6 like a dex, 4 like a zip, 10 like gzip, 8 like XML, 5 like a DER signature |
| layouts | `assets/<name>/lib/arm64-v8a/lib*.so`, `lib/armeabi-v7a`, `split_config.arm64_v8a/*.dex`, `dexopt/*.odex`, `oat/arm64/*.odex`, `META-INF/*.RSA`, `res/raw/*.bin`, `resource/*.xml`, `flutter_assets/assets/*.dat`, `profiling/*.cfg`, and bare `assets/*.bin` / `assets/*.so` |
| sizes | 3-24 kB: above the 1600 tiny files in any size sort, below the audit's 64 kB "this is real data" bar |

The two real containers are still the only large assets, and every bait file is garbage after its
header. The layout templates are the reason the tree reads as a plausible app at a glance: an
`arm64-v8a` directory with `.so` files in it, a `dexopt` and an `oat` directory, a `META-INF` with
signatures - none of it under a real path, all of it under `assets/`, where the platform never looks.

The plain decoys' contents are random bytes and deliberately **not** a stub of a real format; a
decoy name may look like build residue, never like this app - the generator refuses any name containing `zb`, `zettabridge`,
`bridge`, `proxy`, `sysroot`, `host`, `base`, `bundled`, `pokemon`, `niantic`, `chucny` or
`translator`. The budget is enforced by the build gates, not by convention: `MAX_DECOY_BYTES` is
1 MB and `MIN_DECOY_ENTRIES` is 1500 in `build/audit_apk.py`, and the packer's `TRASH_FILES`,
`TRASH_DIRS`, `TRASH_BUDGET`, `TRASH_BAIT_FILES` and `TRASH_BAIT_BUDGET` are environment-tunable for
a different trade-off (the two content budgets must stay under `MAX_DECOY_BYTES` together).

What an unpacked APK looks like now:

```
1664 files, 1365 directories, 66 MB
1655 of those files, and all but eleven of those directories, are noise; the whole path is a maze:

assets/csxfhsmp8i9h0kabjb4mgvoh/h8j2po3jfnsg6dazu6qx0gdw/thread_packet_0uieyd/4h37dyuq500xw7qra2ejuthc/2oz1yfkprlcgdl45fxpj8cd3/za9g8yysj5ewlt76srmszdya8xc3t.json
assets/03nxqi43io2k3/matrix_heap_surface_l9wi/matrix_layout/vendor_gpu_gpu_avehrj/codec_route_yzs4e1.pkg
assets/sngbh3m44iimsu/oqmgpuq7svjxyfeclkqxq7e/
```

### What it costs at runtime: nothing measurable

Neither the platform nor the app ever enumerates `assets/`. `rg getAssets` over the launcher finds
exactly two users: `Payloads.java` opening one container **by the generated name**, and
`ManifestReader`, which walks the resource table's cookies for `AndroidManifest.xml`. There is no
`AssetManager.list()` anywhere. The layer's entire runtime footprint is therefore the ZIP central
directory, which the platform mmaps and parses **once**, when the app process is created. Measured
three ways:

* on the phone, `time (300 x /system/bin/unzip -l)` over two zips carrying the shipped entry names,
  1619 entries against 161 (the pre-bait build; 45 more entries does not move this): **20.2 s against
  12.3 s** (median of three runs each), i.e. **+26 ms per invocation for 1458 more index records** -
  and that number *includes writing every extra name out*. The platform prints nothing, so its share
  of those 26 ms is smaller;
* on this machine, a central-directory parse in Python (which builds a per-entry object): 7.8 ms with
  the decoy entries, 0.1 ms without;
* on the phone, cold starts of the installed APK against the older junk-free build of the same
  launcher, force-stopped and interleaved, 8 rounds each: **median 515 ms against 490 ms**, with 4 of
  the 8 pairs faster for the junk build and a mean paired difference of +2.4 ms. This device's own
  run-to-run spread is 370-800 ms, so those are the same number. (That build differs in more than the
  junk - it has no payload containers at all - so this bounds the whole packaging layer, not the junk
  alone; and it is why the claim below is "below the noise" rather than a decimal figure.)

The two measurements that can be reproduced off the device are
`work/tools/measure_decoy_layer.py` (entry counts, extensions, name shapes, the central-directory
parse on this machine) and `work/tools/make_index_zips.py` (the two name-only zips for the phone).

The honest version: the junk is never read while the app runs - a first launch pays a sub-millisecond
index walk and the game afterwards pays nothing - and it is **noise, not encryption**. 1655 of 1664
entries being garbage is obvious within minutes of looking, and the two containers that matter
are named in the dex. It buys a reader time and a wall of noise; the container layer above is what
removes the free ride.

## Identity

| | |
|---|---|
| name on the home screen | `Chucny's Translator` (`android:label` = `@string/app_name`) |
| installed package | `com.chucny.translator.bridge` |
| Java namespace | `com.zettabridge.launcher` / `com.zettabridge.core` - unchanged, see below |
| bundled client | `Pokemon GO`, import key `com.nianticlabs.pokemongo.classic` |
| APK size | 66,418,525 bytes (1.2 MB of it the decoy layer) |

The Java namespace cannot be renamed in this copy, and it is worth saying why rather than leaving it
looking like an oversight: the bridge library exports `Java_com_zettabridge_core_ZBridge_*`, so the
class name **is half of every JNI symbol**, and `PluginClassLoader` routes a plugin's requests for
`com.zettabridge.core.*` to the host's bridge classes. Both are baked into binaries this folder does
not rebuild; renaming the Java package means rebuilding the runtime, not editing the launcher.

The applicationId, on the other hand, is free: nothing in the core keys off it
(`Process::add_path_alias` registers the `/data/data/<pkg>` and `/data/user/0/<pkg>` spellings of
whatever package the process actually runs as, and the guest's `System.load` path keeps only the file
name). The rename was verified on the device - the guest starts under `:guest` in the new package.

## What is obfuscated

Measured on the shipped APK by `build/audit_apk.py`:

| | result |
|---|---|
| dex layout | 4 dex, 152 classes (baseline) -> 1 dex, **83 classes**, all flat |
| class names outside the keep list | **1** (an SDK class R8 will not rename; the baseline shows 137) |
| names kept on purpose | **15** (the 14 manifest components including `Stubs$*`, and `com.zettabridge.core.ZBridge`) |
| method overloading | `-overloadaggressively`: unrelated methods share one name |
| SourceFile | **none** - every `class_def_item.source_file_idx` is `NO_INDEX`, so no class names a file at all |
| code-item debug info | **none** - the whole `debug_info_item` table is gone, not just unreferenced |
| decoy layer | 1757 files in 1724 nested directories, 668 kB, 24 of them wearing a fake ELF/dex/zip/XML header |
| line numbers | **0 of 394** - `build/strip_dex_debug.py` clears every `code_item.debug_info_off`, because R8 keeps 10 of them whatever the keep rules say |
| R8 mapping / seeds / usage | **not in the APK** - they stay in `app/build/outputs/mapping/release/` |
| entry names | 1884, none of which describes its contents (the audit fails the build otherwise) |
| native objects | 1 loadable library, **0 symbol tables, 0 compiler banners, 0 DWARF** |
| archive bytes readable without the key | **5.3%** (the platform-mandated files) |
| bytes that cannot be read without the key | **94.7%** |
| runtime bundle / client APK in the clear | **nothing** - both are containers, and the audit fails on an ELF or zip signature inside them |

Four things removed that a reader would otherwise get for free:

* **The runtime's function names.** The staged 32-bit runtime shipped with `.symtab`: ~1650 names
  across `zbhost`, `libGLESv2`, `libandroid`, `libzbjni`, `libGLESv1_CM`, `libEGL`, `libzbcompat`,
  `libjnigraphics`. Those were the first thing a disassembler list gave away. `strip-runtime-binaries.sh`
  covers the whole staged tree - guest, `host/libzbproxy.so`, and the bundled bionic sysroot
  (`libc.so` alone carried 3573 names). Dynamic loading only needs `.dynsym`, which `strip` keeps.
* **The compiler banner.** `.comment` names the exact clang/LLD build ("Android (13989888, +pgo,
  +bolt, +lto, +mlgo ...) clang version 21.0.0") - a fingerprint of the toolchain and build host.
  Removed from every shipped binary, together with `.note.gnu.build-id`.
* **The upstream project's name in strings.** The launcher's own literals used to spell out
  `ZettaBridge` and `com.zettabridge.launcher.*` in the dex (toasts, clipboard labels, thread names,
  intent extra keys, URI schemes, the logcat tag `zb-launcher`). They now name this app instead.
  Strings that cross into the native side or name a file on disk were deliberately left alone.
* **The data itself.** The runtime bundle and, more importantly, the bundled client APK are no longer
  files in the APK: they are compressed, encrypted containers with generated names.

## What cannot be hidden

Saying this plainly matters more than the claim that it is impossible: **a shipped binary can always
be read.** What can be removed is every *name*, every *cross-reference*, the build metadata and the
free ride - and that is what the table above measures. What is left, and why:

* **The machine code.** `lib/arm64-v8a/lib<generated>.so` is an ordinary ELF, because ART has to
  `dlopen` it by path; it is the one native object that cannot live inside a container. Anyone with
  the APK can disassemble it. The 32-bit runtime *is* inside a container, but the files it decodes to
  are ordinary ELF as well, and on a rooted device they can be read out of
  `/data/data/com.chucny.translator.bridge/files/zb/` after a first launch. Encryption here buys the
  first look, not permanent secrecy.
* **The key is in the dex.** `Keys.PAYLOAD_KEY` is a byte array the app needs at runtime, so a person
  who reads `Payloads.java`'s compiled form can unpack every container. This defeats extraction tools
  and casual `unzip`-and-lift; it does not defeat reverse engineering, and no local scheme can.
* **The decoy layer is noise, not protection.** 1655 random files do not stop anyone; they cost a
  reader the minutes it takes to notice that 1655 of 1664 entries are garbage. The bait makes the
  first guess wrong rather than making the answer unavailable: a scanner that grabs the first
  `lib/*.so` it finds under a `lib/arm64-v8a` path gets a 20 kB file with an ELF header and no code in
  it. It is deliberately cheap (794 kB of content, never touched at runtime).
* **The 14 kept class names and the `com.zettabridge.core.` prefix.** The framework instantiates the
  components by name, `GuestInstrumentation` swaps `Stubs$*` in by name, and ART resolves the JNI
  symbols by class+method name. Renaming any of them is a runtime failure, not a hardening step.
* **The dex strings that name the extracted layout** (`guest/zbhost`, `guest/lib/libzbcompat.so`, ...).
  They are the completeness check in `RuntimeBundle.requiredFilesPresent`, and the native library
  contains the same paths anyway - the APK's own name for them is not the leak.
* **The manifest, resources and `META-INF`.** `resources.arsc` and `AndroidManifest.xml` are readable
  by design: this is where the app's identity (label, package, permissions, component names) lives.
* **The names the JVM, the manifest and the native library resolve by name.** The audit still prints
  15 residues: `Lcom/zettabridge/core/ZBridge;`, the 13 `Lcom/zettabridge/launcher/*` components the
  manifest instantiates (the activities, the `GuestSupervisor` service that reopens a game whose
  process died, and `ZbApplication`), the `Stubs$*` shapes `GuestInstrumentation` swaps in by string,
  and the `com.zettabridge.core.` prefix `PluginClassLoader` routes on. `ZBridge` is half of every JNI symbol
  in `libzbridge.so` - whose own `.dynstr` still says `com_zettabridge` 11 times and which cannot be
  renamed without rebuilding the runtime - and a manifest that does not spell out its components
  cannot be instantiated. This is the floor, and it is also the one surviving pointer to the upstream
  project: removing it means rebuilding launcher and bridge together under a new namespace, with the
  JNI symbol names and the manifest changed in the same commit.

## Functionality: what "the same app" means here

R8 renames, inlines and merges; it was never allowed to delete anything reachable. Class counts, from
the baseline APK and this one:

| | baseline | obfuscated |
|---|---|---|
| classes in the dex | 152 | 83 |
| fields | 742 | 304 |
| methods with code | 820 | 394 |
| method ids | 1561 | 974 |
| classes that name a source file | 146 of 152 | 0 of 83 |
| methods carrying debug info | 754 of 820 | 0 of 394 |
| classes outside the keep list | 137 | 1 |

Every class of the app's own code is still *in* the APK; what changed shape is how R8 packed it:

* **The static-utility classes are gone as classes and present as code.** `ActivePlugin`, `AdHider`,
  `Diagnostics`, `GuestWindowStyle`, `MainLooperGuard`, `ManifestReader`, `PluginFiles`, `PluginStore`,
  `Reflect`, `Payloads`, `LauncherSettings`, `Nav`, `GuestHeartbeat`, `GuestRecovery`, `CrashPolicy`
  and the generated `R` have no mapping entry: R8 inlined their (static-only) members into their
  callers and dropped the now-empty shells - `usage.txt` lists the moved members, and their
  distinctive string constants and screen labels are all still in `classes.dex`;
* **A screen is not inlined away.** `Ui`, `Palette`, `SeedPlugin`, `RuntimeBundle`, `PluginRecord`,
  `GuestLease`, `CrashGuard` and the new `ImportActivity` are present as classes, because the
  framework instantiates the activity by name and the rest hold state or an instance;
* the `hiddenapibypass` dependency is renamed like everything else, and nothing in it names the
  upstream project.

So the honest form of "the same app" is: the dex holds 83 classes where the baseline held 152, and the
remainder of the baseline's classes are present as inlined code (the class table above is what the
audit prints for these two APKs); the two payload containers decrypt back to byte-identical
inputs (`build/verify_payloads.py` proves that on every build), and the packaged behaviour is checked
on the device. That is a behavioural check, not a metric subtracting 1% from a byte count.

## What the stability layer added to the shipping dex

The layer in `README-STABILITY.md` is behaviour, not narration, so it has to survive R8 in the
obfuscated build. Measured on the delivered pair (`mapping.txt` against `classes*.dex`):

* **`GuestSupervisor`** - the service, kept as a class, because the manifest instantiates it by name;
* **`GuestLease`** and **`CrashGuard`** - kept as classes and renamed like everything else;
* **`GuestHeartbeat`, `GuestRecovery` and `CrashPolicy`** - no mapping entry: R8 inlined their members
  into the callers. Their literals are in the obfuscated dex (the gate below reads them there), which
  is what says the decision table and the phase journal are in the shipping build and not only in the
  debuggable one.

`build/check_debug_build.py` checks the layer this way rather than by counting classes: three of its
string literals must be in **both** dexes and the `GuestSupervisor` element in **both** packaged
manifests. A class renamed away from the manifest, or inlined out of existence, would pass a check on
one of those two and fail this one.

## Verified on the device

Oppo CPH2669, Android 16, arm64; a clean `adb uninstall` then install of the delivered APK, whose
sha256 was compared against the installed `base.apk` (identical):

```
adb install project2/pokemon-go-emulated-obfuscated.apk          # Success
adb shell am start -n com.chucny.translator.bridge/com.zettabridge.launcher.LibraryActivity
```

* the launcher's three sections are the bottom navigation bar, on every screen: **Apps** (the list of
  imported apps - tapping one launches it), **Add apps** (import an APK, or install the one this build
  carries) and **Settings**. Each section is its own activity, so the bar switches windows rather than
  swapping pages, and the section being shown is marked with the Material 3 indicator pill
  (`#EADDFF` on the light scheme, `#4F378B` on the dark one);
* the screen is drawn in the M3 baseline palette, and the two schemes are the only ones there are. On
  this device, sampled out of a raw screencap: light `top app bar #FFFBFE`, `nav bar #F7F2FA`,
  `selected pill #EADDFF`, `group card #F7F2FA`; dark `#1C1B1F`, `#211F26`, `#4F378B`, `#211F26`.
  Nothing is read from the wallpaper - the same four colours come out on any phone, which is why the
  app looks the same everywhere. `build/check_theme.sh` (with `build/sample_ui_colours.py`) drives the
  Light/Dark rows and prints these readings, which is how they were measured;
* in Apps, the bundled app appears by itself after the automatic first-run seed, listed as
  `Pokemon GO` / `com.chucny.translator.bridge 0.29.0` / `32-bit armeabi-v7a: ready`. That status line
  is the proof for the payload layer: the client APK was decrypted and inflated out of its container
  (60,091,309 bytes) and imported, and the runtime bundle was decrypted out of the other one
  (1,649,691 bytes) into `files/zb/` and its libraries fixed up;
* the build with no client in it starts empty, as it should: fresh install, Apps shows only its "no
  apps yet" text, and Add apps offers **Import APK** alone - no install-the-bundled-app row, because
  `SeedPlugin.read` finds no descriptor. Verified on the installed obfuscated APK, not only in
  source;
* the footer renders: `content-desc="Open github.com/Chucny"` and `text="Made by Chucny"`, with the
  octicon drawn inside the button (measured on a raw screencap: 76x75 px of ink in an 80x80 box);
* tapping the app asks for location (the launcher must hold what the guest uses), and after granting
  it: `com.chucny.translator.bridge:guest` starts, `mCurrentFocus` becomes
  `com.chucny.translator.bridge/com.zettabridge.launcher.Stubs$SingleTask`, logcat shows
  `zbridge : GLES pname ...` and a `SurfaceView` acquiring 720x1539 buffers - the 32-bit guest runs
  with the stripped sysroot, the renamed extras/URIs, the new package name, and a runtime that came
  out of an encrypted container.

The run was repeated on the build that carries the dex post-pass and the bait, because a rewritten dex
has to keep loading, verifying and running:

* `adb install -r` over the previous build succeeded - the re-signed APK carries the same signer, so
  the upgrade path survives - and the installed `base.apk` sha256 matches the delivered file;
* the seed status line is `com.chucny.translator.bridge 0.29.0` / `32-bit armeabi-v7a: ready` again,
  which is the payload layer working out of the new containers;
* tapping the row started `com.chucny.translator.bridge:guest`, `mCurrentFocus` moved to
  `com.chucny.translator.bridge/com.zettabridge.launcher.Stubs$SingleTask`, and `AdrenoGLES-0` logged
  from the guest process - the 32-bit guest running through the JNI bridge of a dex whose debug
  metadata was rewritten. That path is what a broken dex would have failed.

One thing the device does **not** prove, and which the build gates cover instead: that the payloads
decode to exactly the staged inputs - a device run only shows that they decode to *something* the
guest accepts. The footer button's target,
`https://github.com/Chucny`, cannot be opened on this phone - it has no browser at all (a shell
`am start -a android.intent.action.VIEW -d https://...` fails the same way), so the click handler
reports the address instead of crashing; the intent it builds is the plain `ACTION_VIEW` one.

## The added footer

`LibraryActivity.credit()` appends a footer to the library screen: GitHub's own `mark-github` octicon
(primer/octicons, MIT) in an `ImageButton` whose content description is `Open github.com/Chucny`, the
text `Made by Chucny`, and a click handler that starts `ACTION_VIEW` for `https://github.com/Chucny`.
The icon is a 192x192 RGBA PNG in `drawable-xxxhdpi` rather than a VectorDrawable: Android's
`PathParser` renders that octicon path as a broken hollow shape, and rasterising it removes the
parser from the equation. Resource shrinking renames it (`res/zt.png` in the shipped APK).

## The copy vs. upstream

Everything below exists only in `project2/`; `ZettaBridge-main/android/launcher` is untouched.

| file | change |
|---|---|
| `build-obfuscated.sh` | the build: strip, pack, dictionary, Gradle, dex post-pass, zipalign, sign, verify, audit |
| `build/pack_payloads.py` | packs the runtime and the client into the encrypted containers, stages the bridge library, generates `Keys.java`, and fills `assets/` with the decoy layer |
| `build/verify_payloads.py` | decrypts the containers out of the built APK and compares them with the staged inputs |
| `build/strip_dex_debug.py` | clears every `debug_info_off` and `source_file_idx` in the packaged dex and recomputes its checksum and signature |
| `build/audit_apk.py` | the residue audit (names, symbol tables, banners, debug info, payload opacity) |
| `build/make_dictionary.py`, `build/verify_icon.py`, `build/icons/` | dictionary generation, icon tooling and the octicon sources |
| `build/check_theme.sh`, `build/sample_ui_colours.py` | drives the Light/Dark rows on a device and reads the painted colours back out of a screenshot |
| `app/src/main/res/values-night/styles.xml` | the application theme in dark mode, so the first frame is right before any code has read the setting |
| `app/src/main/java/.../Ui.java`, `.../Palette.java` | the one place the screens' look is decided: the M3 top app bar, groups, rows with their icon in a coloured circle, switches, pill buttons and the bottom navigation bar; the fixed M3 baseline palette |
| `app/src/main/java/.../Nav.java` | the three sections and the navigation bar that switches them |
| `app/src/main/java/.../ImportActivity.java` | the Add apps section: import an APK, or install the bundled one |
| `app/src/main/res/drawable/ic_*_24.xml` | the Material icons the rows and the bar carry, tinted from the palette at runtime |
| `strip-runtime-binaries.sh` | strips symbol tables and compiler banners from the whole staged runtime |
| `bundled-app.txt` | the descriptor, moved out of the asset tree so it cannot appear on the APK's asset list |
| `app/build.gradle.kts` | applicationId `com.chucny.translator.bridge`, R8 + resource shrinking + `debugSymbolLevel NONE`, `noCompress` for the payloads, asset/jniLibs/java sources from `build/payloads`, lint off (offline) |
| `app/proguard-rules.pro` | the keep list the JVM and the bridge need, `-repackageclasses`, `-allowaccessmodification`, `-overloadaggressively`, random identifier dictionaries, `-renamesourcefileattribute` |
| `app/src/main/AndroidManifest.xml` | label `Translator`, task affinities under `com.chucny.translator.bridge.*` |
| `app/src/main/java/.../Payloads.java` | new: the container reader (AES-CTR, bounded entries, inflate) |
| `app/src/main/java/.../RuntimeBundle.java` | installs the runtime out of the container instead of loose assets |
| `app/src/main/java/.../SeedPlugin.java` | reads the descriptor and the bundled APK out of the container |
| `app/src/main/java/.../ZBridge.java` | loads the bridge library under its generated name |
| `app/src/main/java/**` (20 files) | the footer, and the brand strings (`Translator`, new intent extra keys, URI schemes, thread names, logcat tag) |
| `app/src/main/res/` | `strings.xml` (footer strings), `drawable-xxxhdpi/ic_github.png` |

## Two builds from one source

One flag decides whether the client is inside the APK:

| | command | file | what the library starts as |
|---|---|---|---|
| bundled | `sh project2/build-obfuscated.sh` | `pokemon-go-emulated-obfuscated.apk` (~66 MB) | the client this build carries, installed on first launch |
| stocked | `sh project2/build-obfuscated.sh --no-bundled-app` | `chucnys-translator-no-bundled-app.apk` (~6.5 MB) | empty on a fresh install, with `Import APK` as the only way in |

The flag sets `BUNDLED_APP=none` for `build/pack_payloads.py`, which then writes no client container
and an empty `CLIENT_ASSET` in the generated `Keys`. Everything else - the runtime payload, the
chunking, the decoy layer, R8, the dex strip, the signing key - is the same code path, so the two
APKs cannot drift apart: they are one build with one input removed.

An empty `CLIENT_ASSET` is a supported state rather than a special case, which is what makes the
small build behave: `Payloads.open` over no pieces reaches the same `EOFException` a truncated
payload does, `SeedPlugin.read` turns that into "no bundled app", and the library screen then shows
nothing but the import button instead of an install button.

`build-debug.sh` refuses to reuse payloads staged by a `--no-bundled-app` build. Its `Keys` and its
assets would still agree with each other, so a debug build made from them would quietly be a build
with no bundled app - the one failure here that would not look like one.

The audit measures the opaque share of `assets/`, not of the whole archive, because that is the
question a payload container answers: with the client gone the archive is mostly `classes.dex` and
resources, which are readable in any Android app and which no container can hide. Measured: 100.0%
of the assets are opaque in the bundled build and 98.8% in the one with no client, and the
whole-archive figure is still printed next to it.

An upgrade in place keeps the library: the two APKs share an applicationId, so a device that already
has apps imported shows them after installing either one. "Empty" describes a fresh install.

## Installing

`applicationId` changed, so Android treats this as a different app: it installs **alongside** any
previous `com.chucny.pokemongo.emulated` build, with its own data directory (the bundled client is
imported again on first launch). To end up with only the new one:

```
adb uninstall com.chucny.pokemongo.emulated
```
