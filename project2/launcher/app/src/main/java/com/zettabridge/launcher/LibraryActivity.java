package com.zettabridge.launcher;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Intent;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.Bitmap;
import android.graphics.drawable.Icon;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Library screen (main process): import APKs, launch them, pin shortcuts, delete. UI built in code. */
public class LibraryActivity extends Activity {
    private static final String TAG = "Translator";
    private static final String GITHUB_URL = "https://github.com/Chucny";

    private final List<PluginRecord> plugins = new ArrayList<>();
    private BaseAdapter adapter;
    private TextView emptyView;
    /** The bundled app's install is running; one refresh is queued behind it. */
    private boolean seeding;
    /** The skin this window was built with, so a change made in the settings screen is applied. */
    private LauncherSettings.Skin appliedSkin;
    /** The bundled app's display name has been applied to its stored record already. */
    private boolean identityApplied;
    /** This screen's Material 3 widgets, in the palette the instant it was built in. */
    private Ui ui;

    @Override
    protected void onCreate(Bundle state) {
        // Before super.onCreate, so the whole window - and the dialogs it opens - is the chosen
        // scheme rather than a colour this activity paints itself with.
        appliedSkin = LauncherSettings.skin(this);
        setTheme(LauncherSettings.themeRes(this));
        super.onCreate(state);
        ui = new Ui(this);
        ui.edgeToEdge();

        LinearLayout root = ui.root();
        root.addView(ui.topAppBar(getString(R.string.app_name)), Ui.match());

        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(ui.dp(12), 0, ui.dp(12), ui.dp(8));

        // The Apps section of the bottom bar: the whole screen is the list of apps that have been
        // imported, each one a launch. Getting an app in is the Add apps section and the launcher's
        // own settings are the third, so this screen has no navigation of its own inside it.
        emptyView = ui.body(getString(R.string.library_empty));
        emptyView.setPadding(ui.dp(8), ui.dp(16), ui.dp(8), ui.dp(16));
        content.addView(emptyView);

        ListView list = new ListView(this);
        list.setDivider(null);
        list.setSelector(new android.graphics.drawable.ColorDrawable(0));
        list.setPadding(0, 0, 0, ui.dp(8));
        list.setClipToPadding(false);
        adapter = new BaseAdapter() {
            @Override
            public int getCount() {
                return plugins.size();
            }

            @Override
            public Object getItem(int position) {
                return plugins.get(position);
            }

            @Override
            public long getItemId(int position) {
                return position;
            }

            @Override
            public View getView(int position, View convertView, ViewGroup parent) {
                return row(plugins.get(position));
            }
        };
        list.setAdapter(adapter);
        list.setOnItemClickListener((parent, view, position, id) -> launch(plugins.get(position)));
        list.setOnItemLongClickListener((parent, view, position, id) -> {
            showActions(plugins.get(position));
            return true;
        });
        content.addView(list, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        content.addView(credit(), Ui.match());
        root.addView(content, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        root.addView(Nav.bar(this, ui, Nav.Dest.APPS), Ui.match());
        setContentView(root);
    }

    /** The footer: who made this, and the button that opens their GitHub. */
    private View credit() {
        LinearLayout footer = new LinearLayout(this);
        footer.setOrientation(LinearLayout.HORIZONTAL);
        footer.setGravity(Gravity.CENTER_VERTICAL | Gravity.CENTER_HORIZONTAL);
        footer.setPadding(0, ui.dp(12), 0, ui.dp(4));

        // GitHub's own octicon (primer/octicons, MIT). The icon is decorative; the button's
        // content description is what a screen reader announces.
        ImageButton github = new ImageButton(this);
        github.setImageResource(R.drawable.ic_github);
        github.setBackground(null);
        github.setContentDescription(getString(R.string.github_button));
        github.setScaleType(ImageView.ScaleType.FIT_CENTER);
        // An ImageButton carries default padding from its style, which leaves the 48dp mark drawn
        // at roughly a third of the 40dp box. The icon is meant to fill its button.
        github.setPadding(0, 0, 0, 0);
        github.setOnClickListener(v -> openGitHub());
        footer.addView(github, new LinearLayout.LayoutParams(ui.dp(32), ui.dp(32)));

        TextView madeBy = ui.summary(getString(R.string.made_by));
        madeBy.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        madeBy.setPadding(ui.dp(10), 0, 0, 0);
        footer.addView(madeBy);
        return footer;
    }

    /**
     * Opens this build's author on GitHub.
     *
     * A phone with no browser installed has nothing to hand an https intent to - this build was
     * tested on one - so the failure is reported to the user with the address instead of being
     * swallowed or crashing the launcher.
     */
    private void openGitHub() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(GITHUB_URL)));
        } catch (RuntimeException e) {
            Diagnostics.report(this, "cannot open " + GITHUB_URL, e, false);
            Toast.makeText(this, getString(R.string.github_no_browser, GITHUB_URL), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        // A skin chosen while this window was already up cannot be applied to it (setTheme after a
        // window exists does nothing), so the window is rebuilt instead. The rebuild sets the field
        // to the same value, so this cannot loop.
        if (LauncherSettings.skin(this) != appliedSkin) {
            recreate();
            return;
        }
        refresh();
    }

    private void refresh() {
        plugins.clear();
        plugins.addAll(PluginStore.list(this));
        emptyView.setVisibility(plugins.isEmpty() ? View.VISIBLE : View.GONE);
        adapter.notifyDataSetChanged();
        repointPinnedShortcuts();
        if (seeding) return;
        // A fresh install has nothing imported yet. Bring the bundled app in by itself, so the
        // user's first screen is the app they installed rather than an empty library. An app that
        // is already in gets the name this build was asked for, without a re-import.
        if (plugins.isEmpty()) installBundledApp();
        else applyBundledIdentity();
    }

    /** Writes the bundled app's name from this build onto its record, once per activity. */
    private void applyBundledIdentity() {
        if (identityApplied) return;
        identityApplied = true;
        new Thread(() -> {
            boolean changed;
            try {
                changed = SeedPlugin.reconcile(this);
            } catch (RuntimeException e) {
                Log.w(TAG, "cannot apply the bundled app's name: " + e);
                return;
            }
            if (changed) runOnUiThread(this::reload);
        }, "translator-identity").start();
    }

    /** Re-reads the stored apps into the list, without disturbing the guest or the shortcuts. */
    private void reload() {
        plugins.clear();
        plugins.addAll(PluginStore.list(this));
        emptyView.setVisibility(plugins.isEmpty() ? View.VISIBLE : View.GONE);
        adapter.notifyDataSetChanged();
    }

    /**
     * Brings this build's bundled app in, once, behind the first screen the user sees. Quiet by
     * design: a build with nothing bundled and one that already has its app are both normal, and
     * this runs unprompted. The button that does the same thing on request lives in Add apps, where
     * it reports what happened (see {@link ImportActivity}).
     */
    private void installBundledApp() {
        if (seeding) return;
        if (SeedPlugin.read(this) == null) return;
        if (SeedPlugin.installed(this) != null) return;
        seeding = true;
        // A running plugin keeps the previous APK mapped: stop the guest process first.
        ZbApplication.stopGuestProcess(this);
        AlertDialog progress = new AlertDialog.Builder(this)
                .setMessage(R.string.library_installing_bundled)
                .setCancelable(false)
                .show();
        new Thread(() -> {
            String message;
            try {
                PluginRecord r = SeedPlugin.installIfMissing(this);
                message = r == null ? getString(R.string.library_no_bundled)
                        : "Installed " + r.label + " (" + r.status() + ")";
            } catch (Exception e) {
                Log.e(TAG, "seeding the bundled app failed", e);
                message = "Install failed: " + e.getMessage();
            }
            String shown = message;
            runOnUiThread(() -> {
                seeding = false;
                identityApplied = true;  // installIfMissing reconciled the record already
                progress.dismiss();
                Toast.makeText(this, shown, Toast.LENGTH_LONG).show();
                reload();
                repointPinnedShortcuts();
            });
        }, "translator-seed").start();
    }

    /**
     * Points shortcuts pinned by an older version at the router. They were pinned straight at the
     * guest entry point, which Android resolves to the running game's task instead of launching.
     */
    private void repointPinnedShortcuts() {
        ShortcutManager sm = getSystemService(ShortcutManager.class);
        if (sm == null) return;
        try {
            List<ShortcutInfo> updated = new ArrayList<>();
            for (ShortcutInfo pinned : sm.getPinnedShortcuts()) {
                for (PluginRecord r : plugins) {
                    if (!shortcutId(r).equals(pinned.getId())) continue;
                    ComponentName target = pinned.getIntent() != null ? pinned.getIntent().getComponent() : null;
                    if (target != null && PluginSwitchActivity.class.getName().equals(target.getClassName())) break;
                    updated.add(new ShortcutInfo.Builder(this, pinned.getId())
                            .setIntent(PluginSwitchActivity.intent(this, r.packageName))
                            .build());
                    break;
                }
            }
            if (!updated.isEmpty()) sm.updateShortcuts(updated);
        } catch (RuntimeException e) {
            Log.w(TAG, "cannot repoint the pinned shortcuts: " + e);
        }
    }

    /** A library row in the Material 3 look: the app's icon, its name, and its state underneath. */
    private View row(PluginRecord r) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        int pad = ui.dp(16);
        row.setPadding(pad, ui.dp(14), pad, ui.dp(14));

        ImageView icon = new ImageView(this);
        Bitmap bmp = r.icon();
        if (bmp != null) {
            icon.setImageBitmap(bmp);
        } else {
            icon.setImageResource(android.R.drawable.sym_def_app_icon);
        }
        row.addView(icon, new LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)));

        LinearLayout texts = new LinearLayout(this);
        texts.setOrientation(LinearLayout.VERTICAL);
        texts.setPadding(ui.dp(16), 0, 0, 0);
        texts.addView(ui.title(r.label));
        texts.addView(ui.summary(r.displayName()
                + (r.versionName != null ? " " + r.versionName : "") + "\n" + r.status()));
        row.addView(texts);
        // Each app is its own rounded surface, the way an app list looks in the system UI.
        android.graphics.drawable.GradientDrawable shape = new android.graphics.drawable.GradientDrawable();
        shape.setCornerRadius(ui.dp(24));
        shape.setColor(ui.palette().surfaceContainer);
        row.setBackground(shape);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.setMargins(ui.dp(4), ui.dp(3), ui.dp(4), ui.dp(3));
        row.setLayoutParams(params);
        return row;
    }

    private void launch(PluginRecord r) {
        if (!r.isLaunchable()) {
            Toast.makeText(this, r.label + ": " + r.status(), Toast.LENGTH_LONG).show();
            return;
        }
        startActivity(PluginSwitchActivity.intent(this, r.packageName));
    }

    private void showActions(PluginRecord r) {
        new AlertDialog.Builder(this)
                .setTitle(r.label)
                .setItems(new String[] {getString(R.string.app_launch),
                                        getString(R.string.app_last_report),
                                        getString(R.string.app_pin),
                                        getString(r.hideAds ? R.string.app_show_ads : R.string.app_hide_ads),
                                        getString(r.preciseFaults
                                                ? R.string.app_faults_precise : R.string.app_faults_fast),
                                        getString(r.diagnostics
                                                ? R.string.app_gl_on : R.string.app_gl_off),
                                        getString(R.string.library_delete)},
                        (dialog, which) -> {
                            if (which == 0) launch(r);
                            if (which == 1) showRuntimeReport(r);
                            if (which == 2) pinShortcut(r);
                            if (which == 3) toggleAds(r);
                            if (which == 4) togglePreciseFaults(r);
                            if (which == 5) toggleDiagnostics(r);
                            if (which == 6) confirmDelete(r);
                        })
                .show();
    }

    /** Ads are hidden per plugin; it takes effect the next time the plugin process starts. */
    private void toggleAds(PluginRecord r) {
        r.hideAds = !r.hideAds;
        try {
            r.save();
        } catch (IOException e) {
            Diagnostics.report(this, "cannot save the ad setting of " + r.label, e, false);
            return;
        }
        Toast.makeText(this, (r.hideAds ? getString(R.string.app_hide_ads) + ": " : getString(R.string.app_show_ads) + ": ")
                + r.label + ". " + getString(R.string.app_restart_note), Toast.LENGTH_LONG).show();
    }

    /**
     * Precise memory faults stop at the exact faulting instruction with registers committed,
     * instead of at the end of the translated block, at roughly 2x cost on integer code. It takes
     * effect the next time the plugin process starts, and it is what the settings screen's
     * "Precise memory faults" default hands a new import - this is the per-app override of it.
     */
    private void togglePreciseFaults(PluginRecord r) {
        r.preciseFaults = !r.preciseFaults;
        try {
            r.save();
        } catch (IOException e) {
            Diagnostics.report(this, "cannot save the precise-faults setting of " + r.label, e, false);
            return;
        }
        Toast.makeText(this, getString(R.string.app_restart_note) + (r.preciseFaults
                        ? " Precise faults are on: slower, and crash reports point at the exact instruction."
                        : " Precise faults are off: faster on 32-bit code, at the cost of exact crash positions."),
                Toast.LENGTH_LONG).show();
    }

    /**
     * GL diagnostics instrument every draw: whole-framebuffer readbacks, per-draw pixel diffs and
     * ASCII frame maps in the run report. Debugging only, and very slow. It takes effect the next
     * time the plugin process starts.
     */
    private void toggleDiagnostics(PluginRecord r) {
        r.diagnostics = !r.diagnostics;
        try {
            r.save();
        } catch (IOException e) {
            Diagnostics.report(this, "cannot save the diagnostics setting of " + r.label, e, false);
            return;
        }
        Toast.makeText(this, getString(R.string.app_restart_note), Toast.LENGTH_LONG).show();
    }

    /**
     * What the last :guest run recorded, without a shell: OxygenOS drops third-party logcat
     * output, so this file is the only evidence a device run leaves behind. The text also goes to
     * the clipboard, ready to be sent back.
     */
    private void showRuntimeReport(PluginRecord r) {
        String report = Diagnostics.readRuntimeReport(this);
        if (report == null) {
            Toast.makeText(this, getString(R.string.settings_nothing_copied), Toast.LENGTH_LONG).show();
            return;
        }
        File file = Diagnostics.runtimeReportFile(this);
        String text = report + (file != null ? "\nfile: " + file.getAbsolutePath() + "\n" : "");
        boolean copied = Diagnostics.copy(this, getString(R.string.run_report_label, getString(R.string.app_name)),
                text);

        int pad = ui.dp(16);
        TextView view = ui.mono(text);
        view.setPadding(pad, pad, pad, pad);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(view);
        new AlertDialog.Builder(this)
                .setTitle(copied ? getString(R.string.app_last_report) + "  " + getString(R.string.settings_copied)
                        : getString(R.string.app_last_report))
                .setView(scroll)
                .setPositiveButton(android.R.string.ok, null)
                .show();
    }

    private static String shortcutId(PluginRecord r) {
        return "plugin:" + r.packageName;
    }

    private void pinShortcut(PluginRecord r) {
        ShortcutManager sm = getSystemService(ShortcutManager.class);
        if (sm == null || !sm.isRequestPinShortcutSupported()) {
            Toast.makeText(this, R.string.library_no_pin, Toast.LENGTH_LONG).show();
            return;
        }
        Bitmap bmp = r.icon();
        Icon icon = bmp != null ? Icon.createWithBitmap(bmp)
                : Icon.createWithResource(this, android.R.drawable.sym_def_app_icon);
        ShortcutInfo info = new ShortcutInfo.Builder(this, shortcutId(r))
                .setShortLabel(r.label)
                .setLongLabel(r.label)
                .setIcon(icon)
                .setIntent(PluginSwitchActivity.intent(this, r.packageName))
                .build();
        sm.requestPinShortcut(info, null);
    }

    private void confirmDelete(PluginRecord r) {
        new AlertDialog.Builder(this)
                .setMessage(getString(R.string.library_delete_message, r.label))
                .setPositiveButton(R.string.library_delete, (dialog, which) -> {
                    ZbApplication.stopGuestProcess(this);
                    ShortcutManager sm = getSystemService(ShortcutManager.class);
                    if (sm != null) {
                        try {
                            sm.disableShortcuts(Collections.singletonList(shortcutId(r)), "App was deleted");
                        } catch (RuntimeException e) {
                            Log.w(TAG, "cannot disable shortcut: " + e);
                        }
                    }
                    PluginStore.delete(r);
                    refresh();
                })
                .setNegativeButton(R.string.library_cancel, null)
                .show();
    }

}
