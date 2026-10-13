package com.zettabridge.launcher;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * The launcher's own settings: how its screens look, what a newly imported app is given, and the
 * switches that decide how much work the app does around a running guest.
 *
 * <p>These belong to the launcher. Anything that belongs to one app in the library lives on its
 * {@link PluginRecord} and is reached by long-pressing it; what is here applies to the launcher's
 * own windows, to the next import, or to both processes for the next launch.
 *
 * <p>Nothing here needs a migration path: an unknown or missing value falls back to the default,
 * which is also what an upgrade from a build that had no settings gets. That is also why a value an
 * older build stored for a skin this build no longer offers simply reads as the default.
 */
final class LauncherSettings {
    private static final String FILE = "launcher-settings";
    private static final String KEY_SKIN = "skin";
    private static final String KEY_PRECISE_FAULTS = "precise-faults-by-default";
    private static final String KEY_HIDE_ADS = "hide-ads-by-default";
    private static final String KEY_GL_DIAGNOSTICS = "gl-diagnostics-by-default";
    private static final String KEY_APP_LOG = "app-log";
    private static final String KEY_RUNTIME_REPORT = "persist-runtime-report";
    private static final String KEY_REOPEN = "reopen-after-stop";
    private static final String KEY_RETRY = "retry-failed-launch";
    private static final String KEY_CLIPBOARD = "copy-failures-to-clipboard";

    /**
     * Light or dark - there is nothing else to choose.
     *
     * <p>An app on Material You does not pick its own colours: the palette comes from the phone's
     * wallpaper (see {@link Palette}), so the only thing left for a user to choose is which of the
     * two schemes to use. Each is a whole theme (see {@code res/values/styles.xml}) rather than a
     * colour a screen paints itself with, and it is applied before the activity's window exists -
     * which is what makes the dialogs, the menus and every widget in a code-built layout follow it
     * with no view being coloured by hand.
     */
    enum Skin {
        LIGHT("Light"),
        DARK("Dark");

        final String label;

        Skin(String label) {
            this.label = label;
        }
    }

    private LauncherSettings() {}

