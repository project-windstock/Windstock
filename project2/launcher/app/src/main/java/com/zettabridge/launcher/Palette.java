package com.zettabridge.launcher;

import android.content.Context;

/**
 * The colours a screen is built from, in the roles Material 3 defines: the surface it sits on, the
 * containers stacked on that surface, and the text and outline colours that stay legible on each.
 *
 * <p>These are Google's own Material 3 baseline colours - the palette the design system is specified
 * with (primary {@code #6750A4} and the rest), which is what {@code res/values/colors.xml} holds.
 * They are fixed rather than taken from the phone: this app runs on phones of every make, so its look
 * is the same M3 design everywhere instead of something different from device to device. Light and
 * dark are the two schemes, and they are the same design read at two levels of light - the dark
 * scheme is not a recolouring but the same roles taken from the other end of the palette.
 *
 * <p>Nothing here is guessed at runtime and nothing needs a library: the values are resources, and
 * {@link #scheme} exists so the settings screen can draw a swatch of a scheme it is not currently in.
 */
final class Palette {
    /** Whether this is the dark scheme; the caller needs it for status-bar icons and edge-to-edge. */
    final boolean dark;
    final int primary;
    final int onPrimary;
    final int primaryContainer;
    final int onPrimaryContainer;
    /** The window and top-app-bar colour. */
    final int surface;
    /** A card's or a group's fill: one step above the surface. */
    final int surfaceContainer;
    /** A raised or selected fill: two steps above the surface. */
    final int surfaceContainerHigh;
    final int onSurface;
    final int onSurfaceVariant;
    final int outline;
    /** The hairline between rows, which Material 3 draws barely above the surface. */
    final int outlineVariant;

    private Palette(boolean dark, int primary, int onPrimary, int primaryContainer,
                    int onPrimaryContainer, int surface, int surfaceContainer, int surfaceContainerHigh,
                    int onSurface, int onSurfaceVariant, int outline, int outlineVariant) {
        this.dark = dark;
        this.primary = primary;
        this.onPrimary = onPrimary;
        this.primaryContainer = primaryContainer;
        this.onPrimaryContainer = onPrimaryContainer;
        this.surface = surface;
        this.surfaceContainer = surfaceContainer;
        this.surfaceContainerHigh = surfaceContainerHigh;
        this.onSurface = onSurface;
        this.onSurfaceVariant = onSurfaceVariant;
        this.outline = outline;
        this.outlineVariant = outlineVariant;
    }

    /** The palette for the scheme currently chosen on this device. */
    static Palette of(Context context) {
        return scheme(context, LauncherSettings.skin(context));
    }

    /**
     * The palette of one named scheme, whether or not it is the chosen one. The settings screen draws
     * a swatch per scheme, and a swatch has to show the colour it stands for.
     */
    static Palette scheme(Context context, LauncherSettings.Skin skin) {
        boolean dark = skin == LauncherSettings.Skin.DARK;
        if (dark) {
            return new Palette(true,
                    color(context, R.color.m3_primary_dark),
                    color(context, R.color.m3_on_primary_dark),
                    color(context, R.color.m3_primary_container_dark),
                    color(context, R.color.m3_on_primary_container_dark),
                    color(context, R.color.m3_surface_dark),
                    color(context, R.color.m3_surface_container_dark),
                    color(context, R.color.m3_surface_container_high_dark),
                    color(context, R.color.m3_on_surface_dark),
                    color(context, R.color.m3_on_surface_variant_dark),
                    color(context, R.color.m3_outline_dark),
                    color(context, R.color.m3_outline_variant_dark));
        }
        return new Palette(false,
                color(context, R.color.m3_primary_light),
                color(context, R.color.m3_on_primary_light),
                color(context, R.color.m3_primary_container_light),
                color(context, R.color.m3_on_primary_container_light),
                color(context, R.color.m3_surface_light),
                color(context, R.color.m3_surface_container_light),
                color(context, R.color.m3_surface_container_high_light),
                color(context, R.color.m3_on_surface_light),
                color(context, R.color.m3_on_surface_variant_light),
                color(context, R.color.m3_outline_light),
                color(context, R.color.m3_outline_variant_light));
    }

    private static int color(Context context, int res) {
        return context.getResources().getColor(res, context.getTheme());
    }
}
