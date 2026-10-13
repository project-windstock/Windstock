package com.zettabridge.launcher;

import android.app.Application;
import android.content.ContentProvider;
import android.content.Context;
import android.content.pm.ActivityInfo;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ProviderInfo;
import android.content.pm.ServiceInfo;
import android.content.res.Resources;
import android.graphics.Bitmap;
import android.util.Log;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;

import com.zettabridge.core.ZBridge;

/** A plugin loaded into the :guest process: its class loader, resources and Application. */
final class LoadedPlugin {
    private static final String TAG = "Translator";

    final PluginRecord record;
    final String packageName;
    final String label;
    final ApplicationInfo appInfo;
    final Resources resources;
    final ClassLoader classLoader;
    /** Plugin activities by fully qualified class name. */
    final Map<String, ActivityInfo> activities = new HashMap<>();
    /** Services, receivers and providers by class name, for package manager virtualization. */
    final Map<String, ServiceInfo> services = new HashMap<>();
    final Map<String, ActivityInfo> receivers = new HashMap<>();
    final Map<String, ProviderInfo> providerInfos = new HashMap<>();
    /** The parsed archive (version, meta-data). */
    PackageInfo info;
    /** Content providers started in-process (kept alive for the process lifetime). */
    final List<ContentProvider> providers = new ArrayList<>();
    Application application;

    private LoadedPlugin(PluginRecord record, ApplicationInfo appInfo, Resources resources, ClassLoader classLoader) {
        this.record = record;
        this.packageName = record.packageName;
        this.label = record.label;
        this.appInfo = appInfo;
        this.resources = resources;
        this.classLoader = classLoader;
    }

    Bitmap icon() {
        return record.icon();
    }

    /** onParsed runs before the plugin Application is created (the runtime marks it current). */
    static LoadedPlugin load(Application host, PluginRecord record, Consumer<LoadedPlugin> onParsed) throws Exception {
        PackageManager pm = host.getPackageManager();
        PackageInfo info = pm.getPackageArchiveInfo(record.apk().getPath(), PluginStore.ARCHIVE_FLAGS);
        if (info == null) throw new IllegalStateException("cannot parse " + record.apk());
        ApplicationInfo ai = PluginStore.applicationInfo(info, record);
        ai.uid = android.os.Process.myUid();
        // Public API; works for an APK that is not installed once sourceDir points at it.
        Resources res = pm.getResourcesForApplication(ai);

        File codeCache = new File(host.getCodeCacheDir(), "plugins/" + record.packageName);
        if (!codeCache.isDirectory() && !codeCache.mkdirs()) {
            throw new IllegalStateException("cannot create " + codeCache);
        }
        if (!record.dataDir().isDirectory() && !record.dataDir().mkdirs()) {
            throw new IllegalStateException("cannot create " + record.dataDir());
        }
        if (record.isTranslated()) RuntimeBundle.install(host);
        // Plugin dex is child-first and isolated from launcher implementation classes. Only the
        // stable com.zettabridge.core bridge is explicitly delegated to the launcher loader.
        ClassLoader cl = new PluginClassLoader(record, codeCache, RuntimeBundle.proxyLibrary(host),
                android.content.Context.class.getClassLoader(), ZBridge.class.getClassLoader());

        LoadedPlugin p = new LoadedPlugin(record, ai, res, cl);
        p.info = info;
        if (record.isTranslated()) {
            // Must precede activatePlugin: the guest runtime is process-lifetime and reads
            // ZB_PRECISE_FAULTS only once, at the first Process construction it triggers.
            ZBridge.setPreciseFaults(record.preciseFaults);
            // Same rule: the GL instrumentation is read once, when the guest JNI runtime is built.
            ZBridge.setGlDiagnostics(record.diagnostics);
            // Must precede every plugin class initialization, provider and Application callback.
            ZBridge.activatePlugin(record.dir.getCanonicalPath(), record.targetSdk, cl);
        }
        if (info.activities != null) {
            for (ActivityInfo a : info.activities) {
                a.applicationInfo = ai;
                p.activities.put(a.name, a);
            }
        }
        if (info.services != null) {
            for (ServiceInfo s : info.services) {
                s.applicationInfo = ai;
                p.services.put(s.name, s);
            }
        }
        if (info.receivers != null) {
            for (ActivityInfo r : info.receivers) {
                r.applicationInfo = ai;
                p.receivers.put(r.name, r);
            }
        }
        if (info.providers != null) {
            for (ProviderInfo pi : info.providers) {
                pi.applicationInfo = ai;
                p.providerInfos.put(pi.name, pi);
            }
        }

        onParsed.accept(p);

        // Same order as ActivityThread.handleBindApplication: attach, content providers, onCreate.
        // p.application is set before attach, so getApplicationContext() already returns the
        // plugin Application inside attachBaseContext (apps commonly cache it there).
        String appClass = ai.className != null ? ai.className : Application.class.getName();
        PluginContext appContext = new PluginContext(host.getBaseContext(), p);
        p.application = (Application) cl.loadClass(appClass).getDeclaredConstructor().newInstance();
        Reflect.method(Application.class, "attach", Context.class).invoke(p.application, appContext);
        Log.i(TAG, "plugin " + p.packageName + ": application " + appClass + " attached");
        installProviders(p, info, ai);
        p.application.onCreate();
        return p;
    }

    /**
     * Instantiates the plugin's content providers in-process so auto-init providers (AndroidX
     * Startup, Firebase, WorkManager) run before Application.onCreate. They are not registered
     * with the system: resolving their authorities through ContentResolver does not work yet.
     */
    private static void installProviders(LoadedPlugin p, PackageInfo info, ApplicationInfo ai) {
        if (info.providers == null) return;
        for (ProviderInfo pi : info.providers) {
            try {
                pi.applicationInfo = ai;
                ContentProvider provider = (ContentProvider) p.classLoader.loadClass(pi.name)
                        .getDeclaredConstructor().newInstance();
                provider.attachInfo(p.application, pi);
                p.providers.add(provider);
                Log.i(TAG, "plugin " + p.packageName + ": provider " + pi.name + " started");
            } catch (Throwable t) {
                Diagnostics.report(p.application, "plugin " + p.packageName + ": provider " + pi.name + " failed", t,
                        false);
            }
        }
    }
}
