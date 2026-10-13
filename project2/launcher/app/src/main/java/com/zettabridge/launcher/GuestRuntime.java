package com.zettabridge.launcher;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.Application;
import android.app.Instrumentation;
import android.content.ContextWrapper;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.os.Bundle;
import android.util.Log;
import android.view.ContextThemeWrapper;
import android.view.Window;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;

/** Plugin runtime of the :guest process: loaded plugins, intent routing and activity fixups. */
final class GuestRuntime {
    private static final String TAG = "Translator";
    // Extra keys travel only between this app's own components. They name this app rather than the
    // module it was built from, so an unpacked dex does not carry the upstream project's name.
    static final String EXTRA_PLUGIN = "com.chucny.translator.bridge.PLUGIN";
    static final String EXTRA_ACTIVITY = "com.chucny.translator.bridge.ACTIVITY";
    static final String EXTRA_INTENT = "com.chucny.translator.bridge.INTENT";

    private static final GuestRuntime INSTANCE = new GuestRuntime();

    private final Map<String, LoadedPlugin> plugins = new HashMap<>();
    private final Map<String, Integer> liveActivities = new HashMap<>();
    /** Plugin windows that are currently resumed - the game is on screen. */
    private int resumed;
    private Application host;
    private boolean installed;
    private volatile LoadedPlugin current;

    static GuestRuntime get() {
        return INSTANCE;
    }

    boolean isInstalled() {
        return installed;
    }

    void install(Application app) {
        host = app;
        try {
            RuntimeBundle.install(app);
        } catch (IOException e) {
            Diagnostics.report(app, "cannot install the Translator runtime bundle", e, false);
            return;
        }
        HiddenApi.exemptAll();
        try {
            Class<?> activityThread = Class.forName("android.app.ActivityThread");
            Object thread = Reflect.method(activityThread, "currentActivityThread").invoke(null);
            Field field = Reflect.field(activityThread, "mInstrumentation");
            Instrumentation original = (Instrumentation) field.get(thread);
            if (!(original instanceof GuestInstrumentation)) {
                GuestInstrumentation guest = new GuestInstrumentation(this, original);
                try {
                    // Lets inherited, non-overridden methods see the ActivityThread like the original.
                    Method basicInit = Reflect.method(Instrumentation.class, "basicInit", activityThread);
                    basicInit.invoke(guest, thread);
                } catch (ReflectiveOperationException e) {
                    Log.w(TAG, "Instrumentation.basicInit unavailable: " + e);
                }
                field.set(thread, guest);
            }
            installed = true;
            Log.i(TAG, "guest Instrumentation installed");
            DebugLog.note("guest instrumentation installed in " + ZbApplication.processName()
                    + "; plugin windows are now routed through the stub activities");
            PackageManagerHook.install(app, this);
        } catch (ReflectiveOperationException | RuntimeException e) {
            Log.e(TAG, "cannot install the guest Instrumentation; plugins will not launch", e);
        }
    }

    synchronized LoadedPlugin load(String packageName) throws Exception {
        LoadedPlugin p = plugins.get(packageName);
        if (p != null) {
            current = p;
            return p;
        }
        PluginRecord record = PluginStore.find(host, packageName);
        if (record == null) throw new IllegalStateException(packageName + " is not imported");
        // current is set inside LoadedPlugin.load before the plugin Application runs, so its
        // package manager queries already see the plugin's meta-data.
        p = LoadedPlugin.load(host, record, loaded -> current = loaded);
        plugins.put(packageName, p);
        ActivePlugin.record(host, packageName);
        GuestHeartbeat.phase(host, GuestHeartbeat.LOADED, packageName);
        DebugLog.note("plugin " + packageName + " loaded (" + record.label + ", " + record.status()
                + "); the :guest process now serves it alone");
        return p;
    }

    /** The plugin launched most recently; all plugins share the :guest process. */
    LoadedPlugin current() {
        return current;
    }

    /** A plugin component by class name, for the IPackageManager method that asks for it. */
    synchronized android.content.pm.ComponentInfo findComponent(String method, String className) {
        LoadedPlugin cur = current;
        if (cur != null) {
            android.content.pm.ComponentInfo info = componentOf(cur, method, className);
            if (info != null) return info;
        }
        for (LoadedPlugin p : plugins.values()) {
            android.content.pm.ComponentInfo info = componentOf(p, method, className);
            if (info != null) return info;
        }
        return null;
    }

    private static android.content.pm.ComponentInfo componentOf(LoadedPlugin p, String method, String className) {
        switch (method) {
            case "getServiceInfo":
                return p.services.get(className);
            case "getActivityInfo":
                return p.activities.get(className);
            case "getReceiverInfo":
                return p.receivers.get(className);
            case "getProviderInfo":
                return p.providerInfos.get(className);
            default:
                return null;
        }
    }

