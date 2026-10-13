package com.zettabridge.launcher;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.util.Log;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * What the user sees when a launch fails, instead of a window that closes the moment it appears.
 *
 * <p>This screen exists as its own activity because the guest's launch entry point is declared with
 * a NoDisplay theme, and the framework kills any such activity that does not finish before its
 * onResume completes ("did not call finish() prior to onResume() completing" - measured on the
 * device, it takes the whole :guest process with it). So the entry point cannot report anything
 * itself: it hands the reason to this activity, which has a real window, runs in the launcher
 * process, and stays until the user acts.
 *
 * <p>Where the reason comes from, in the order it is useful: the exception the launch threw, the
 * bridge's own last failure, the plugin's state in the library, and whether the bundled app is
 * imported. The one failure this activity can repair by itself is an app that is simply not
 * imported yet - the usual reason a shortcut opens and closes at once, because on a fresh install
 * the library's seeding can still be running when the first launch arrives. That repair happens
 * here, off the main thread, and then the launch is retried.
 *
 * <p>Both things this screen does on its own - retrying once, and putting the reason on the
 * clipboard - are switches in the settings screen (Reliability), and both default on.
 */
public class LaunchFailureActivity extends Activity {
    private static final String TAG = "Translator";
    /** Where a repair attempt is recorded, so the retry that follows cannot start a restart loop. */
    private static final String PREFS = "translator.launches";
    static final String EXTRA_PACKAGE = "com.chucny.translator.bridge.FAILURE.PACKAGE";
    static final String EXTRA_TITLE = "com.chucny.translator.bridge.FAILURE.TITLE";
    static final String EXTRA_DETAIL = "com.chucny.translator.bridge.FAILURE.DETAIL";
    /** True when importing the bundled app might be what unblocks this launch. */
    static final String EXTRA_REPAIR = "com.chucny.translator.bridge.FAILURE.REPAIR";
    /** True when the app is installed and the failure looked transient, so one retry is worth it. */
    static final String EXTRA_AUTO = "com.chucny.translator.bridge.FAILURE.AUTO";
    /** A retry that restarts :guest is expensive and, if the failure is real, useless on repeat. */
    private static final long AUTO_RETRY_COOLDOWN_MS = 60_000;

    private String packageName;
    private LinearLayout actions;
    private TextView body;
    /** This screen's Material 3 widgets, in the palette the instant it was built in. */
    private Ui ui;

