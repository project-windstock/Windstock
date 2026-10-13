package com.zettabridge.launcher;

import android.app.Activity;
import android.content.res.ColorStateList;
import android.content.res.TypedArray;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;

/**
 * The launcher's screens are built in code, so this is the one place that decides how they look -
 * and they look like the Settings app on a Pixel: Material 3, a large title across the top, lists of
 * rounded rows that carry an icon in a coloured circle, switches, and pill-shaped buttons.
 *
 * <p>What makes it read as Material 3 rather than as older Material:
 * <ul>
 *   <li>the top app bar is part of the layout - a surface-coloured band with the title at its left,
 *       running up behind the status bar (the platform's action bar cannot look like this, so every
 *       theme is a NoActionBar theme);</li>
 *   <li>a list row carries its icon in a filled circle, its title, optional supporting text under
 *       it, and a chevron when it leads somewhere - the shape the system UI uses throughout;</li>
 *   <li>groups are rounded containers a step above the surface, and the rows inside one are not
 *       divided from each other, because the tone already says where the group begins and ends;</li>
 *   <li>every colour comes from {@link Palette} - Google's fixed Material 3 baseline palette, so the
 *       app looks the same on every phone rather than taking a colour from the wallpaper.</li>
 * </ul>
 *
 * <p>The icons are the app's own vector drawables ({@code res/drawable/ic_*_24.xml}), tinted here
 * with the scheme's on-primary-container colour, so one icon works in both schemes.
 *
 * <p>An instance is built with an Activity, so it can set the window's bars and read the system
 * insets; the widgets are then plain {@code View}s the caller adds to a layout.
 */
final class Ui {
    /** "No icon", for a row or button that does not have one. */
    static final int NO_ICON = 0;

    private final Activity activity;
    private final Palette palette;

    Ui(Activity activity) {
        this.activity = activity;
        this.palette = Palette.of(activity);
    }

    Palette palette() {
        return palette;
    }

    // -------------------------------------------------------------- the window

