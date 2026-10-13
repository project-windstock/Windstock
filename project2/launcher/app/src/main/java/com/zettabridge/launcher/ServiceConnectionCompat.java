package com.zettabridge.launcher;

import android.content.ComponentName;
import android.content.ServiceConnection;
import android.os.IBinder;

import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.List;

/**
 * Bridges a plugin's {@link ServiceConnection} to the interface Android 16 calls.
 *
 * <p>Android 16 added {@code onServiceConnected(ComponentName, IBinder, IBinderSession)} to
 * {@code ServiceConnection}, as a default method, and dispatches a connection through it. A
 * plugin's connection is not an ordinary implementation of that interface: a translated plugin
 * implements it in its own native code, and what it hands to {@code bindService} is a
 * {@code java.lang.reflect.Proxy} whose handler forwards every call to that native code (Unity's
 * {@code bitter.jnibridge.JNIBridge} does exactly this). The proxy answers the methods the plugin
 * was compiled against and nothing else, so the platform's call to the new three-argument overload
 * leaves the guest with a method it has no implementation for, and the guest dies throwing from
 * its own handler:
 *
 * <pre>
 * java.lang.NoSuchMethodError: public default void
 *     android.content.ServiceConnection.onServiceConnected(
 *         android.content.ComponentName, android.os.IBinder, android.app.IBinderSession)
 *     at bitter.jnibridge.JNIBridge.invoke(Native Method)
 *     at android.app.LoadedApk$ServiceDispatcher.doConnected(LoadedApk.java)
 * </pre>
 *
 * <p>That method is a <em>default</em>: its own body is the call to the two-argument overload the
 * plugin does implement. This class is what gets that body executed. {@link #wrap} substitutes an
 * adapter that implements the two-argument methods and inherits the newer overloads, so the
 * platform's three-argument call runs the interface's default body, which calls
 * {@code onServiceConnected(name, service)}, which reaches the plugin. The plugin's own connection
 * is untouched and is still the object that answers; it is simply never asked for a method it was
 * not built with.
 *
 * <p>{@code onBindingDied} and {@code onNullBinding} are left to the interface's defaults for the
 * same reason - they postdate the plugin, and the no-op is what its own build expected. A plugin
 * that does implement them is not the case this class exists for; forwarding them would be the
 * crash above again, only rarer.
 *
 * <p>Because the framework identifies a connection by the object it was given, {@link #unwrap}
 * finds the adapter again when the plugin calls {@code unbindService} with the object it bound
 * with. The adapters are held weakly: the framework is what keeps a bound one alive, so one the
 * framework has dropped is one this class no longer needs to remember.
 */
final class ServiceConnectionCompat {

    private static final List<WeakReference<Adapter>> LIVE = new ArrayList<>();

    private ServiceConnectionCompat() {}

    /** The connection to hand to the framework in place of the plugin's own. */
    static ServiceConnection wrap(ServiceConnection connection) {
        if (connection == null || connection instanceof Adapter) return connection;
        Adapter adapter = new Adapter(connection);
        synchronized (LIVE) {
            purge();
            LIVE.add(new WeakReference<>(adapter));
        }
        DebugLog.note("bindService: " + connection.getClass().getName() + " is a plugin connection, "
                + "handing the framework an adapter so a post-2016 ServiceConnection overload reaches "
                + "the plugin as the two-argument call it implements");
        return adapter;
    }

    /** The object the framework knows this plugin connection by, for unbindService. */
    static ServiceConnection unwrap(ServiceConnection connection) {
        if (connection == null || connection instanceof Adapter) return connection;
        synchronized (LIVE) {
            purge();
            for (WeakReference<Adapter> reference : LIVE) {
                Adapter adapter = reference.get();
                if (adapter != null && adapter.delegate == connection) return adapter;
            }
        }
        // Bound before this class saw it (a context that is not the plugin's), so the framework's
        // own object is the plugin's connection itself.
        return connection;
    }

    private static void purge() {
        for (int i = LIVE.size() - 1; i >= 0; i--) {
            if (LIVE.get(i).get() == null) LIVE.remove(i);
        }
    }

    /** The two methods the plugin implements; every later addition stays the interface's default. */
    private static final class Adapter implements ServiceConnection {
        final ServiceConnection delegate;

        Adapter(ServiceConnection delegate) {
            this.delegate = delegate;
        }

        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            delegate.onServiceConnected(name, service);
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            delegate.onServiceDisconnected(name);
        }
    }
}
