package com.zettabridge.launcher;

import android.app.Activity;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.util.List;

/**
 * The launcher's settings: its theme, how a translated app is run, what a new import is given, what
 * the launcher writes down while a game is running, and everything a failure on somebody else's phone
 * has to be described with.
 *
 * <p>The screen is the Pixel's own settings shape (see {@link Ui}): a large title across the top, one
 * rounded group per subject, each row carrying its icon in a coloured circle, switches on the rows
 * that have one, and the app's own Material 3 palette throughout. The only thing there is to choose
 * about the look is Light or Dark, and the font of the design is fixed rather than taken from the
 * phone - this app runs on phones of every make and looks the same on all of them.
 *
 * <p>The diagnose section is the other half of the point of this screen. This app is expected to run
 * on devices nobody here has, and the reports it leaves behind are the only evidence a run on such a
 * device produces (some ROMs drop third-party logcat output entirely, so a file is the primary copy -
 * see {@link Diagnostics}). Each action below therefore puts its text on the clipboard as well, which
 * is the one way a phone with no file manager and no browser can still hand the evidence over.
 *
 * <p>What is here is launcher-wide. An app's own switches (hide ads, memory faults, GL diagnostics)
 * stay on its long-press menu in the library; the three under Performance are the defaults a new
 * import starts with, which is the only way those can follow the setting without a reimport.
 */
public class SettingsActivity extends Activity {
    private static final String TAG = "Translator";
    /** The clipboard is not a place for an 8 MB log; the newest part of it is the useful part. */
    private static final int COPY_LIMIT = 400_000;

    private Ui ui;

    @Override
    protected void onCreate(Bundle state) {
        // Before the window exists: the theme is fixed by the time onCreate runs, so the scheme is
        // resolved here rather than by the theme itself.
        setTheme(LauncherSettings.themeRes(this));
        super.onCreate(state);
        ui = new Ui(this);
        ui.edgeToEdge();

        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(0, 0, 0, ui.dp(32));

        theme(column);
        performance(column);
        imports(column);
        logging(column);
        reliability(column);
        diagnose(column);
        about(column);

        setContentView(ui.screen(getString(R.string.action_settings), column,
                Nav.bar(this, ui, Nav.Dest.SETTINGS)));
    }

    /** Adds a section label and returns the group that follows it. */
    private LinearLayout section(LinearLayout column, int label) {
        column.addView(ui.section(getString(label)), Ui.match());
        LinearLayout group = ui.group();
        column.addView(group, ui.groupParams());
        return group;
    }

    /** Adds a row's supporting note, which sits under a group rather than inside it. */
    private void note(LinearLayout column, int text) {
        column.addView(ui.summary(getString(text)), Ui.match());
        column.getChildAt(column.getChildCount() - 1).setPadding(
                ui.dp(28), ui.dp(4), ui.dp(28), 0);
    }

    // ------------------------------------------------------------------- theme

