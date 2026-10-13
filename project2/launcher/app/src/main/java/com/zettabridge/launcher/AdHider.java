package com.zettabridge.launcher;

import android.app.Activity;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.view.ViewTreeObserver;

/**
 * Hides ad views inside a plugin activity when the plugin has ads turned off.
 *
 * Ad SDKs bundled in old games cannot work here (their network calls fail, and Google Play
 * services answers a plugin with DEVELOPER_ERROR), so what is left on screen is a leftover banner
 * or an error message. This walks the view hierarchy on every layout pass and hides views whose
 * class comes from a known ad SDK package; nothing else is touched, and no SDK code is patched.
 */
final class AdHider {
    private static final String TAG = "Translator";
    private static final String[] AD_PACKAGES = {
        "com.google.android.gms.ads.",
        "com.google.ads.",
        "com.mopub.",
        "com.applovin.",
        "com.chartboost.",
        "com.inmobi.",
        "com.unity3d.ads.",
        "com.vungle.",
        "com.facebook.ads.",
        "com.ironsource.",
        "com.adcolony.",
        "com.startapp.",
    };
    private static final int MAX_DEPTH = 24;

    private AdHider() {}

    static void attach(Activity activity) {
        final View root = activity.getWindow() != null ? activity.getWindow().getDecorView() : null;
        if (root == null) return;
        root.getViewTreeObserver().addOnGlobalLayoutListener(new ViewTreeObserver.OnGlobalLayoutListener() {
            @Override
            public void onGlobalLayout() {
                try {
                    hide(root, 0);
                } catch (RuntimeException e) {
                    Log.w(TAG, "ad hider: " + e);
                    root.getViewTreeObserver().removeOnGlobalLayoutListener(this);
                }
            }
        });
    }

    private static void hide(View view, int depth) {
        if (depth > MAX_DEPTH) return;
        if (isAdView(view)) {
            if (view.getVisibility() != View.GONE) {
                Log.i(TAG, "hiding ad view " + view.getClass().getName());
                view.setVisibility(View.GONE);
            }
            return;  // its children go with it
        }
        if (view instanceof ViewGroup) {
            ViewGroup group = (ViewGroup) view;
            for (int i = 0; i < group.getChildCount(); i++) hide(group.getChildAt(i), depth + 1);
        }
    }

    private static boolean isAdView(View view) {
        String name = view.getClass().getName();
        for (String prefix : AD_PACKAGES) {
            if (name.startsWith(prefix)) return true;
        }
        return false;
    }
}
