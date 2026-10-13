package com.zettabridge.launcher;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.IBinder;
import android.os.PersistableBundle;
import android.util.Log;

import java.lang.reflect.Method;

/**
 * Replaces ActivityThread.mInstrumentation in the :guest process.
 * - execStartActivity*: intents naming a plugin activity are rewritten to a manifest stub.
 * - newActivity: a stub intent instantiates the plugin activity from the plugin class loader,
 *   so the framework attaches it with the stub's real token and window.
 * - callActivityOnCreate: swaps in the plugin context, resources, theme and original intent
 *   before the plugin's onCreate runs.
 * Everything else delegates to the original instance, which the framework initialized.
 */
final class GuestInstrumentation extends Instrumentation {
    private static final String TAG = "Translator";

    private final GuestRuntime runtime;
    private final Instrumentation base;
    private Method execStartActivity;
    private Method execStartActivityFromString;
    private Method execStartActivities;

    GuestInstrumentation(GuestRuntime runtime, Instrumentation base) {
        this.runtime = runtime;
        this.base = base;
    }

    @Override
    public Activity newActivity(ClassLoader cl, String className, Intent intent)
            throws InstantiationException, IllegalAccessException, ClassNotFoundException {
        Activity plugin = runtime.instantiatePluginActivity(intent);
        DebugLog.note("newActivity " + className + " -> " + (plugin != null
                ? "plugin activity " + plugin.getClass().getName() + " intent=" + intent
                : "the stub itself (no plugin intent on it)"));
        return plugin != null ? plugin : base.newActivity(cl, className, intent);
    }

    @Override
    public void callActivityOnCreate(Activity activity, Bundle icicle) {
        runtime.prepareActivity(activity, icicle);
        DebugLog.attach(activity);
        base.callActivityOnCreate(activity, icicle);
        GuestWindowStyle.afterCreate(activity);
    }

    @Override
    public void callActivityOnCreate(Activity activity, Bundle icicle, PersistableBundle persistentState) {
        runtime.prepareActivity(activity, icicle);
        DebugLog.attach(activity);
        base.callActivityOnCreate(activity, icicle, persistentState);
        GuestWindowStyle.afterCreate(activity);
    }

    @Override
    public void callActivityOnResume(Activity activity) {
        base.callActivityOnResume(activity);
        // After the plugin's own onResume: a window that is resumed is the state a crash is worth
        // reopening from. This is the only hook that fires for plugin activities (see GuestRuntime).
        runtime.onWindowResumed(activity);
    }

    @Override
    public void callActivityOnPause(Activity activity) {
        // Before the plugin's own onPause, so the phase is already the non-visible one if it throws.
        runtime.onWindowPaused(activity);
        base.callActivityOnPause(activity);
    }

    @Override
    public void callActivityOnDestroy(Activity activity) {
        // Before the framework unwinds anything: this is where the debug log explains why the
        // window is going away (finish, configuration change, or the system tearing it down).
        DebugLog.windowGone(activity, "GuestInstrumentation.callActivityOnDestroy");
        base.callActivityOnDestroy(activity);
        runtime.onActivityDestroyed(activity);
    }

    // Hidden framework entry points of Activity.startActivity and Context.startActivity. They are
    // not in the SDK stubs, so these only override at runtime; the originals are called on base.

    public ActivityResult execStartActivity(Context who, IBinder contextThread, IBinder token, Activity target,
                                            Intent intent, int requestCode, Bundle options) {
        try {
            if (execStartActivity == null) {
                execStartActivity = Reflect.method(Instrumentation.class, "execStartActivity", Context.class,
                        IBinder.class, IBinder.class, Activity.class, Intent.class, int.class, Bundle.class);
            }
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException("Instrumentation.execStartActivity not found", e);
        }
        Intent routed = runtime.route(intent);
        DebugLog.note("startActivity " + intent.getComponent() + " routed to " + routed.getComponent()
                + (routed == intent ? " (unchanged)" : ""));
        return (ActivityResult) Reflect.invoke(execStartActivity, base, who, contextThread, token, target, routed,
                requestCode, options);
    }

    public ActivityResult execStartActivity(Context who, IBinder contextThread, IBinder token, String target,
                                            Intent intent, int requestCode, Bundle options) {
        try {
            if (execStartActivityFromString == null) {
                execStartActivityFromString = Reflect.method(Instrumentation.class, "execStartActivity", Context.class,
                        IBinder.class, IBinder.class, String.class, Intent.class, int.class, Bundle.class);
            }
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException("Instrumentation.execStartActivity(String) not found", e);
        }
        Intent routed = runtime.route(intent);
        DebugLog.note("startActivity(String target) " + intent.getComponent() + " routed to "
                + routed.getComponent());
        return (ActivityResult) Reflect.invoke(execStartActivityFromString, base, who, contextThread, token, target,
                routed, requestCode, options);
    }

    public void execStartActivities(Context who, IBinder contextThread, IBinder token, Activity target,
                                    Intent[] intents, Bundle options) {
        try {
            if (execStartActivities == null) {
                execStartActivities = Reflect.method(Instrumentation.class, "execStartActivities", Context.class,
                        IBinder.class, IBinder.class, Activity.class, Intent[].class, Bundle.class);
            }
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException("Instrumentation.execStartActivities not found", e);
        }
        Intent[] routed = new Intent[intents.length];
        for (int i = 0; i < intents.length; i++) routed[i] = runtime.route(intents[i]);
        Log.d(TAG, "execStartActivities: " + intents.length + " intents");
        Reflect.invoke(execStartActivities, base, who, contextThread, token, target, routed, options);
    }
}
