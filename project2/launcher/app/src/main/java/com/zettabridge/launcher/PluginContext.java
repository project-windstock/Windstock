package com.zettabridge.launcher;

import android.content.Context;
import android.content.ContextParams;
import android.content.ContextWrapper;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.res.AssetManager;
import android.content.res.Configuration;
import android.content.res.Resources;
import android.database.DatabaseErrorHandler;
import android.database.sqlite.SQLiteDatabase;
import android.os.Bundle;
import android.view.Display;
import android.view.LayoutInflater;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;

/**
 * Base context of plugin activities and the plugin Application. Code, resources and storage
 * belong to the plugin; identity (package name, uid, system services) stays the launcher's,
 * because system services check the caller's package against its uid.
 *
 * Contexts derived from this one (configuration, display, window, attribution, package
 * contexts) are wrapped again. Otherwise the framework hands back a ContextImpl with the
 * launcher's resources: Compose screens that localize through createConfigurationContext then
 * fail with Resources$NotFoundException, and Flutter, which takes its AssetManager from
 * createPackageContext(getPackageName()), cannot load any asset.
 */
final class PluginContext extends ContextWrapper {
    private final LoadedPlugin plugin;
    private final Resources resources;
    private Resources.Theme theme;
    private LayoutInflater inflater;

    PluginContext(Context base, LoadedPlugin plugin) {
        this(base, plugin, plugin.resources);
    }

    private PluginContext(Context base, LoadedPlugin plugin, Resources resources) {
        super(base);
        this.plugin = plugin;
        this.resources = resources;
    }

    /** Wraps a context derived by the framework, with plugin resources in its configuration. */
    private PluginContext derived(Context frameworkContext) {
        Resources host = frameworkContext.getResources();
        @SuppressWarnings("deprecation")
        Resources pluginResources = new Resources(plugin.resources.getAssets(), host.getDisplayMetrics(),
                host.getConfiguration());
        return new PluginContext(frameworkContext, plugin, pluginResources);
    }

    @Override
    public Resources getResources() {
        return resources;
    }

    @Override
    public AssetManager getAssets() {
        return resources.getAssets();
    }

    @Override
    public ClassLoader getClassLoader() {
        return plugin.classLoader;
    }

    @Override
    public ApplicationInfo getApplicationInfo() {
        return plugin.appInfo;
    }

    @Override
    public String getPackageCodePath() {
        return plugin.appInfo.sourceDir;
    }

    @Override
    public String getPackageResourcePath() {
        return plugin.appInfo.sourceDir;
    }

    @Override
    public Context getApplicationContext() {
        return plugin.application != null ? plugin.application : super.getApplicationContext();
    }

    // Derived contexts.

    @Override
    public Context createConfigurationContext(Configuration overrideConfiguration) {
        return derived(super.createConfigurationContext(overrideConfiguration));
    }

    @Override
    public Context createDisplayContext(Display display) {
        return derived(super.createDisplayContext(display));
    }

    @Override
    public Context createWindowContext(int type, Bundle options) {
        return derived(super.createWindowContext(type, options));
    }

    @Override
    public Context createWindowContext(Display display, int type, Bundle options) {
        return derived(super.createWindowContext(display, type, options));
    }

    @Override
    public Context createAttributionContext(String attributionTag) {
        return derived(super.createAttributionContext(attributionTag));
    }

    @Override
    public Context createContext(ContextParams contextParams) {
        return derived(super.createContext(contextParams));
    }

    @Override
    public Context createDeviceProtectedStorageContext() {
        return derived(super.createDeviceProtectedStorageContext());
    }

    /** "Our own package" is the plugin, whether it is named by the plugin or the launcher package. */
    @Override
    public Context createPackageContext(String packageName, int flags) throws PackageManager.NameNotFoundException {
        if (packageName.equals(plugin.packageName) || packageName.equals(getBaseContext().getPackageName())) {
            return derived(super.createPackageContext(getBaseContext().getPackageName(), flags));
        }
        return super.createPackageContext(packageName, flags);
    }

    // Bindings. Every plugin component that binds a service does it through a context derived
    // from this one (an activity's base context, or the plugin Application's), which is what makes
    // this an interception point at all - and it has to be one, because Android 16 dispatches a
    // connection through a ServiceConnection overload no plugin compiled before it implements.
    // ServiceConnectionCompat explains the failure and why substituting an adapter fixes it.

    @Override
    public boolean bindService(Intent service, ServiceConnection connection, int flags) {
        return super.bindService(service, ServiceConnectionCompat.wrap(connection), flags);
    }

