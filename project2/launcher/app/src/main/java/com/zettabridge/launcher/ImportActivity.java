package com.zettabridge.launcher;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.widget.LinearLayout;
import android.widget.Toast;

/**
 * The Add apps section: how an APK gets into the library.
 *
 * <p>Two ways in, and they are the same machinery: pick a file with the system's own picker, or
 * install the app this build carries. Both write into the launcher's private storage through
 * {@link PluginStore} and {@link SeedPlugin} - neither installs anything for the rest of the phone,
 * and the file that was picked is left where it was.
 *
 * <p>The work is done on a thread behind a non-cancellable dialog, because an import copies, parses
 * and rewrites an APK's native libraries and can take seconds; the section then shows nothing new
 * itself, because the app it just added belongs to Apps. It only reports what happened.
 */
public class ImportActivity extends Activity {
    private static final String TAG = "Translator";
    private static final int REQUEST_IMPORT = 7;

    private Ui ui;

    @Override
    protected void onCreate(Bundle state) {
        // Before the window exists, like every other screen: the scheme is what the theme is.
        setTheme(LauncherSettings.themeRes(this));
        super.onCreate(state);
        ui = new Ui(this);
        ui.edgeToEdge();

        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(0, 0, 0, ui.dp(24));

        column.addView(ui.section(getString(R.string.imports_title)), Ui.match());
        LinearLayout group = ui.group();
        column.addView(group, ui.groupParams());
        group.addView(ui.actionRow(R.drawable.ic_add_24, getString(R.string.library_import),
                getString(R.string.imports_pick_note), this::pickApk), Ui.match());
        // This build can ship one app inside itself; installing it here is what makes a fresh
        // install work with no file picker and no adb push. A build without the asset hides the row.
        if (SeedPlugin.read(this) != null) {
            group.addView(ui.divider(), ui.hairline());
            group.addView(ui.actionRow(R.drawable.ic_download_24,
                    getString(R.string.library_install_bundled),
                    getString(R.string.imports_bundled_note), () -> installBundled(false)), Ui.match());
        }
        column.addView(ui.summary(getString(R.string.imports_note)), Ui.match());
        int last = column.getChildCount() - 1;
        column.getChildAt(last).setPadding(ui.dp(28), ui.dp(4), ui.dp(28), 0);

        setContentView(ui.screen(getString(R.string.nav_add_apps), column,
                Nav.bar(this, ui, Nav.Dest.ADD_APPS)));
    }

    private void pickApk() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("*/*")
                .putExtra(Intent.EXTRA_MIME_TYPES,
                        new String[] {"application/vnd.android.package-archive", "application/octet-stream"});
        startActivityForResult(intent, REQUEST_IMPORT);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_IMPORT && resultCode == RESULT_OK && data != null && data.getData() != null) {
            importApk(data.getData());
        }
    }

    private void importApk(Uri uri) {
        // A running plugin keeps the old APK mapped: stop the guest process first.
        ZbApplication.stopGuestProcess(this);
        AlertDialog progress = new AlertDialog.Builder(this)
                .setMessage(R.string.library_importing).setCancelable(false).show();
        new Thread(() -> {
            String message;
            try {
                PluginRecord r = PluginStore.importApk(this, uri);
                message = "Imported " + r.label + " (" + r.status() + ")";
                // A hand import lands under the client's own name. If the file is the app this build
                // bundles, the launcher's own name belongs on it, and reconcile is what puts it back.
                SeedPlugin.reconcile(this);
            } catch (Exception e) {
                Log.e(TAG, "import failed", e);
                message = "Import failed: " + e.getMessage();
            }
            String shown = message;
            runOnUiThread(() -> {
                progress.dismiss();
                Toast.makeText(this, shown, Toast.LENGTH_LONG).show();
            });
        }, "translator-import").start();
    }

    /**
     * Installs the app this build bundles. `automatic` is the library's first-screen call, which is
     * quiet about a build that carries nothing and about one already installed - both normal - while
     * the button press reports them.
     */
    private void installBundled(boolean automatic) {
        if (SeedPlugin.read(this) == null) {
            if (!automatic) Toast.makeText(this, R.string.library_no_bundled, Toast.LENGTH_LONG).show();
            return;
        }
        if (SeedPlugin.installed(this) != null) {
            if (!automatic) Toast.makeText(this, R.string.imports_already_installed, Toast.LENGTH_LONG).show();
            return;
        }
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
                progress.dismiss();
                Toast.makeText(this, shown, Toast.LENGTH_LONG).show();
            });
        }, "translator-seed").start();
    }
}