    synchronized boolean isRunning(String packageName) {
        Integer n = liveActivities.get(packageName);
        return n != null && n > 0;
    }

    /** Rewrites an intent that names a plugin activity to the matching stub; others pass through. */
    Intent route(Intent intent) {
        if (intent == null || intent.getComponent() == null) return intent;
        if (host.getPackageName().equals(intent.getComponent().getPackageName())
                && intent.getComponent().getClassName().startsWith(Stubs.class.getName())) {
            return intent;  // already a stub intent
        }
        String className = intent.getComponent().getClassName();
        synchronized (this) {
            for (LoadedPlugin p : plugins.values()) {
                ActivityInfo ai = p.activities.get(className);
                if (ai != null) return stubIntent(p, ai, intent);
            }
        }
        return intent;
    }

    private Intent stubIntent(LoadedPlugin p, ActivityInfo ai, Intent original) {
        Intent stub = new Intent(host, stubFor(ai));
        stub.setFlags(original.getFlags());
        stub.putExtra(EXTRA_PLUGIN, p.packageName);
        stub.putExtra(EXTRA_ACTIVITY, ai.name);
        stub.putExtra(EXTRA_INTENT, new Intent(original));
        return stub;
    }

    private static Class<? extends Activity> stubFor(ActivityInfo ai) {
        switch (ai.launchMode) {
            case ActivityInfo.LAUNCH_SINGLE_TOP:
                return Stubs.SingleTop.class;
            case ActivityInfo.LAUNCH_SINGLE_TASK:
                return Stubs.SingleTask.class;
            case ActivityInfo.LAUNCH_SINGLE_INSTANCE:
                return Stubs.SingleInstance.class;
            default:
                break;
        }
        switch (ai.screenOrientation) {
            case ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE:
            case ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE:
            case ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE:
            case ActivityInfo.SCREEN_ORIENTATION_USER_LANDSCAPE:
                return Stubs.Landscape.class;
            case ActivityInfo.SCREEN_ORIENTATION_PORTRAIT:
            case ActivityInfo.SCREEN_ORIENTATION_REVERSE_PORTRAIT:
            case ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT:
            case ActivityInfo.SCREEN_ORIENTATION_USER_PORTRAIT:
                return Stubs.Portrait.class;
            default:
                return Stubs.Standard.class;
        }
    }

    /** Called from Instrumentation.newActivity: the plugin activity for a stub intent, or null. */
    Activity instantiatePluginActivity(Intent intent) {
        if (intent == null) return null;
        String packageName;
        String activity;
        try {
            intent.setExtrasClassLoader(GuestRuntime.class.getClassLoader());
            packageName = intent.getStringExtra(EXTRA_PLUGIN);
            activity = intent.getStringExtra(EXTRA_ACTIVITY);
        } catch (RuntimeException e) {
            return null;
        }
        if (packageName == null || activity == null) {
            instantiationError = "the stub intent named "
                    + (packageName == null ? "no app" : "app " + packageName)
                    + " and " + (activity == null ? "no screen" : "screen " + activity);
            return null;
        }
        try {
            LoadedPlugin p = load(packageName);
            return (Activity) p.classLoader.loadClass(activity).getDeclaredConstructor().newInstance();
        } catch (Throwable t) {
            // Kept for the stub activity, which is the only thing still alive to report this: the
            // framework falls back to the placeholder class and would otherwise show a blank window
            // that closes immediately.
            instantiationError = packageName + "/" + activity + " could not be created: "
                    + Log.getStackTraceString(t);
            Diagnostics.report(host, "cannot instantiate " + packageName + "/" + activity, t, true);
            return null;
        }
    }

    /** Why the last stub intent produced no plugin activity, or null if there was no failure. */
    static String lastInstantiationError() {
        return instantiationError;
    }

    private static volatile String instantiationError;

    private synchronized LoadedPlugin pluginOf(Activity activity) {
        ClassLoader cl = activity.getClass().getClassLoader();
        for (LoadedPlugin p : plugins.values()) {
            if (p.classLoader == cl) return p;
        }
        return null;
    }