    static Intent intent(Context context, String packageName, String title, String detail, boolean repair,
                         boolean autoRetry) {
        return new Intent(context, LaunchFailureActivity.class)
                .putExtra(EXTRA_PACKAGE, packageName)
                .putExtra(EXTRA_TITLE, title)
                .putExtra(EXTRA_DETAIL, detail)
                .putExtra(EXTRA_REPAIR, repair)
                .putExtra(EXTRA_AUTO, autoRetry)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    @Override
    protected void onCreate(Bundle state) {
        // Before the window exists, like every other screen: the failure screen is part of this app
        // and follows the chosen colour scheme (the manifest gives it a theme so a window exists).
        setTheme(LauncherSettings.themeRes(this));
        super.onCreate(state);
        ui = new Ui(this);
        ui.edgeToEdge();
        packageName = getIntent().getStringExtra(EXTRA_PACKAGE);
        String title = getIntent().getStringExtra(EXTRA_TITLE);
        String detail = getIntent().getStringExtra(EXTRA_DETAIL);
        boolean repair = getIntent().getBooleanExtra(EXTRA_REPAIR, false);
        boolean autoRetry = getIntent().getBooleanExtra(EXTRA_AUTO, false)
                && LauncherSettings.retryFailedLaunch(this);
        DebugLog.note("failure screen for " + packageName + ": " + title
                + (repair ? " (trying the import first)" : "") + (autoRetry ? " (transient, retrying once)" : ""));
        screen(title == null ? "Cannot open this app" : title,
                detail == null ? "The launch failed and gave no reason." : detail);
        if (repair && tryRepair()) return;
        if (autoRetry && autoRetryAllowed()) {
            // This is the failure that comes and goes: the app is installed and the launch still
            // failed, which is what a window or a proxy load that did not take hold looks like. One
            // retry in a fresh :guest process usually opens it; repeating that forever would not.
            DebugLog.note("retrying " + packageName + " once in a fresh :guest process");
            relaunch();
            return;
        }
        buttons();
    }

    private boolean autoRetryAllowed() {
        if (packageName == null) return false;
        String key = packageName + ":auto";
        long last = getSharedPreferences(PREFS, MODE_PRIVATE).getLong(key, 0);
        long age = System.currentTimeMillis() - last;
        if (last != 0 && age < AUTO_RETRY_COOLDOWN_MS) {
            DebugLog.note("not retrying " + packageName + " automatically again: it was retried "
                    + age + " ms ago and failed then too");
            return false;
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putLong(key, System.currentTimeMillis()).apply();
        return true;
    }

    /**
     * Imports the bundled app when that is what was missing, then relaunches. Returns false when
     * there is nothing to import, so the caller shows the failure instead.
     */
    private boolean tryRepair() {
        if (SeedPlugin.read(this) == null) return false;
        if (PluginStore.find(this, packageName) != null) return false;
        body.setText("This app is not in the library yet. Importing it now, then opening it. "
                + "This happens once, right after a fresh install.");
        clearActions();
        // The retry happens in a fresh :guest process, so the fact that this app was just repaired
        // has to outlive this activity; otherwise a permanently missing app would repair and retry
        // forever. GuestLaunchActivity reads this and refuses a second repair inside the cooldown.
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putLong(packageName, System.currentTimeMillis()).apply();
        new Thread(() -> {
            String failure = null;
            try {
                PluginRecord record = SeedPlugin.installIfMissing(this);
                if (record == null) failure = "this build carries no bundled app";
            } catch (Throwable t) {
                Log.e(TAG, "installing the bundled app failed", t);
                failure = t.toString();
            }
            final String reason = failure;
            runOnUiThread(() -> {
                if (reason != null) {
                    DebugLog.failure("cannot import the bundled app while repairing " + packageName,
                            new IllegalStateException(reason));
                    body.setText("Importing the bundled app failed: " + reason);
                    buttons();
                    return;
                }
                DebugLog.note("imported the bundled app; retrying " + packageName);
                relaunch();
            });
        }, "translator-repair").start();
        return true;
    }

    private void buttons() {
        clearActions();
        // The two pills a Pixel puts at the end of a dialog: the action, then the way out.
        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(android.view.Gravity.END);
        buttons.setPadding(ui.dp(8), ui.dp(8), ui.dp(8), ui.dp(8));
        if (packageName != null) {
            buttons.addView(ui.pillButton(R.drawable.ic_refresh_24, "Try again", this::relaunch));
        }
        buttons.addView(ui.pillButton(R.drawable.ic_close_24, "Close", this::finish));
        actions.addView(buttons, Ui.match());
        if (!body.getText().toString().contains("zb-errors.txt")) {
            // zb-errors.txt is written by both builds. Deliberately no mention of the debug build's
            // zb-app-log.txt: a release build does not write one, and pointing a user at a file that
            // only the diagnostic build creates is worse than saying nothing.
            body.append("\n\nThis text is on the clipboard and in files/zb-errors.txt.");
        }
        if (LauncherSettings.copyFailuresToClipboard(this)) {
            Diagnostics.copy(this, getString(R.string.could_not_open, getString(R.string.app_name)),
                    String.valueOf(body.getText()));
        }
    }

    private void clearActions() {
        actions.removeAllViews();
    }

    /**
     * The retry goes through the switch activity on purpose: the guest runtime is process-lifetime,
     * so a plugin that failed to load leaves :guest in a state it cannot be retried in. The switch
     * activity ends that process and starts a fresh one before opening the app again.
     */
    private void relaunch() {
        startActivity(PluginSwitchActivity.intent(this, packageName));
        finish();
    }

    /**
     * Builds the screen: a heading, a selectable body, and a place for the actions - all in the same
     * Google look as the settings screen, so a failure does not look like a different app.
     */
    private void screen(String title, String detail) {
        int pad = ui.dp(24);
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(pad, pad, pad, pad);

        column.addView(ui.heading(title));
        body = ui.body(detail);
        body.setTextIsSelectable(true);
        body.setPadding(0, ui.dp(12), 0, ui.dp(12));
        column.addView(body);

        // The reason can be long (a bridge error plus a stack trace), so it scrolls; the actions
        // stay pinned under it, which is where a Material 3 dialog puts its buttons.
        LinearLayout root = ui.root();
        root.addView(ui.topAppBar(getString(R.string.app_name)), Ui.match());

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(ui.dp(12), 0, ui.dp(12), ui.dp(12));

        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(column, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        content.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        actions = ui.group();
        content.addView(actions, ui.groupParams());
        root.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        // The bar every screen carries. This one has nowhere of its own to go, so the two pills
        // above are its actions and the bar is the way into the rest of the app - which is the
        // point of it here: a launch that did not open should not leave the user with only Close.
        root.addView(Nav.bar(this, ui, Nav.Dest.APPS), Ui.match());
        setContentView(root);
    }
}
