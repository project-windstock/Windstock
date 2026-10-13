package com.zettabridge.launcher;

import android.app.Activity;
import android.content.Intent;
import android.view.ViewGroup;
import android.widget.LinearLayout;

/**
 * The app's three sections, and the Material 3 navigation bar at the bottom of every screen that
 * switches between them.
 *
 * <p>The sections are the whole structure of the app, which is why they are a bar under the content
 * rather than a list of links inside it: <b>Apps</b> is the library - the apps that have been
 * imported, each one a launch; <b>Add apps</b> is how an APK gets in; <b>Settings</b> is everything
 * that applies to the launcher itself. A screen names its own section, so the bar always says where
 * the user is, and tapping another section goes there.
 *
 * <p>Each section is its own activity, so the switch is a real navigation rather than a page swap:
 * the screen being left is finished, which keeps the back stack one window deep and means Back from
 * a section leaves the app - the behaviour of a top-level destination. The intent asks for
 * {@code REORDER_TO_FRONT} so that a section already in the task comes forward instead of being
 * opened a second time.
 */
final class Nav {
    private Nav() {}

    /** One section: the icon and label the bar shows, and the screen it means. */
    enum Dest {
        APPS(R.drawable.ic_apps_24, R.string.nav_apps, LibraryActivity.class),
        ADD_APPS(R.drawable.ic_add_24, R.string.nav_add_apps, ImportActivity.class),
        SETTINGS(R.drawable.ic_settings_24, R.string.action_settings, SettingsActivity.class);

        final int icon;
        final int label;
        final Class<? extends Activity> screen;

        Dest(int icon, int label, Class<? extends Activity> screen) {
            this.icon = icon;
            this.label = label;
            this.screen = screen;
        }
    }

    /**
     * The bar, with {@code selected} marked as the current section. Tapping the section the screen
     * already is does nothing (there is nowhere to go); tapping any other brings it forward.
     */
    static LinearLayout bar(Activity activity, Ui ui, Dest selected) {
        LinearLayout bar = ui.bottomBar();
        for (Dest dest : Dest.values()) {
            Runnable action = dest.screen == activity.getClass() ? () -> {} : () -> open(activity, dest);
            bar.addView(ui.navItem(dest.icon, activity.getString(dest.label), dest == selected, action),
                    new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
        return bar;
    }

    /**
     * Goes to a section. The screen that was showing is finished: the bar is a switcher, not a
     * stack, so visiting a section twice does not leave two windows behind to press Back through.
     * The failure screen is the one caller that gains something from that - it is reachable from a
     * launch that did not open and has nothing else to offer but the way out.
     */
    static void open(Activity activity, Dest dest) {
        activity.startActivity(new Intent(activity, dest.screen)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
        activity.finish();
    }
}
