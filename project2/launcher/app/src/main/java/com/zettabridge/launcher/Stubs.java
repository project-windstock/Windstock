package com.zettabridge.launcher;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.os.Bundle;
import android.util.TypedValue;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.zettabridge.core.ZBridge;

/**
 * Manifest-declared placeholder activities in :guest, one per launch mode plus fixed
 * orientations. GuestInstrumentation instantiates the plugin activity in their place, so these
 * classes only run when a stub intent arrives without a loadable plugin.
 *
 * <p>That case used to be a Toast and a finish(): the window appeared blank and was gone before
 * anyone could read it, which is indistinguishable from a crash. The stub now keeps its window and
 * says what it was asked for and why nothing came of it - it is the only component alive at that
 * point that knows the plugin never materialised.
 */
public final class Stubs {
    private Stubs() {}

    public static class Base extends Activity {
        @Override
        protected void onCreate(Bundle state) {
            super.onCreate(state);
            DebugLog.attach(this);
            DebugLog.note("stub " + getClass().getName() + " started with no plugin on its intent: "
                    + "the plugin activity could not be instantiated, so this window would otherwise "
                    + "close itself immediately");
            explain();
        }

        private void explain() {
            Intent intent = getIntent();
            String plugin = intent.getStringExtra(GuestRuntime.EXTRA_PLUGIN);
            String activity = intent.getStringExtra(GuestRuntime.EXTRA_ACTIVITY);
            String instantiation = GuestRuntime.lastInstantiationError();
            String bridgeError = null;
            try {
                bridgeError = ZBridge.lastLoadError();
            } catch (Throwable ignored) {
                // The bridge is optional here: the plugin may never have got that far.
            }

            StringBuilder detail = new StringBuilder();
            detail.append("The app's own screen never started, so this placeholder window is what ");
            detail.append("was left. It stays open so the reason can be read.\n\n");
            detail.append("Requested app: ").append(plugin == null ? "unknown" : plugin);
            detail.append("\nRequested screen: ").append(activity == null ? "unknown" : activity);
            detail.append("\nProcess: :guest (pid ").append(android.os.Process.myPid()).append(')');
            detail.append("\nGuest runtime installed: ").append(GuestRuntime.get().isInstalled());
            detail.append("\nWindow that closed: this placeholder's");
            if (instantiation != null) {
                detail.append("\n\nWhy the screen could not be created:\n").append(instantiation);
            } else {
                detail.append("\n\nWhy the screen could not be created: the intent named no plugin "
                        + "activity, which means the launch did not come from the library.");
            }
            if (bridgeError != null) {
                detail.append("\n\nBridge: ").append(bridgeError);
            }
            Diagnostics.report(this, "stub " + getClass().getSimpleName() + " without a plugin", 
                    new IllegalStateException(detail.toString()), false);

            int pad = Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 20,
                    getResources().getDisplayMetrics()));
            LinearLayout column = new LinearLayout(this);
            column.setOrientation(LinearLayout.VERTICAL);
            column.setPadding(pad, pad, pad, pad);

            TextView heading = new TextView(this);
            heading.setText(getString(R.string.could_not_open, getString(R.string.app_name)));
            heading.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
            heading.setTypeface(Typeface.DEFAULT_BOLD);
            column.addView(heading);

            TextView body = new TextView(this);
            // zb-errors.txt is written by both builds; the debug build's zb-app-log.txt is not
            // mentioned here, because a release build does not have one.
            body.setText(detail.toString() + "\n\nThis text is on the clipboard and in "
                    + "files/zb-errors.txt.");
            body.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
            body.setTextIsSelectable(true);
            body.setPadding(0, Math.round(pad * 0.6f), 0, Math.round(pad * 0.6f));
            column.addView(body);

            ScrollView scroll = new ScrollView(this);
            scroll.addView(column, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT));

            LinearLayout actions = new LinearLayout(this);
            actions.setOrientation(LinearLayout.VERTICAL);
            if (plugin != null) {
                Button retry = new Button(this);
                retry.setText("Try again");
                retry.setOnClickListener(v -> {
                    // A plugin that never loaded cannot be loaded in this process afterwards: the
                    // switch activity ends :guest and starts a fresh one.
                    startActivity(PluginSwitchActivity.intent(this, plugin));
                    finish();
                });
                actions.addView(retry);
            }
            Button close = new Button(this);
            close.setText("Close");
            close.setOnClickListener(v -> finish());
            actions.addView(close);

            LinearLayout root = new LinearLayout(this);
            root.setOrientation(LinearLayout.VERTICAL);
            root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
            root.addView(actions, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT));
            setContentView(root);

            Diagnostics.copy(this, getString(R.string.could_not_open, getString(R.string.app_name)),
                    detail.toString());
        }
    }

    public static class Standard extends Base {}

    public static class Landscape extends Base {}

    public static class Portrait extends Base {}

    public static class SingleTop extends Base {}

    public static class SingleTask extends Base {}

    public static class SingleInstance extends Base {}
}