    /**
     * Edge-to-edge, the way a Pixel draws: both bars transparent, the window colour showing through,
     * and the bar icons flipped to match the scheme. Called before the content is built.
     */
    void edgeToEdge() {
        Window window = activity.getWindow();
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.TRANSPARENT);
        window.setBackgroundDrawable(new android.graphics.drawable.ColorDrawable(palette.surface));
        int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
        // Dark icons on a light surface, light icons on a dark one - the two flags the platform has
        // for it, each from the release that added it.
        if (!palette.dark) flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (!palette.dark && android.os.Build.VERSION.SDK_INT >= 27) {
            flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        }
        window.getDecorView().setSystemUiVisibility(flags);
    }

    /** The screen's root: the surface colour, padded by the system bars, with the bars behind it. */
    LinearLayout root() {
        LinearLayout root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(palette.surface);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(0, insets.getSystemWindowInsetTop(), 0, insets.getSystemWindowInsetBottom());
            return insets;
        });
        return root;
    }

    /**
     * The large top app bar: surface-coloured, the title at its left in the medium weight the style
     * uses. No accent colour and no shadow, which is the visible difference from an old action bar.
     */
    LinearLayout topAppBar(CharSequence title) {
        LinearLayout bar = new LinearLayout(activity);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(palette.surface);
        bar.setPadding(dp(24), dp(12), dp(24), dp(16));
        bar.setMinimumHeight(dp(72));
        TextView heading = new TextView(activity);
        heading.setText(title);
        heading.setTextSize(TypedValue.COMPLEX_UNIT_SP, 26);
        heading.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        heading.setTextColor(palette.onSurface);
        bar.addView(heading, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        return bar;
    }

    /** A screen: the top app bar over a scrolling body, on the surface colour. */
    View screen(CharSequence title, View body) {
        return screen(title, body, null);
    }

    /**
     * A screen: the top app bar, a scrolling body, and - when one is given - the navigation bar at
     * the bottom, pinned under the content the way a Pixel keeps it.
     */
    View screen(CharSequence title, View body, View bottomBar) {
        LinearLayout root = root();
        root.addView(topAppBar(title), match());
        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.addView(body, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        if (bottomBar != null) root.addView(bottomBar, match());
        return root;
    }

    // ------------------------------------------------------- the navigation bar

    /**
     * The bottom bar itself: one section per destination, on a container a step above the surface.
     * The bar is the app's only structure - the top app bar names the section, this switches it.
     */
    LinearLayout bottomBar() {
        LinearLayout bar = new LinearLayout(activity);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(palette.surfaceContainer);
        bar.setPadding(dp(8), dp(8), dp(8), dp(8));
        return bar;
    }

    /**
     * One section in the bottom bar: the icon in the pill Material 3 marks the current section
     * with, and the label under it. Tapping it goes to that section.
     */
    View navItem(int icon, CharSequence label, boolean selected, Runnable action) {
        LinearLayout item = new LinearLayout(activity);
        item.setOrientation(LinearLayout.VERTICAL);
        item.setGravity(Gravity.CENTER_HORIZONTAL);
        item.setContentDescription(label);

        ImageView glyph = new ImageView(activity);
        glyph.setImageResource(icon);
        // The selected icon sits on the container colour; the rest are quiet, like the system's.
        glyph.setImageTintList(ColorStateList.valueOf(
                selected ? palette.onPrimaryContainer : palette.onSurfaceVariant));
        if (selected) {
            GradientDrawable pill = new GradientDrawable();
            pill.setCornerRadius(dp(16));
            pill.setColor(palette.primaryContainer);
            glyph.setBackground(pill);
        }
        glyph.setScaleType(ImageView.ScaleType.FIT_CENTER);
        // A 32dp-tall pill around a 24dp glyph, which is the indicator's own proportion.
        glyph.setPadding(dp(20), dp(4), dp(20), dp(4));
        item.addView(glyph, new LinearLayout.LayoutParams(dp(64), dp(32)));

        TextView text = new TextView(activity);
        text.setText(label);
        text.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        text.setTextColor(selected ? palette.onSurface : palette.onSurfaceVariant);
        text.setTypeface(Typeface.create(selected ? "sans-serif-medium" : "sans-serif",
                Typeface.NORMAL));
        text.setPadding(dp(2), dp(4), dp(2), 0);
        // The label sits under the icon whether or not it is as wide as the pill, so it is centred
        // in its own box rather than left-aligned at the item's edge.
        text.setGravity(Gravity.CENTER);
        text.setMaxLines(1);
        text.setEllipsize(TextUtils.TruncateAt.END);
        item.addView(text, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Drawable ripple = selectable();
        if (ripple != null) item.setBackground(ripple);
        item.setOnClickListener(v -> action.run());
        return item;
    }

    // ------------------------------------------------------------------- text

    /** A section label: small, bold, spaced, in the scheme's primary colour. */
    TextView section(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        view.setTypeface(Typeface.DEFAULT_BOLD);
        view.setLetterSpacing(0.02f);
        view.setTextColor(palette.primary);
        view.setPadding(dp(28), dp(20), dp(28), dp(6));
        return view;
    }

    /** The title of a list row. */
    TextView title(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        view.setTextColor(palette.onSurface);
        return view;
    }

    /** The supporting text under a row's title. */
    TextView summary(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        view.setTextColor(palette.onSurfaceVariant);
        view.setLineSpacing(0, 1.2f);
        view.setPadding(0, dp(2), 0, 0);
        return view;
    }

    /** Plain body text, for a screen that has something to say rather than something to set. */
    TextView body(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        view.setTextColor(palette.onSurface);
        view.setLineSpacing(0, 1.25f);
        return view;
    }

    /** Selectable, fixed-width text, for a report the user may have to read or copy by hand. */
    TextView mono(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        view.setTypeface(Typeface.MONOSPACE);
        view.setTextColor(palette.onSurface);
        view.setTextIsSelectable(true);
        return view;
    }

    /** A screen heading, for a dialog body or a failure screen. */
    TextView heading(CharSequence text) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 24);
        view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        view.setTextColor(palette.onSurface);
        return view;
    }

    // ---------------------------------------------------------------- surfaces

    /**
     * A group: the rounded container a related set of rows sits in, one step above the surface. This
     * is the shape the system's own settings pages use; rows inside it are not divided from each
     * other, because the tone and the spacing already say where they begin and end.
     */
    LinearLayout group() {
        LinearLayout group = new LinearLayout(activity);
        group.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable shape = new GradientDrawable();
        shape.setCornerRadius(dp(24));
        shape.setColor(palette.surfaceContainer);
        group.setBackground(shape);
        int pad = dp(4);
        group.setPadding(pad, pad, pad, pad);
        group.setClipToPadding(false);
        return group;
    }

    /** A hairline, for the one or two places a group of plain rows still needs one. */
    View divider() {
        View line = new View(activity);
        line.setBackgroundColor(palette.outlineVariant);
        return line;
    }

    // ------------------------------------------------------------------ rows

    /**
     * A row that leads somewhere or does something: the icon in its coloured circle, the label, and
     * a chevron at the end. Pass {@link #NO_ICON} for a row that has no icon, which is what the
     * system does for plain actions.
     */
    LinearLayout actionRow(int icon, CharSequence label, CharSequence supporting, Runnable action) {
        LinearLayout row = row(icon, 0, label, supporting);
        row.addView(chevron());
        clickable(row, action);
        return row;
    }

    /**
     * A settings row with a switch on it: the icon in its circle, the label and supporting text, and
     * the switch at the end. The whole row toggles it, as it does in the system UI.
     */
    LinearLayout toggleRow(int icon, CharSequence label, CharSequence supporting, boolean checked,
                          OnToggle sink) {
        LinearLayout row = row(icon, 0, label, supporting);
        Switch toggle = new Switch(activity);
        toggle.setChecked(checked);
        toggle.setContentDescription(label);
        // The thumb is the accent colour and the track the accent at the opacity Material uses for
        // it, so a chosen switch is a filled pill rather than a bare thumb.
        toggle.setThumbTintList(ColorStateList.valueOf(palette.primary));
        toggle.setTrackTintList(ColorStateList.valueOf(withAlpha(palette.primary, 0.55f)));
        toggle.setOnCheckedChangeListener((button, value) -> sink.set(value));
        row.addView(toggle, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        row.setOnClickListener(v -> toggle.setChecked(!toggle.isChecked()));
        return row;
    }

    /**
     * The row that chooses a scheme: a swatch of that scheme's colour in place of an icon, the name,
     * and a check mark when it is the one in use.
     */
    LinearLayout schemeRow(CharSequence label, int swatchColor, boolean selected, Runnable action) {
        LinearLayout row = row(NO_ICON, swatchColor, label, null);
        row.addView(selected ? check() : chevron());
        clickable(row, action);
        return row;
    }

    /** A one-line label and its current value, for a read-out rather than a control. */
    LinearLayout valueRow(CharSequence label, CharSequence value) {
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(20), dp(14), dp(20), dp(14));
        row.addView(summary(label), new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        TextView shown = title(value);
        shown.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        row.addView(shown);
        return row;
    }

    /**
     * A filled tonal button: the primary container as the fill, the matching text colour, and a
     * fully rounded corner - the pill a Pixel puts under a dialog or beside a setting.
     */
    Button pillButton(int icon, CharSequence text, Runnable action) {
        Button button = new Button(activity);
        button.setText(text);
        button.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        button.setAllCaps(false);
        button.setTextColor(palette.onPrimaryContainer);
        if (icon != NO_ICON) {
            Drawable glyph = activity.getDrawable(icon).mutate();
            glyph.setTint(palette.onPrimaryContainer);
            button.setCompoundDrawablePadding(dp(8));
            button.setCompoundDrawablesRelativeWithIntrinsicBounds(glyph, null, null, null);
        }
        GradientDrawable shape = new GradientDrawable();
        shape.setCornerRadius(dp(40));
        shape.setColor(palette.primaryContainer);
        button.setBackground(new RippleDrawable(
                ColorStateList.valueOf(withAlpha(palette.onPrimaryContainer, 0.2f)), shape, shape));
        button.setMinHeight(dp(44));
        button.setPadding(dp(28), 0, dp(28), 0);
        button.setOnClickListener(v -> action.run());
        return button;
    }

    /** The interface a switch reports to, without boxing a Boolean on every toggle. */
    interface OnToggle {
        void set(boolean value);
    }

    // -------------------------------------------------------------- the parts

    /**
     * The body of a row: an optional 40dp circle carrying a glyph or a colour swatch, the label and
     * its supporting text, and room for whatever the caller puts at the end.
     */
    private LinearLayout row(int icon, int swatch, CharSequence label, CharSequence supporting) {
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(16), dp(14), dp(20), dp(14));
        row.setMinimumHeight(dp(64));

        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        if (icon != NO_ICON) {
            row.addView(icon(icon), new LinearLayout.LayoutParams(dp(40), dp(40)));
            texts.setPadding(dp(16), 0, dp(12), 0);
        } else if (swatch != 0) {
            row.addView(swatch(swatch), new LinearLayout.LayoutParams(dp(40), dp(40)));
            texts.setPadding(dp(16), 0, dp(12), 0);
        } else {
            texts.setPadding(dp(4), 0, dp(4), 0);
        }
        texts.addView(title(label));
        if (supporting != null && supporting.length() > 0) texts.addView(summary(supporting));
        row.addView(texts, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        return row;
    }

    /**
     * An icon in a filled circle: the shape every icon in the system UI is drawn in. The icon itself
     * is one of this app's vector drawables, tinted here so it follows the scheme.
     */
    private ImageView icon(int res) {
        ImageView view = new ImageView(activity);
        int inset = dp(8);
        view.setPadding(inset, inset, inset, inset);
        view.setImageResource(res);
        view.setImageTintList(ColorStateList.valueOf(palette.onPrimaryContainer));
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(palette.primaryContainer);
        view.setBackground(circle);
        return view;
    }

    /** A plain filled circle, for the colour scheme rows. */
    private View swatch(int color) {
        View view = new View(activity);
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(color);
        view.setBackground(circle);
        return view;
    }

    /** The chevron a row that leads somewhere ends with. */
    private ImageView chevron() {
        return trailing(R.drawable.ic_chevron_right_24, palette.onSurfaceVariant);
    }

    /** The mark on the row that is the current choice. */
    private ImageView check() {
        return trailing(R.drawable.ic_check_24, palette.primary);
    }

    private ImageView trailing(int res, int color) {
        ImageView view = new ImageView(activity);
        view.setImageResource(res);
        view.setImageTintList(ColorStateList.valueOf(color));
        view.setLayoutParams(new LinearLayout.LayoutParams(dp(24), dp(24)));
        return view;
    }

    private void clickable(LinearLayout row, Runnable action) {
        Drawable ripple = selectable();
        if (ripple != null) row.setBackground(ripple);
        row.setOnClickListener(v -> action.run());
    }

    // ------------------------------------------------------------------- utils

    /** A colour with an alpha applied, for the ripples and tracks Material draws translucently. */
    private static int withAlpha(int color, float alpha) {
        return Color.argb((int) (255 * alpha), Color.red(color), Color.green(color), Color.blue(color));
    }

    /** The platform's own list-row ripple, which is the Material one for the theme in force. */
    private Drawable selectable() {
        TypedArray attributes = activity.obtainStyledAttributes(
                new int[] {android.R.attr.selectableItemBackground});
        try {
            return attributes.getDrawable(0);
        } catch (RuntimeException e) {
            return null;
        } finally {
            attributes.recycle();
        }
    }

    int dp(int value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                activity.getResources().getDisplayMetrics()));
    }

    /** A full-width, wrap-height row's parameters. */
    static LinearLayout.LayoutParams match() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    /** A divider's parameters: one hairline, full width. */
    LinearLayout.LayoutParams hairline() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1));
    }

    /** A group's outer spacing: the page margin a Pixel leaves around a card. */
    LinearLayout.LayoutParams groupParams() {
        LinearLayout.LayoutParams params = match();
        params.setMargins(dp(16), dp(4), dp(16), dp(8));
        return params;
    }
}
