package com.zettabridge.core;

import com.zettabridge.launcher.Keys;

import java.io.IOException;

/**
 * JNI surface of the bridge library in the launcher's :guest process. The library ships under a
 * name this build generated (see {@link Keys#BRIDGE_LIBRARY} and build/pack_payloads.py), so the
 * APK's native-library directory says nothing about what it is.
 *
 * <p>Order for an arm32 plugin, all in one :guest process:
 * <ol>
 *   <li>Import: {@link #fixGuestLibrary} on every extracted arm32 library before the import is
 *       marked complete.</li>
 *   <li>Launch, before plugin code runs (before the plugin Application is created):
 *       {@link #activatePlugin} with the plugin root, its targetSdk and its class loader.</li>
 *   <li>Plugin code calls System.loadLibrary; the plugin class loader returns
 *       filesDir/plugins/&lt;pkg&gt;/proxy/lib&lt;name&gt;.so, and that proxy's JNI_OnLoad calls
 *       {@link #onProxyLoaded}.</li>
 * </ol>
 *
 * <p>Runtime layout (fixed in the native code, so the names below are what the extracted files are
 * called on disk; the APK itself holds them only inside the encrypted payload):
 * filesDir/zb/sysroot/system/..., filesDir/zb/guest/zbhost,
 * filesDir/zb/guest/lib/{libzbcompat.so,libzbjni.so,...}, filesDir/plugins/&lt;pkg&gt;/lib/lib&lt;name&gt;.so.
 *
 * <p>Failures are final for the process. ART replaces the UnsatisfiedLinkError thrown here by a
 * generic "JNI_ERR returned from JNI_OnLoad" and never calls JNI_OnLoad again for the same path;
 * the guest runtime is started once and never restarted; only one plugin can be active. Show
 * {@link #lastLoadError} (or {@link #loadError}) on screen, and restart the :guest process before
 * retrying or switching plugins.
 */
public final class ZBridge {
    static {
        System.loadLibrary(Keys.BRIDGE_LIBRARY);
    }

    private ZBridge() {}

    /**
     * Blocks until the guest exits.
     *
     * @param sysroot directory holding system/bin/linker and system/lib (arm32 bionic)
     * @param argv argv[0] is the absolute path of the guest executable
     * @param envp guest environment ("NAME=VALUE"), or null to pass this process's environment
     * @return the guest exit status, or 128 + signal for a guest crash
     */
    public static native int runExecutable(String sysroot, String[] argv, String[] envp);

    /**
     * Binds this process's guest JNI runtime to one plugin. Calling it again with the same plugin
     * root, targetSdk and class loader is a no-op.
     *
     * @param pluginRoot filesDir/plugins/&lt;pkg&gt; (symlinks are resolved)
     * @param targetSdk the plugin's targetSdkVersion, passed to the guest linker
     * @param classLoader the plugin class loader; retained for the life of the process
     * @throws IllegalStateException when the layout is incomplete, or another plugin, targetSdk
     *     or class loader is already active in this process
     */
    public static native void activatePlugin(String pluginRoot, int targetSdk, ClassLoader classLoader);

    /**
     * Called by libzbproxy.so from JNI_OnLoad. Loads the arm32 library that belongs to the proxy,
     * binds its Java_* exports and runs its JNI_OnLoad on the calling thread. Results are memoized
     * by canonical proxy path.
     *
     * @return the guest JNI version (JNI_VERSION_1_2, 1_4 or 1_6)
     * @throws UnsatisfiedLinkError with the detailed reason
     */
    public static native int onProxyLoaded(String proxyPath);

    /**
     * Called by libzbproxy.so from ANativeActivity_onCreate, which is what the framework's
     * android.app.NativeActivity looks up in the library the manifest names. Builds the 32-bit
     * activity the guest sees and calls the guest's own ANativeActivity_onCreate.
     *
     * @param activity the host ANativeActivity* the framework passed, as a pointer value
     * @param savedState the saved-state block, or 0
     * @param savedStateSize its size in bytes
     * @param proxyPath the proxy library the framework loaded, which names the arm32 library
     * @return true when the guest activity is built and its callbacks are installed; false leaves
     *     the framework's activity untouched, and the reason is in the runtime report
     */
    public static native boolean onNativeActivityCreated(long activity, long savedState, long savedStateSize,
                                                         String proxyPath);

    /** The stored failure message for a proxy path, or null if it did not fail. */
    public static native String loadError(String proxyPath);

    /** The most recent proxy load failure message, or null. */
    public static native String lastLoadError();

    /**
     * Starts persisting the runtime report (see {@link #runtimeReport}) to a file, rewritten
     * atomically whenever the report changes. Call once per :guest process, before plugin code
     * runs: the report then survives a :guest process that dies without a Java or JNI error.
     *
     * @param path absolute file path; its directory must already exist
     * @return true when the first write succeeded and persisting is on
     */
    public static native boolean setReportFile(String path);

    /**
     * What this process recorded about the guest run, as short plain text: the active plugin,
     * proxy loads, guest JNI_OnLoad calls, registered natives, unimplemented host calls (with the
     * first one by name, for example "libGLESv2.so glCreateProgram") and how the guest ended.
     *
     * <p>ROMs that drop third-party logcat output make this the only way to see what happened.
     */
    public static native String runtimeReport();

    /**
     * Applies the arm32 import fixups (absolute DT_NEEDED to basename, DT_TEXTREL marker) in place.
     *
     * @return "unchanged", "changed: ..." or "skipped: reason" for a file that is not an ARM ELF32
     *     shared library
     * @throws IOException for a malformed ELF file or an I/O error
     */
    public static native String fixGuestLibrary(String path) throws IOException;

    /**
     * Sets ZB_PRECISE_FAULTS in this process's environment (setenv), so a memory fault stops at
     * the exact faulting instruction with registers committed instead of at the end of the
     * translated block. Diagnostic only: roughly 2x slower on integer code. Must be called before
     * the guest runtime/Process for this plugin is constructed, since it is process-lifetime and
     * reads the variable only once.
     */
    public static native void setPreciseFaults(boolean enabled);

    /**
     * Sets ZB_GL_DIAGNOSTICS in this process's environment (setenv), which turns on the GL
     * instrumentation: whole-framebuffer readbacks, per-draw pixel diffs and ASCII frame maps.
     * Debugging only and very slow; off in a normal run. Must be called before the guest JNI
     * runtime of this plugin is constructed, since it reads the variable only once.
     */
    public static native void setGlDiagnostics(boolean enabled);
}