    /** The framework knows a connection by the object it was given, which here is the adapter. */
    @Override
    public void unbindService(ServiceConnection connection) {
        super.unbindService(ServiceConnectionCompat.unwrap(connection));
    }

    // Theme and inflater are only used when this context is not wrapped by an Activity
    // (the plugin Application); activities have their own ContextThemeWrapper state.
    @Override
    public Resources.Theme getTheme() {
        if (theme == null) {
            theme = resources.newTheme();
            int id = plugin.appInfo.theme != 0 ? plugin.appInfo.theme : android.R.style.Theme_DeviceDefault_Light_DarkActionBar;
            theme.applyStyle(id, true);
        }
        return theme;
    }

    @Override
    public void setTheme(int resid) {
        getTheme().applyStyle(resid, true);
    }

    @Override
    public Object getSystemService(String name) {
        if (LAYOUT_INFLATER_SERVICE.equals(name)) {
            if (inflater == null) inflater = LayoutInflater.from(getBaseContext()).cloneInContext(this);
            return inflater;
        }
        return super.getSystemService(name);
    }

    // Storage: each plugin gets filesDir/plugins/<package>/data and its own external subdirectory.

    private File dir(String name) {
        File f = new File(plugin.record.dataDir(), name);
        f.mkdirs();
        return f;
    }

    private static File external(File base, String sub) {
        if (base == null) return null;
        File f = new File(base, sub);
        f.mkdirs();
        return f;
    }

    @Override
    public File getDataDir() {
        return plugin.record.dataDir();
    }

    @Override
    public File getFilesDir() {
        return dir("files");
    }

    @Override
    public File getCacheDir() {
        return dir("cache");
    }

    @Override
    public File getNoBackupFilesDir() {
        return dir("no_backup");
    }

    @Override
    public File getDir(String name, int mode) {
        return dir("app_" + name);
    }

    @Override
    public File getFileStreamPath(String name) {
        return new File(getFilesDir(), name);
    }

    @Override
    public FileInputStream openFileInput(String name) throws FileNotFoundException {
        return new FileInputStream(getFileStreamPath(name));
    }

    @Override
    public FileOutputStream openFileOutput(String name, int mode) throws FileNotFoundException {
        return new FileOutputStream(getFileStreamPath(name), (mode & MODE_APPEND) != 0);
    }

    @Override
    public boolean deleteFile(String name) {
        return getFileStreamPath(name).delete();
    }

    @Override
    public String[] fileList() {
        String[] names = getFilesDir().list();
        return names != null ? names : new String[0];
    }

    @Override
    public File getDatabasePath(String name) {
        if (name.startsWith(File.separator)) return new File(name);
        return new File(dir("databases"), name);
    }

    @Override
    public SQLiteDatabase openOrCreateDatabase(String name, int mode, SQLiteDatabase.CursorFactory factory) {
        return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name), factory);
    }

    @Override
    public SQLiteDatabase openOrCreateDatabase(String name, int mode, SQLiteDatabase.CursorFactory factory,
                                              DatabaseErrorHandler errorHandler) {
        return SQLiteDatabase.openOrCreateDatabase(getDatabasePath(name).getPath(), factory, errorHandler);
    }

    @Override
    public boolean deleteDatabase(String name) {
        return SQLiteDatabase.deleteDatabase(getDatabasePath(name));
    }

    @Override
    public String[] databaseList() {
        String[] names = dir("databases").list();
        return names != null ? names : new String[0];
    }

    @Override
    public android.content.SharedPreferences getSharedPreferences(String name, int mode) {
        return super.getSharedPreferences(plugin.packageName + "__" + name, mode);
    }

    @Override
    public boolean deleteSharedPreferences(String name) {
        return super.deleteSharedPreferences(plugin.packageName + "__" + name);
    }

    @Override
    public File getExternalFilesDir(String type) {
        String sub = "plugins/" + plugin.packageName + (type != null ? "/" + type : "");
        return external(super.getExternalFilesDir(null), sub);
    }

    @Override
    public File[] getExternalFilesDirs(String type) {
        return new File[] {getExternalFilesDir(type)};
    }

    @Override
    public File getExternalCacheDir() {
        return external(super.getExternalCacheDir(), "plugins/" + plugin.packageName);
    }

    @Override
    public File[] getExternalCacheDirs() {
        return new File[] {getExternalCacheDir()};
    }

    @Override
    public File getObbDir() {
        return external(super.getObbDir(), plugin.packageName);
    }

    @Override
    public File[] getObbDirs() {
        return new File[] {getObbDir()};
    }
}
