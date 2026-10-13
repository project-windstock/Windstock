package com.zettabridge.launcher;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Runtime permissions a guest needs, requested on the host.
 *
 * A plugin runs inside the launcher's process and therefore under the launcher's
 * uid: Android attributes every runtime permission to the launcher package. So
 * when a guest wants GPS, the launcher must both declare the permission in its
 * manifest and ask the user for it -- the guest cannot hold a permission of its
 * own. The grant is then visible to the guest for free, because a plugin's
 * context ({@link PluginContext}) delegates identity and system services to the
 * launcher: {@code checkSelfPermission} on the plugin context already answers
 * with the launcher's grant state.
 *
 * {@link #ensure} is called on the way into a guest launch, so the prompt is
 * shown before the guest's own {@code requestPermissions} would fail silently.
 */
final class GuestPermissions {
    /** Dangerous permissions the launcher proxies for its guests. */
    private static final String[] PROXIED = {
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
    };

    /** Continuations keyed by request code; one launch is in flight at a time. */
    private static final Map<Integer, Runnable> PENDING = new HashMap<>();

    private GuestPermissions() {}

    /** The proxied permissions this package declares and the user has not granted yet. */
    static List<String> missing(Activity activity) {
        String[] declared = declaredPermissions(activity);
        List<String> out = new ArrayList<>();
        if (declared == null) return out;
        for (String permission : PROXIED) {
            if (!contains(declared, permission)) continue;
            if (activity.checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                out.add(permission);
            }
        }
        return out;
    }

    /** True when every proxied permission this package declares is already granted. */
    static boolean allGranted(Activity activity) {
        return missing(activity).isEmpty();
    }

    /**
     * Requests whatever is missing, then runs {@code then}. If nothing is missing
     * it runs {@code then} at once, so callers can use this as a plain gate.
     */
    static void ensure(Activity activity, int requestCode, Runnable then) {
        List<String> need = missing(activity);
        if (need.isEmpty()) {
            then.run();
            return;
        }
        PENDING.put(requestCode, then);
        activity.requestPermissions(need.toArray(new String[0]), requestCode);
    }

    /**
     * Runs the continuation for {@code requestCode}, if one is waiting. Call from
     * the activity's {@code onRequestPermissionsResult}. Returns whether it ran.
     * The guest is allowed to start even on denial: a game that does not need the
     * permission should still launch, and one that does will show its own prompt.
     */
    static boolean onResult(int requestCode) {
        Runnable next = PENDING.remove(requestCode);
        if (next == null) return false;
        next.run();
        return true;
    }

    private static String[] declaredPermissions(Activity activity) {
        try {
            return activity.getPackageManager()
                    .getPackageInfo(activity.getPackageName(), PackageManager.GET_PERMISSIONS)
                    .requestedPermissions;
        } catch (PackageManager.NameNotFoundException e) {
            return null;
        }
    }

    private static boolean contains(String[] haystack, String needle) {
        for (String candidate : haystack) {
            if (needle.equals(candidate)) return true;
        }
        return false;
    }
}
