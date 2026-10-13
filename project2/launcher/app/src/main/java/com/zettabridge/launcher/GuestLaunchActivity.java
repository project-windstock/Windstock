package com.zettabridge.launcher;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

import com.zettabridge.core.ZBridge;

/**
 * Entry point of a plugin (library tap or pinned shortcut), running in :guest. Loads the plugin
 * and starts its launcher activity through a stub; if the plugin is already on screen, finishing
 * simply brings its task to the front.
 *
 * <p>The manifest gives this activity a NoDisplay theme, and takes it literally: the framework
 * kills any NoDisplay activity that has not finished by the end of onResume, measured on the device
 * as "did not call finish() prior to onResume() completing". So this activity can never show
 * anything itself - not a progress view, not an error - and it must finish on every path. A failure
 * is therefore handed to {@link LaunchFailureActivity}, which has a real window in the launcher
 * process and stays on screen with the reason.
 */
public class GuestLaunchActivity extends Activity {
    static final String EXTRA_PACKAGE = "com.chucny.translator.bridge.PACKAGE";

    /**
     * How long after a repair attempt the same app is not repaired again. The retry runs in a fresh
     * :guest process - the runtime is process-lifetime - so a flag inside this activity cannot bound
     * it: without this, an app that is permanently missing would be re-imported and retried in a
     * loop of restarts. Recorded by LaunchFailureActivity, read here.
     */
    private static final String PREFS = "translator.launches";
    private static final long REPAIR_COOLDOWN_MS = 60_000;

    private String packageName;