    private static SharedPreferences prefs(Context context) {
        Context app = context.getApplicationContext();
        return (app != null ? app : context).getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    private static boolean get(Context context, String key, boolean fallback) {
        try {
            return prefs(context).getBoolean(key, fallback);
        } catch (Throwable t) {
            // A settings file that cannot be read must not be the reason a launch fails.
            return fallback;
        }
    }

    private static void put(Context context, String key, boolean value) {
        prefs(context).edit().putBoolean(key, value).apply();
    }

    /**
     * The chosen scheme - Light or Dark.
     *
     * <p>With nothing stored yet the phone's own dark setting decides, which is the behaviour a
     * Material You app has by default: an app that has never been configured looks like the rest of
     * the phone rather than like the app's own idea of light.
     */
    static Skin skin(Context context) {
        String stored = prefs(context).getString(KEY_SKIN, null);
        for (Skin skin : Skin.values()) {
            if (skin.name().equals(stored)) return skin;
        }
        return isNight(context) ? Skin.DARK : Skin.LIGHT;
    }

    static void skin(Context context, Skin skin) {
        prefs(context).edit().putString(KEY_SKIN, skin.name()).apply();
    }

    /** The XML theme a scheme means: one scheme, one theme. */
    static int themeRes(Context context) {
        return skin(context) == Skin.DARK ? R.style.SkinDark : R.style.SkinLight;
    }

    /** Whether the phone is in dark mode; the default scheme and the palette both ask. */
    static boolean isNight(Context context) {
        int mode = context.getResources().getConfiguration().uiMode
                & android.content.res.Configuration.UI_MODE_NIGHT_MASK;
        return mode == android.content.res.Configuration.UI_MODE_NIGHT_YES;
    }

    // ------------------------------------------------------- what a guest costs

    /**
     * Whether a translated (32-bit) app is given precise memory faults: a fault stops at the exact
     * faulting instruction with the guest registers committed, instead of at the end of the
     * translated block. It is what the crash reports and Unity's own null checks are written
     * against, and it costs roughly 2x on integer code - so it defaults on, and this is the switch
     * that trades that accuracy back for speed.
     */
    static boolean preciseFaultsByDefault(Context context) {
        return get(context, KEY_PRECISE_FAULTS, true);
    }

    static void preciseFaultsByDefault(Context context, boolean value) {
        put(context, KEY_PRECISE_FAULTS, value);
    }

    static boolean hideAdsByDefault(Context context) {
        return get(context, KEY_HIDE_ADS, false);
    }

    static void hideAdsByDefault(Context context, boolean value) {
        put(context, KEY_HIDE_ADS, value);
    }

    static boolean glDiagnosticsByDefault(Context context) {
        return get(context, KEY_GL_DIAGNOSTICS, false);
    }

    static void glDiagnosticsByDefault(Context context, boolean value) {
        put(context, KEY_GL_DIAGNOSTICS, value);
    }

    // ----------------------------------------------------------- what a run costs

    /**
     * The detailed app log. On by default, and only the debuggable build has one to switch
     * (see {@link DebugLog#available()}): it is what turns a session into a file that explains
     * every window and every death, and what makes the debug build measure itself while it runs.
     */
    static boolean appLog(Context context) {
        return get(context, KEY_APP_LOG, true);
    }

    static void appLog(Context context, boolean value) {
        put(context, KEY_APP_LOG, value);
    }

    /**
     * Whether the :guest process persists its runtime report to
     * {@code files/zb-runtime-report.txt} as it changes. On by default: it is the only account a
     * guest that dies without a Java or JNI error leaves behind.
     */
    static boolean persistRuntimeReport(Context context) {
        return get(context, KEY_RUNTIME_REPORT, true);
    }

    static void persistRuntimeReport(Context context, boolean value) {
        put(context, KEY_RUNTIME_REPORT, value);
    }

    /**
     * Whether a :guest process that dies with a game window on screen is reopened by the launcher.
     * On by default; see {@link GuestRecovery} for the policy, which caps how often it happens.
     */
    static boolean reopenAfterStop(Context context) {
        return get(context, KEY_REOPEN, true);
    }

    static void reopenAfterStop(Context context, boolean value) {
        put(context, KEY_REOPEN, value);
    }

    /**
     * Whether a launch that failed while the app is installed is retried once in a fresh :guest
     * process before the user is told. On by default: that retry is what fixes the intermittent
     * failures (a window or a proxy load that did not take hold).
     */
    static boolean retryFailedLaunch(Context context) {
        return get(context, KEY_RETRY, true);
    }

    static void retryFailedLaunch(Context context, boolean value) {
        put(context, KEY_RETRY, value);
    }

    /**
     * Whether a failure reason is put on the clipboard by itself. On by default, because some ROMs
     * drop third-party logcat and some phones have no file manager to open the error file with.
     */
    static boolean copyFailuresToClipboard(Context context) {
        return get(context, KEY_CLIPBOARD, true);
    }

    static void copyFailuresToClipboard(Context context, boolean value) {
        put(context, KEY_CLIPBOARD, value);
    }

    /**
     * The settings a newly imported app starts with.
     *
     * <p>Called from {@link PluginStore} after the record is built and before it is written, so an
     * import that fails later leaves nothing behind. Everything here is a default, not a policy:
     * each of these can be changed for one app afterwards by long-pressing it in the library.
     */
    static void applyImportDefaults(Context context, PluginRecord record) {
        record.hideAds = hideAdsByDefault(context);
        record.diagnostics = glDiagnosticsByDefault(context);
        // Also what a translated app cannot start without when it is left on (see PluginStore):
        // every one of these is read by ZBridge before the guest runtime is constructed.
        record.preciseFaults = preciseFaultsByDefault(context);
    }

    /** Every setting and its current value, for the settings screen's "effective settings" report. */
    static String describe(Context context) {
        StringBuilder out = new StringBuilder();
        out.append("skin                  : ").append(skin(context).name()).append('\n');
        out.append("preciseFaultsDefault  : ").append(preciseFaultsByDefault(context)).append('\n');
        out.append("glDiagnosticsDefault  : ").append(glDiagnosticsByDefault(context)).append('\n');
        out.append("hideAdsDefault        : ").append(hideAdsByDefault(context)).append('\n');
        out.append("appLog                : ").append(appLog(context))
                .append(DebugLog.available() ? "" : " (no effect: this build compiles the log out)").append('\n');
        out.append("persistRuntimeReport  : ").append(persistRuntimeReport(context)).append('\n');
        out.append("reopenAfterStop       : ").append(reopenAfterStop(context)).append('\n');
        out.append("retryFailedLaunch     : ").append(retryFailedLaunch(context)).append('\n');
        out.append("copyFailuresToClipboard: ").append(copyFailuresToClipboard(context)).append('\n');
        return out.toString();
    }
}