    /** Light or Dark, shown as the swatch each one is. Nothing else: see {@link Palette}. */
    private void theme(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_look);
        LauncherSettings.Skin current = LauncherSettings.skin(this);
        boolean first = true;
        for (LauncherSettings.Skin skin : LauncherSettings.Skin.values()) {
            if (!first) group.addView(ui.divider(), ui.hairline());
            first = false;
            int swatch = Palette.scheme(this, skin).primary;
            group.addView(ui.schemeRow(skin.label, swatch, skin == current, () -> {
                LauncherSettings.skin(this, skin);
                // A theme cannot be applied to an existing window, so the window is rebuilt. That
                // re-runs onCreate, which re-reads the palette too.
                recreate();
            }), Ui.match());
        }
        note(column, R.string.settings_look_note);
    }

    // ------------------------------------------------------------- performance

    /**
     * What a translated app is given when it is imported. Precise faults is the switch that decides
     * whether a 32-bit app runs at full speed or at half of it (see {@link LoadedPlugin}), so it is
     * the first setting on this screen, and it defaults on.
     */
    private void performance(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_performance);
        group.addView(ui.toggleRow(R.drawable.ic_bolt_24, getString(R.string.settings_precise_faults),
                getString(R.string.settings_precise_faults_note),
                LauncherSettings.preciseFaultsByDefault(this),
                value -> LauncherSettings.preciseFaultsByDefault(this, value)), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.toggleRow(R.drawable.ic_contrast_24, getString(R.string.settings_gl_diagnostics),
                getString(R.string.settings_gl_diagnostics_note),
                LauncherSettings.glDiagnosticsByDefault(this),
                value -> LauncherSettings.glDiagnosticsByDefault(this, value)), Ui.match());
        note(column, R.string.settings_performance_note);
    }

    // ----------------------------------------------------------------- imports

    private void imports(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_imports);
        group.addView(ui.toggleRow(R.drawable.ic_block_24, getString(R.string.settings_hide_ads),
                getString(R.string.settings_hide_ads_note), LauncherSettings.hideAdsByDefault(this),
                value -> LauncherSettings.hideAdsByDefault(this, value)), Ui.match());
    }

    // ----------------------------------------------------------------- logging

    /**
     * What this app writes down while a game runs. Both are on by default and both cost something:
     * the detailed app log reads the runtime report every few seconds, and persisting the report
     * means the :guest process rewrites a file whenever it changes.
     */
    private void logging(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_logging);
        if (DebugLog.available()) {
            group.addView(ui.toggleRow(R.drawable.ic_description_24, getString(R.string.settings_app_log),
                    getString(R.string.settings_app_log_note), DebugLog.enabled(this),
                    value -> DebugLog.setEnabled(this, value)), Ui.match());
        } else {
            // The release build has no log to switch. Saying which build this is beats a switch that
            // silently does nothing.
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.VERTICAL);
            row.setPadding(ui.dp(20), ui.dp(14), ui.dp(20), ui.dp(14));
            row.addView(ui.title(getString(R.string.settings_app_log)));
            row.addView(ui.summary(getString(R.string.settings_app_log_release)));
            group.addView(row, Ui.match());
        }
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.toggleRow(R.drawable.ic_save_24, getString(R.string.settings_runtime_report),
                getString(R.string.settings_runtime_report_note),
                LauncherSettings.persistRuntimeReport(this),
                value -> LauncherSettings.persistRuntimeReport(this, value)), Ui.match());
    }

    // ------------------------------------------------------------- reliability

    private void reliability(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_reliability);
        group.addView(ui.toggleRow(R.drawable.ic_restore_24, getString(R.string.settings_reopen),
                getString(R.string.settings_reopen_note), LauncherSettings.reopenAfterStop(this),
                value -> LauncherSettings.reopenAfterStop(this, value)), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.toggleRow(R.drawable.ic_refresh_24, getString(R.string.settings_retry),
                getString(R.string.settings_retry_note), LauncherSettings.retryFailedLaunch(this),
                value -> LauncherSettings.retryFailedLaunch(this, value)), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.toggleRow(R.drawable.ic_copy_24, getString(R.string.settings_clipboard),
                getString(R.string.settings_clipboard_note),
                LauncherSettings.copyFailuresToClipboard(this),
                value -> LauncherSettings.copyFailuresToClipboard(this, value)), Ui.match());
    }

    // ---------------------------------------------------------------- diagnose

    private void diagnose(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_diagnose);
        group.addView(ui.actionRow(R.drawable.ic_assessment_24,
                getString(R.string.settings_copy_run_report), null, this::copyRunReport), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.actionRow(R.drawable.ic_warning_24,
                getString(R.string.settings_copy_errors), null, this::copyErrors), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.actionRow(R.drawable.ic_description_24,
                getString(R.string.settings_copy_debug_log), null, this::copyAppLog), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.actionRow(R.drawable.ic_phone_24,
                getString(R.string.settings_device_info), null, this::copyDeviceInfo), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.actionRow(R.drawable.ic_tune_24,
                getString(R.string.settings_effective_settings), null, this::copySettings), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.actionRow(R.drawable.ic_stop_24,
                getString(R.string.settings_stop_app), null, this::stopApp), Ui.match());
        note(column, R.string.settings_diagnose_note);
    }

    // ------------------------------------------------------------------- about

    /** What this build is, and the facts worth being able to check. */
    private void about(LinearLayout column) {
        LinearLayout group = section(column, R.string.settings_about);
        group.addView(ui.valueRow(getString(R.string.settings_about_version), versionName()), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.valueRow(getString(R.string.settings_about_palette), paletteName()), Ui.match());
        group.addView(ui.divider(), ui.hairline());
        group.addView(ui.valueRow(getString(R.string.settings_about_bundled), bundledApp()), Ui.match());
        note(column, R.string.made_by);
    }

    /** Which scheme is in use - a fact worth being able to check in a report. */
    private String paletteName() {
        return ui.palette().dark ? "dark" : "light";
    }

    // The actions. Each one reports what it did rather than failing quietly: a button that does
    // nothing is indistinguishable from a broken one on a device nobody can inspect.

    private void copyRunReport() {
        String report = Diagnostics.readRuntimeReport(this);
        copy(getString(R.string.run_report_label, getString(R.string.app_name)), report);
    }

    private void copyErrors() {
        copy(getString(R.string.error_report_label, getString(R.string.app_name)), Diagnostics.readErrors(this));
    }

    private void copyAppLog() {
        copy(getString(R.string.app_name) + " app log", Diagnostics.readAppLog(this));
    }

    private void copyDeviceInfo() {
        copy(getString(R.string.app_name) + " device info", deviceInfo());
    }

    /** Every switch on this screen and what it is set to, in one paste. */
    private void copySettings() {
        copy(getString(R.string.app_name) + " settings", LauncherSettings.describe(this));
    }

    private void copy(String label, String text) {
        if (text == null || text.isEmpty()) {
            Toast.makeText(this, R.string.settings_nothing_copied, Toast.LENGTH_LONG).show();
            return;
        }
        if (text.length() > COPY_LIMIT) {
            // The end of a log is the part that explains an ending.
            text = "(the oldest part of this report was dropped: it is " + text.length()
                    + " characters)\n" + text.substring(text.length() - COPY_LIMIT);
        }
        boolean copied = Diagnostics.copy(this, label, text);
        Toast.makeText(this, copied ? R.string.settings_copied : R.string.settings_nothing_copied,
                Toast.LENGTH_LONG).show();
    }

    private void stopApp() {
        boolean stopped = ZbApplication.stopGuestProcess(this);
        Toast.makeText(this, stopped ? R.string.settings_stopped : R.string.settings_nothing_running,
                Toast.LENGTH_LONG).show();
    }

    /**
     * Everything a report from another phone needs to be reproducible: the device, the platform, the
     * app itself, and the per-app facts (which ABI was picked, whether that ABI had to be translated,
     * and which of the diagnostic switches are on). The 32-bit ABI list matters most - an empty one
     * means this device runs no arm32 code natively, which is the whole reason a translated guest
     * exists.
     */
    private String deviceInfo() {
        StringBuilder out = new StringBuilder();
        out.append("launcher       : ").append(getString(R.string.app_name)).append(' ')
                .append(versionName()).append('\n');
        out.append("package        : ").append(getPackageName()).append('\n');
        out.append("device         : ").append(Build.MANUFACTURER).append(' ').append(Build.MODEL)
                .append(" (").append(Build.DEVICE).append(")\n");
        out.append("platform       : Android ").append(Build.VERSION.RELEASE)
                .append(" (API ").append(Build.VERSION.SDK_INT).append(")\n");
        out.append("build          : ").append(Build.DISPLAY).append('\n');
        out.append("fingerprint    : ").append(Build.FINGERPRINT).append('\n');
        out.append("abis supported : ").append(join(Build.SUPPORTED_ABIS)).append('\n');
        out.append("abis 64-bit    : ").append(join(Build.SUPPORTED_64_BIT_ABIS)).append('\n');
        out.append("abis 32-bit    : ").append(join(Build.SUPPORTED_32_BIT_ABIS))
                .append(join(Build.SUPPORTED_32_BIT_ABIS).isEmpty()
                        ? "  (none: every guest here has to be translated)" : "").append('\n');
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            out.append("soc            : ").append(Build.SOC_MANUFACTURER).append(' ')
                    .append(Build.SOC_MODEL).append('\n');
        }
        out.append("palette        : ").append(paletteName()).append('\n');
        out.append("app log        : ")
                .append(DebugLog.available() ? (DebugLog.enabled(this) ? "detailed (debug build)" : "off")
                        : "none (release build)").append('\n');
        out.append("bundled app    : ").append(bundledApp()).append('\n');
        List<PluginRecord> plugins = PluginStore.list(this);
        out.append("imported apps  : ").append(plugins.size()).append('\n');
        for (PluginRecord r : plugins) {
            out.append("  - ").append(r.label).append("  ").append(r.packageName).append('\n');
            out.append("    version    : ").append(r.versionName).append(" targetSdk ").append(r.targetSdk)
                    .append('\n');
            out.append("    abis       : ").append(join(r.abis.toArray(new String[0])))
                    .append(" using ").append(r.selectedAbi)
                    .append(r.isTranslated() ? " (translated)" : " (native)").append('\n');
            out.append("    switches   : preciseFaults=").append(r.preciseFaults)
                    .append(" glDiagnostics=").append(r.diagnostics)
                    .append(" hideAds=").append(r.hideAds).append('\n');
            out.append("    state      : ").append(r.status()).append('\n');
        }
        out.append("\n").append(LauncherSettings.describe(this));
        return out.toString();
    }

    private String bundledApp() {
        try {
            java.util.Properties bundled = SeedPlugin.read(this);
            return bundled == null ? "none (this build ships no app)"
                    : bundled.getProperty("package", "unknown");
        } catch (Throwable t) {
            // Reading the payload means decrypting an asset; a failure here is a fact about the
            // build, and the report is more useful with it stated than with the action failing.
            return "cannot be read: " + t;
        }
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            Log.w(TAG, "cannot read the version name: " + e);
            return "unknown";
        }
    }

    private static String join(String[] values) {
        if (values == null || values.length == 0) return "";
        StringBuilder out = new StringBuilder();
        for (String value : values) {
            if (out.length() > 0) out.append(", ");
            out.append(value);
        }
        return out.toString();
    }
}