    /**
     * Runs after Activity.attach and before onCreate. attach() gave the activity the stub's
     * context, and creating its window already cached launcher resources and theme, so those
     * caches are cleared before the plugin theme is applied.
     */
    void prepareActivity(Activity activity, Bundle icicle) {
        LoadedPlugin p = pluginOf(activity);
        if (p == null) return;
        synchronized (this) {
            liveActivities.merge(p.packageName, 1, Integer::sum);
        }
        ActivityInfo ai = p.activities.get(activity.getClass().getName());

        Reflect.trySet(ContextWrapper.class, activity, "mBase", new PluginContext(activity.getBaseContext(), p));
        Reflect.trySet(ContextThemeWrapper.class, activity, "mResources", null);
        Reflect.trySet(ContextThemeWrapper.class, activity, "mTheme", null);
        Reflect.trySet(ContextThemeWrapper.class, activity, "mInflater", null);
        Reflect.trySet(Window.class, activity.getWindow(), "mWindowStyle", null);
        if (p.application != null) Reflect.trySet(Activity.class, activity, "mApplication", p.application);

        int theme = ai != null ? ai.getThemeResource() : p.appInfo.theme;
        activity.setTheme(theme != 0 ? theme : GuestWindowStyle.defaultTheme(p.appInfo.targetSdkVersion));

        GuestWindowStyle.apply(activity, p.record != null && p.record.isTranslated());
        if (p.record != null && p.record.hideAds) AdHider.attach(activity);

        Intent original = activity.getIntent().getParcelableExtra(EXTRA_INTENT);
        if (original != null) {
            original.setExtrasClassLoader(p.classLoader);
            activity.setIntent(original);
        }
        if (icicle != null) icicle.setClassLoader(p.classLoader);

        CharSequence title = p.label;
        if (ai != null) {
            if (ai.screenOrientation != ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED) {
                activity.setRequestedOrientation(ai.screenOrientation);
            }
            if (ai.softInputMode != 0) activity.getWindow().setSoftInputMode(ai.softInputMode);
            if (ai.labelRes != 0) {
                try {
                    title = p.resources.getText(ai.labelRes);
                } catch (RuntimeException ignored) {
                    // keep the application label
                }
            } else if (ai.nonLocalizedLabel != null) {
                title = ai.nonLocalizedLabel;
            }
        }
        activity.setTitle(title);
        activity.setTaskDescription(new ActivityManager.TaskDescription(p.label, p.icon()));
    }

    void onActivityDestroyed(Activity activity) {
        LoadedPlugin p = pluginOf(activity);
        if (p == null) return;
        boolean last;
        synchronized (this) {
            liveActivities.merge(p.packageName, -1, Integer::sum);
            last = !isRunning(p.packageName);
        }
        try {
            if (last && activity.isFinishing() && !activity.isChangingConfigurations() && resumedCount() == 0) {
                // The plugin finished its own last window: the process is idle, not crashed, and the
                // launcher must not drag the user back into a game they closed.
                GuestHeartbeat.cleanExit(host, p.packageName + " finished its last window itself");
            } else if (last) {
                // Destroyed without finishing (the system is rebuilding it), or destroyed while
                // another plugin window is up: the process is still doing something, it just has no
                // window of this plugin on screen - which is the state a death should not be
                // reopened from.
                GuestHeartbeat.phase(host, GuestHeartbeat.BACKGROUND, p.packageName);
            }
        } catch (Throwable ignored) {
            // The heartbeat is a net under the game, never a condition of it running.
        }
    }

    /**
     * The window phases are told to the launcher process so that a death can be told apart from a
     * close. They are driven from {@link GuestInstrumentation} rather than from
     * {@code Application.registerActivityLifecycleCallbacks}, which is the obvious public API and
     * does not work here: {@link #prepareActivity} hands each plugin activity the *plugin's*
     * Application, so {@code getApplication().dispatchActivityResumed()} reaches an object nothing
     * registered on. Measured on device: with the callbacks the phase stayed 'loaded' for the whole
     * life of a running game, which the recovery in the launcher process reads as a window that was
     * on screen - and a game killed while the user was on the home screen got reopened. The
     * instrumentation is called by the framework for every activity in this process, and
     * {@link #pluginOf} already answers "is this a plugin window" there.
     */

    /** A plugin window is on screen: this is the state a crash is worth reopening from. */
    void onWindowResumed(Activity activity) {
        try {
            LoadedPlugin p = pluginOf(activity);
            if (p == null) return;
            synchronized (this) {
                resumed++;
            }
            GuestHeartbeat.phase(host, GuestHeartbeat.VISIBLE, p.packageName);
        } catch (Throwable ignored) {
            // see onActivityDestroyed
        }
    }

    /**
     * A plugin window lost focus - another plugin window took it, the game went to the background,
     * or the task is being taken away. Only the last of those means the user is not watching, but
     * none of them is a state a dead process should be reopened from, so they all record the same
     * phase; a resume puts it back to 'visible'.
     */
    void onWindowPaused(Activity activity) {
        try {
            LoadedPlugin p = pluginOf(activity);
            if (p == null) return;
            int still;
            synchronized (this) {
                still = resumed > 0 ? --resumed : 0;
            }
            GuestHeartbeat.phase(host, still > 0 ? GuestHeartbeat.VISIBLE : GuestHeartbeat.BACKGROUND,
                    p.packageName);
        } catch (Throwable ignored) {
            // see onActivityDestroyed
        }
    }

    private synchronized int resumedCount() {
        return resumed;
    }
}