    /**
     * The intent that opens a plugin. The data names the plugin, because Android matches an
     * existing task against the intent that created it and ignores extras: with no data, a launch
     * of one plugin matches the task of another and revives that one instead. {@code clearTask}
     * discards a task left by a different plugin; without it a task whose process is gone is
     * recreated from its own old intent, which is the previous game.
     */
    static Intent intent(Context context, String packageName, boolean clearTask) {
        Intent intent = new Intent(Intent.ACTION_MAIN)
                .setClass(context, GuestLaunchActivity.class)
                .setData(Uri.parse("translator://guest/" + packageName))
                .putExtra(EXTRA_PACKAGE, packageName)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (clearTask) intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK);
        return intent;
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        DebugLog.attach(this);
        // A process that closed one game can be handed another without being restarted, so the
        // lease is taken here as well as at process start: it has to be held for exactly as long as
        // a launch is in flight or a game is up, and released by an exit the launcher would
        // otherwise read as a crash. Idempotent, and never a reason for the launch to fail.
        GuestHeartbeat.begin(this);
        packageName = getIntent().getStringExtra(EXTRA_PACKAGE);
        GuestHeartbeat.phase(this, GuestHeartbeat.STARTING, packageName);
        DebugLog.note("guest launch of " + packageName + " (clearTask="
                + ((getIntent().getFlags() & Intent.FLAG_ACTIVITY_CLEAR_TASK) != 0) + ")");
        if (packageName == null) {
            fail("This shortcut names no app", "It carries no package name, so there is nothing to "
                    + "load. Remove it and open the app from the library instead.", false);
            return;
        }
        attempt();
    }

    /** Loads the plugin and opens its window; repairs what can be repaired once, then explains. */
    private void attempt() {
        try {
            launch(packageName);
            // The plugin's own window is up, or its task was brought to the front: this activity has
            // no window of its own to show, and staying alive would cover what was just opened.
            finish();
        } catch (Throwable t) {
            if (t.toString().contains("already runs plugin")) {
                // One plugin per :guest process (the guest runtime is process-lifetime). A game that
                // never exits keeps the process, so switching games means restarting it.
                startActivity(PluginSwitchActivity.intent(this, packageName));
                finish();
                return;
            }
            String bridgeError = lastBridgeError();
            if (repairable(bridgeError)) {
                DebugLog.note("launch of " + packageName + " failed (" + t + "); asking the "
                        + "launcher to import and retry instead of closing the window");
                fail("Preparing " + packageName, explain(t, bridgeError), true);
                return;
            }
            Throwable report = bridgeError != null ? new IllegalStateException(bridgeError, t) : t;
            DebugLog.failure("cannot launch " + packageName, report);
            Diagnostics.report(this, "cannot launch " + packageName, report, false);
            // The app is installed and was still expected to run: this kind of failure is the
            // intermittent one (a window or proxy load that did not take hold). It is worth one
            // automatic retry in a fresh :guest process before the user is told anything.
            fail("Cannot launch " + packageName, explain(t, bridgeError), false, true);
        }
    }

    private String lastBridgeError() {
        try {
            return ZBridge.lastLoadError();
        } catch (Throwable ignored) {
            // The bridge itself may be the thing that failed; the original throwable still stands.
            return null;
        }
    }

    /**
     * True when the failure is one the launcher can fix on its own: the app is simply not imported
     * yet. That is the usual reason a shortcut opens and closes at once - on a fresh install the
     * library's seeding of the bundled app can still be running when the first launch arrives.
     */
    private boolean repairable(String bridgeError) {
        if (bridgeError != null) return false;
        // A :guest process whose runtime never installed cannot run a plugin at all, and the only
        // cure is a fresh process - which the retry path provides.
        if (!GuestRuntime.get().isInstalled()) return cooledDown("the runtime is not installed");
        if (SeedPlugin.read(this) == null) return false;
        PluginRecord record = PluginStore.find(this, packageName);
        if (record != null && record.isLaunchable()) return false;
        return cooledDown("the app is not imported or not launchable");
    }

    /**
     * Whether a retry is still worth making. The retry restarts the :guest process, so the record
     * has to outlive this activity; without it an app that cannot be repaired would restart the
     * process in a loop.
     */
    private boolean cooledDown(String why) {
        long lastAttempt = getSharedPreferences(PREFS, MODE_PRIVATE).getLong(packageName, 0);
        long age = System.currentTimeMillis() - lastAttempt;
        if (lastAttempt != 0 && age < REPAIR_COOLDOWN_MS) {
            DebugLog.note("not retrying " + packageName + " again yet (" + why + "): the last attempt "
                    + "was " + age + " ms ago, so the failure is real and its screen is the answer");
            return false;
        }
        return true;
    }

    private void fail(String title, String detail, boolean repair) {
        fail(title, detail, repair, false);
    }

    private void fail(String title, String detail, boolean repair, boolean autoRetry) {
        startActivity(LaunchFailureActivity.intent(this, packageName, title, detail, repair, autoRetry));
        finish();
    }

    private void launch(String packageName) throws Exception {
        GuestRuntime runtime = GuestRuntime.get();
        if (!runtime.isInstalled()) throw new IllegalStateException("guest runtime is not installed");
        if (runtime.isRunning(packageName)) return;
        DebugLog.note("loading plugin " + packageName + " into " + android.os.Process.myPid());

        PluginRecord record = PluginStore.find(this, packageName);
        if (record == null) throw new IllegalStateException("not imported (remove this shortcut)");
        if (!record.isLaunchable()) throw new IllegalStateException(record.status());

        runtime.load(packageName);
        Intent target = new Intent(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER)
                .setClassName(packageName, record.launcherActivity);
        Intent routed = runtime.route(target);
        if (routed == target) throw new IllegalStateException(record.launcherActivity + " is not a plugin activity");
        startActivity(routed);
    }

    /** The reason, in the order a reader needs it: what happened, then what the bridge said. */
    private String explain(Throwable t, String bridgeError) {
        StringBuilder out = new StringBuilder();
        if (t.getMessage() != null) {
            out.append(t.getMessage());
        } else {
            out.append(t);
        }
        if (!GuestRuntime.get().isInstalled()) {
            out.append("\n\nThe runtime is not installed in this process, so no plugin can start "
                    + "here. The launcher records why in zb-errors.txt.");
        }
        PluginRecord record = PluginStore.find(this, packageName);
        out.append("\n\nIn the library: ").append(record == null ? "not imported" : record.status());
        if (record != null) out.append("\nActivity it asked for: ").append(record.launcherActivity);
        out.append("\nGuest process: pid ").append(android.os.Process.myPid());
        if (bridgeError != null) out.append("\n\nBridge: ").append(bridgeError);
        return out.toString();
    }
}
