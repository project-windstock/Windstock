"""
editor.py -- the desktop asset editor.

A CustomTkinter window over :mod:`asset_tools.assetlib`: pick a Pokemon, paint or
recolour its textures, spin the model, build a variant bundle. Everything it does
is the same call the CLI makes, so what you see here is what
``py src/asset_tools/cli.py build`` will do.

    py src/asset_tools/cli.py editor
    py src/asset_tools/cli.py editor pm0025

Why a desktop app and not a page in the World Manager: this needs to be usable on
a machine that has never run the game server, and it must not be able to touch a
served bundle by accident. Two panels, one rule -- *nothing reaches disk until you
press Build*, and Build only ever writes a new variant name unless you explicitly
ask to replace a base bundle, which is refused unless assets.allow_replace is on.

The window is threaded, not because Tk is unsafe off-thread (it is not) but
because opening a bundle decrypts ~450 KB, rebuilding one repacks and re-encrypts
it, and a 400x400 render is a few hundred milliseconds. Doing any of that on the
UI thread freezes the window, and a frozen window during a repack reads as a
crash. So the work runs on a worker and results come back through a queue that
``after()`` drains.
"""
import os
import queue
import sys
import threading
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from asset_tools import assetlib as A
from asset_tools import config
from asset_tools import meshdata
from asset_tools import render3d

# Editor-only. Overwritten colours are kept in the working image until Build, so
# a mis-click is undoable and nothing on disk moves until then.
BRUSH_COLOURS = [(255, 255, 255, 255), (0, 0, 0, 255), (255, 60, 60, 255),
                 (60, 160, 255, 255), (255, 220, 60, 255), (60, 220, 140, 255),
                 (200, 90, 255, 255), (0, 0, 0, 0)]


class Job:
    """A unit of work for the worker thread."""

    def __init__(self, fn, *args, **kwargs):
        self.fn, self.args, self.kwargs = fn, args, kwargs


class Editor(ctk.CTk):
    """The main window. One bundle open at a time, one texture painted at a time."""

    POLL_MS = 40
    PREVIEW_PX = 380
    # While the mouse is down, render at a fraction of the canvas and let Tk
    # scale the bitmap up, then draw one sharp frame once the drag settles. The
    # cost of a frame is dominated by per-pixel work, so halving the resolution
    # roughly quarters it; at a 640px canvas that is the difference between a
    # preview that keeps up with the mouse and one that lags seconds behind.
    DRAFT_SCALE = 0.5
    SETTLE_MS = 200

    def __init__(self, platform="android", name=None):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Windstock Asset Editor")
        self.geometry("1440x900")
        self.minsize(1100, 700)

        self.platform = platform
        self.bundle = None            # open assetlib.Bundle
        self.bundle_name = ""
        self.texture_id = None        # path_id being edited
        self.image = None             # working PIL image for that texture
        self.original = None          # ...as loaded, for revert
        self.dirty = False            # working image differs from the bundle
        self.staged = {}              # path_id -> PIL image the user has drawn
        self._orig_png = {}           # path_id -> pristine PNG, for Revert
        self.model = None             # meshdata dict for the 3D view
        self.yaw, self.pitch = 32.0, -12.0
        self.preset = "shiny"
        self.variant = "s"
        self._tex_photo = None        # keep ImageTk refs alive
        self._model_photo = None
        self._tex_by_label = {}
        self._zoom = 1.0
        self._zoom_model = 1.0
        self._pan = (0, 0)
        self._pan_anchor = None
        self._orbit_from = None
        self._paint = None            # 'draw' | 'erase' | None
        self._last_pt = None
        self.brush_rgba = BRUSH_COLOURS[0]
        self.results = queue.Queue()
        self._closing = False
        self._render_pending = False
        self._render_req = None
        self._settle_id = None
        self._interacting = False
        self._full_size = 0

        self._build_ui()
        self._set_preset("shiny")
        self._load_list(name)
        self.after(self.POLL_MS, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._bar()
        self._left()
        self._centre()
        self._right()
        self._log()

    def _bar(self):
        bar = ctk.CTkFrame(self, fg_color=("#23262e", "#1b1d23"), corner_radius=0)
        bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        bar.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(bar, text="Asset Editor", font=ctk.CTkFont(size=17,
                                                                weight="bold")
                     ).grid(row=0, column=0, padx=(14, 10), pady=8)
        self.platform_menu = ctk.CTkOptionMenu(
            bar, values=["android", "ios"], command=self._switch_platform,
            width=100)
        self.platform_menu.grid(row=0, column=1, padx=6)
        self.where = ctk.CTkLabel(bar, text="", anchor="w", text_color="#9aa3b2")
        self.where.grid(row=0, column=2, padx=10)
        ctk.CTkButton(bar, text="Open editor folder", width=140, height=28,
                      fg_color="transparent", border_width=1,
                      command=self._open_settings).grid(row=0, column=4, padx=8)
        ctk.CTkButton(bar, text="Refresh", width=90, height=28,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._load_list(self.bundle_name)
                      ).grid(row=0, column=5, padx=(0, 14))

    def _left(self):
        wrap = ctk.CTkFrame(self, width=250)
        wrap.grid(row=1, column=0, sticky="nsw", padx=(10, 0), pady=10)
        wrap.grid_rowconfigure(3, weight=1)
        wrap.grid_columnconfigure(0, weight=1)
        self.search = ctk.CTkEntry(wrap, placeholder_text="Search name or dex")
        self.search.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        self.search.bind("<KeyRelease>", lambda e: self._filter_list())
        self.replace_hint = ctk.CTkLabel(
            wrap, text="", text_color="#e0a33e", anchor="w", wraplength=210)
        self.replace_hint.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        ctk.CTkLabel(wrap, text="Bundles", anchor="w",
                     text_color="#9aa3b2").grid(row=2, column=0, sticky="ew",
                                                padx=10)
        self.listbox = ctk.CTkScrollableFrame(wrap)
        self.listbox.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.rows = {}
        self.saved = ctk.CTkFrame(wrap, fg_color="transparent")
        self.saved.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 10))

    def _centre(self):
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=1, sticky="nsew", padx=10, pady=10)

        paint = self.tabs.add("Texture")
        paint.grid_columnconfigure(0, weight=1)
        paint.grid_rowconfigure(1, weight=1)
        self.texture_bar = ctk.CTkFrame(paint, fg_color="transparent")
        self.texture_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        self.texture_bar.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(self.texture_bar, text="Texture", anchor="w"
                     ).grid(row=0, column=0, padx=(6, 8))
        self.texture_menu = ctk.CTkOptionMenu(
            self.texture_bar, values=["(open a bundle)"], width=280,
            command=self._pick_texture)
        self.texture_menu.grid(row=0, column=1, padx=4)
        self.tex_info = ctk.CTkLabel(self.texture_bar, text="", text_color="#9aa3b2")
        self.tex_info.grid(row=0, column=2, padx=8)
        self.dirty_tag = ctk.CTkLabel(self.texture_bar, text="")
        self.dirty_tag.grid(row=0, column=4, padx=8)

        self.canvas_host = ctk.CTkFrame(paint, fg_color=("#1a1c22", "#15171c"))
        self.canvas_host.grid(row=1, column=0, sticky="nsew", padx=8)
        self.canvas_host.grid_rowconfigure(0, weight=1)
        self.canvas_host.grid_columnconfigure(0, weight=1)
        self.canvas = ctk.CTkCanvas(self.canvas_host, bg="#15171c",
                                    highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self._redraw_canvas())
        self.canvas.bind("<ButtonPress-1>", lambda e: self._canvas_down(e))
        self.canvas.bind("<B1-Motion>", self._canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_up)
        self.canvas.bind("<MouseWheel>", self._canvas_wheel)
        self.canvas.bind("<ButtonPress-3>", lambda e: self._pan_start(e))
        self.canvas.bind("<B3-Motion>", self._canvas_pan)
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._pan_start(e, True))

        model = self.tabs.add("3D preview")
        model.grid_columnconfigure(0, weight=1)
        model.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(model, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(bar, text="Drag to orbit, scroll to zoom"
                     ).grid(row=0, column=0, padx=6)
        ctk.CTkButton(bar, text="Reset view", width=100, height=26,
                      fg_color="transparent", border_width=1,
                      command=self._reset_view).grid(row=0, column=1, padx=6)
        ctk.CTkButton(bar, text="Rerender", width=100, height=26,
                      fg_color="transparent", border_width=1,
                      command=lambda: self._render_model(force=True)
                      ).grid(row=0, column=2, padx=6)
        self.model_info = ctk.CTkLabel(bar, text="", text_color="#9aa3b2")
        self.model_info.grid(row=0, column=3, padx=10)
        self.model_host = ctk.CTkFrame(model, fg_color="#15171c")
        self.model_host.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.model_host.grid_rowconfigure(0, weight=1)
        self.model_host.grid_columnconfigure(0, weight=1)
        self.model_canvas = ctk.CTkCanvas(self.model_host, bg="#15171c",
                                          highlightthickness=0)
        self.model_canvas.grid(row=0, column=0, sticky="nsew")
        self.model_canvas.bind("<Configure>",
                               lambda e: self._render_model(force=True))
        self.model_canvas.bind("<ButtonPress-1>", self._orbit_start)
        self.model_canvas.bind("<B1-Motion>", self._orbit_move)
        self.model_canvas.bind("<MouseWheel>", self._orbit_wheel)

    def _right(self):
        side = ctk.CTkScrollableFrame(self, width=300)
        side.grid(row=1, column=2, sticky="nse", padx=(0, 10), pady=10)

        ctk.CTkLabel(side, text="Paint", anchor="w", font=ctk.CTkFont(
            weight="bold")).pack(anchor="w", padx=10, pady=(10, 4))
        tools = ctk.CTkFrame(side, fg_color="transparent")
        tools.pack(fill="x", padx=6)
        self.tool_menu = ctk.CTkOptionMenu(tools, values=["Draw", "Erase"],
                                           width=90, command=self._set_tool)
        self.tool_menu.grid(row=0, column=0, padx=2)
        self.brush = ctk.CTkSlider(tools, from_=1, to=64, number_of_steps=63,
                                   width=110, command=lambda v: None)
        self.brush.set(12)
        self.brush.grid(row=0, column=1, padx=6)
        ctk.CTkLabel(tools, text="size").grid(row=0, column=2)
        swatches = ctk.CTkFrame(side, fg_color="transparent")
        swatches.pack(fill="x", padx=6, pady=6)
        for i, c in enumerate(BRUSH_COLOURS):
            hexv = "#%02x%02x%02x" % c[:3]
            btn = ctk.CTkButton(swatches, text="", width=26, height=26,
                                fg_color=hexv, hover_color=hexv,
                                border_width=0,
                                command=lambda col=c: self._set_colour(col))
            btn.grid(row=i // 4, column=i % 4, padx=2, pady=2)
        self.colour = ctk.CTkButton(side, text="Foreground", width=110,
                                    height=28, fg_color="#ffffff",
                                    hover_color="#ffffff", text_color="#111111",
                                    command=self._pick_colour)
        self.colour.pack(pady=2)
        ctk.CTkButton(side, text="Revert texture", height=26,
                      fg_color="transparent", border_width=1,
                      command=self._revert).pack(fill="x", padx=10, pady=4)
        ctk.CTkButton(side, text="Apply preset to this texture", height=28,
                      command=self._apply_preset_here
                      ).pack(fill="x", padx=10, pady=4)

        ctk.CTkLabel(side, text="Recolour", anchor="w", font=ctk.CTkFont(
            weight="bold")).pack(anchor="w", padx=10, pady=(12, 4))
        self.preset_menu = ctk.CTkOptionMenu(
            side, values=sorted(A.PRESETS) + ["none"], command=self._set_preset)
        self.preset_menu.set("shiny")
        self.preset_menu.pack(fill="x", padx=10)
        self.sliders = {}
        for key, label, lo, hi in (("dh", "Hue shift", -0.25, 0.25),
                                   ("ks", "Saturation", 0.0, 2.0),
                                   ("kv", "Value", 0.0, 1.8)):
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(row, text=label, width=88, anchor="w"
                         ).grid(row=0, column=0)
            s = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=250,
                              width=120, command=self._preset_moved)
            s.grid(row=0, column=1)
            val = ctk.CTkLabel(row, text="", width=46, text_color="#9aa3b2")
            val.grid(row=0, column=2)
            self.sliders[key] = (s, val)
        tint = ctk.CTkFrame(side, fg_color="transparent")
        tint.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(tint, text="Tint", width=88, anchor="w").grid(row=0, column=0)
        self.tint_button = ctk.CTkButton(tint, text="none", width=64, height=24,
                                         fg_color="#5a5f6b", hover_color="#5a5f6b",
                                         command=self._pick_tint)
        self.tint_button.grid(row=0, column=1)
        self.tint_amount = ctk.CTkSlider(tint, from_=0.0, to=1.0,
                                         number_of_steps=100, width=120,
                                         command=self._tint_moved)
        self.tint_amount.set(0.6)
        self.tint_amount.grid(row=1, column=1, pady=2)
        self.tint = None

        ctk.CTkLabel(side, text="Build", anchor="w", font=ctk.CTkFont(
            weight="bold")).pack(anchor="w", padx=10, pady=(12, 4))
        build = ctk.CTkFrame(side, fg_color="transparent")
        build.pack(fill="x", padx=6)
        ctk.CTkLabel(build, text="Variant", width=60, anchor="w").grid(row=0, column=0)
        self.variant_entry = ctk.CTkEntry(build, width=70)
        self.variant_entry.insert(0, "s")
        self.variant_entry.grid(row=0, column=1, padx=4)
        self.replace_check = ctk.CTkCheckBox(
            build, text="replace base", width=16,
            command=self._replace_toggled)
        self.replace_check.grid(row=1, column=0, columnspan=2, sticky="w",
                                pady=(6, 0))
        self.build_button = ctk.CTkButton(side, text="Build variant", height=32,
                                          font=ctk.CTkFont(weight="bold"),
                                          command=self._build)
        self.build_button.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkButton(side, text="Export glTF", height=26,
                      fg_color="transparent", border_width=1,
                      command=self._export_gltf).pack(fill="x", padx=10, pady=2)
        ctk.CTkButton(side, text="Verify all bundles", height=26,
                      fg_color="transparent", border_width=1,
                      command=self._verify).pack(fill="x", padx=10, pady=2)

    def _log(self):
        self.logbox = ctk.CTkTextbox(self, height=132, font=ctk.CTkFont(
            family="Consolas", size=11), activate_scrollbars=True)
        self.logbox.grid(row=2, column=0, columnspan=3, sticky="nsew",
                         padx=10, pady=(0, 10))
        self.logbox.configure(state="disabled")
        self.grid_rowconfigure(2, weight=0)

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def say(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", str(msg).rstrip() + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def submit(self, fn, *a, **kw):
        """Run work off the UI thread; the result arrives in self.results."""
        job = Job(fn, *a, **kw)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job):
        try:
            out = job.fn(*job.args, **job.kwargs)
        except Exception as exc:                       # noqa: BLE001
            out = ("error", exc, traceback.format_exc())
        self.results.put((job, out))

    def _drain(self):
        while True:
            try:
                job, out = self.results.get_nowait()
            except queue.Empty:
                break
            if isinstance(out, tuple) and len(out) == 3 and out[0] == "error":
                self.say("!! %s" % out[1])
                if os.environ.get("WINDSTOCK_EDITOR_TRACE"):
                    self.say(out[2])
                self._set_busy(False)
            else:
                self._finish(job, out)
        if not self._closing:
            self.after(self.POLL_MS, self._drain)

    def _set_busy(self, busy):
        self.build_button.configure(state="disabled" if busy else "normal",
                                    text="Building..." if busy else "Build variant")

    # ------------------------------------------------------------------
    # bundle list
    # ------------------------------------------------------------------
    def _load_list(self, select=None):
        for w in self.listbox.winfo_children():
            w.destroy()
        self.rows = {}
        self._all = A.list_bundles(self.platform)
        info = config.describe(self.platform)
        self.where.configure(text="%d bundles  |  %s" % (len(self._all),
                                                         info["dir"]))
        self.replace_hint.configure(
            text="" if not info["missing_configured"] else
            "settings.json points at a missing folder; using the shipped set")
        if not info["allow_replace"]:
            self.replace_check.configure(state="disabled")
            self.replace_hint.configure(
                text="replace is off (assets.allow_replace in settings.json)"
                if not self.replace_hint.cget("text") else
                self.replace_hint.cget("text"))
        self._filter_list(select)

    def _filter_list(self, select=None):
        needle = (self.search.get() or "").strip().lower()
        for w in self.listbox.winfo_children():
            w.destroy()
        self.rows = {}
        shown = 0
        for row in self._all:
            hay = ("%s %s" % (row["name"], row["label"])).lower()
            if needle and needle not in hay:
                continue
            shown += 1
            if shown > 400:
                break
            btn = ctk.CTkButton(
                self.listbox, anchor="w", height=26, corner_radius=4,
                text="%-9s %s%s" % (row["name"], row["label"][:14],
                                    "  *" if row["ours"] else ""),
                font=ctk.CTkFont(size=12),
                fg_color="transparent",
                hover_color=("#2a2e38", "#22252c"),
                command=lambda n=row["name"]: self._open(n))
            btn.pack(fill="x", pady=1)
            self.rows[row["name"]] = btn
        if select and select in self.rows:
            self.rows[select].configure(fg_color=("#2f3b4d", "#28323f"))

    def _switch_platform(self, value):
        self._close_bundle()
        self.platform = value
        self._load_list()

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------
    def _open(self, name):
        if self.bundle is not None and self.dirty and not self._confirm_discard():
            return
        self._close_bundle()
        self.say("opening %s..." % name)
        self.submit(self._open_job, name)

    def _open_job(self, name):
        bundle = A.Bundle(name, self.platform)
        model = None
        try:
            model = meshdata.load(bundle)
        except Exception:
            model = None                    # a bundle with no skin is still editable
        return bundle, model

    def _close_bundle(self):
        if self.bundle is not None:
            try:
                self.bundle.close()
            except Exception:
                pass
        self.bundle = None
        self.model = None
        self.image = None
        self.original = None
        self.texture_id = None
        self.dirty = False
        self.dirty_tag.configure(text="")
        self.model_canvas.delete("all")
        self.canvas.delete("all")

    def _confirm_discard(self):
        from tkinter import messagebox
        return messagebox.askokcancel(
            "Unsaved paint",
            "This texture has unsaved edits. Build to keep them, or discard?")

    def _on_close(self):
        self._closing = True
        self._close_bundle()
        self.destroy()

    # ------------------------------------------------------------------
    # texture pane
    # ------------------------------------------------------------------
    def _pick_texture(self, label):
        if self.bundle is None:
            return
        row = self._tex_by_label.get(label)
        if row is None:
            return
        if self.dirty and self.texture_id is not None:
            self._push_edit()
        path_id = row["path_id"]

        # The bundle cannot be the source of truth for a texture the user drew.
        # set_texture assigns the image onto a parsed Texture2D and calls save(),
        # but reading that object back re-parses the original serialised bytes, so
        # the edit is already gone: measured on pm0147, m_TextureFormat stayed 5
        # and image_data stayed 21,844 bytes, and texture_png returned the shipped
        # pixels. So reloading from the bundle on every switch is what made a drawn
        # texture disappear the moment you clicked another one.
        #
        # self.staged is therefore the authoritative in-memory copy -- it is also
        # what Build sends, so it cannot drift from what gets written. The pristine
        # PNG is cached alongside it so Revert still has something true to restore.
        import io
        png = self._orig_png.get(path_id)
        if png is None:
            try:
                png = self.bundle.texture_png(path_id)
            except Exception as exc:
                self.say("!! cannot read that texture: %s" % exc)
                return
            self._orig_png[path_id] = png
        with Image.open(io.BytesIO(png)) as im:
            self.original = im.convert("RGBA")

        drawn = self.staged.get(path_id)
        self.image = drawn.copy() if drawn is not None else self.original.copy()
        self.texture_id = path_id
        self.dirty = False
        self.tex_info.configure(
            text="%d x %d  %s%s" % (row.get("width") or 0, row.get("height") or 0,
                                    row.get("format_name") or "",
                                    "  (re-encoded on save)"
                                    if row.get("reencode") else ""))
        self.dirty_tag.configure(text="")
        self._zoom, self._pan = 1.0, (0, 0)
        self._redraw_canvas()

    def _set_tool(self, value):
        self._paint = "erase" if value.lower().startswith("e") else "draw"

    def _set_colour(self, rgba):
        self.brush_rgba = tuple(rgba)
        self.colour.configure(fg_color="#%02x%02x%02x" % rgba[:3],
                              text_color="#ffffff" if sum(rgba[:3]) < 380
                              else "#111111")

    def _pick_colour(self):
        from tkinter import colorchooser
        rgb = colorchooser.askcolor(title="Brush colour")
        if rgb and rgb[0]:
            self._set_colour(tuple(rgb[0]) + (255,))

    def _canvas_pos(self, event):
        return event.x, event.y

    def _tex_pixel(self, cx, cy):
        """Canvas pixel -> texture pixel, or None if outside the image."""
        if self.image is None:
            return None
        w, h = self.image.size
        ox, oy = self._origin()
        ix = int((cx - ox) / self._zoom)
        iy = int((cy - oy) / self._zoom)
        if 0 <= ix < w and 0 <= iy < h:
            return ix, iy
        return None

    def _origin(self):
        w, h = self.image.size if self.image else (0, 0)
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        return (cw - w * self._zoom) / 2 + self._pan[0], \
               (ch - h * self._zoom) / 2 + self._pan[1]

    def _canvas_down(self, event):
        if self.image is None:
            return
        self._last_pt = self._tex_pixel(*self._canvas_pos(event))

    def _canvas_drag(self, event):
        if self.image is None or self._last_pt is None or not self._paint:
            return
        pt = self._tex_pixel(*self._canvas_pos(event))
        if pt:
            self._stroke(self._last_pt, pt)
            self._last_pt = pt

    def _canvas_up(self, _event):
        self._last_pt = None

    def _stroke(self, a, b):
        from PIL import ImageDraw
        layer = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        width = max(1, int(self.brush.get()))
        fill = (0, 0, 0, 0) if self._paint == "erase" else self.brush_rgba
        d.line([a, b], fill=fill, width=width)
        r = width / 2.0
        for pt in (a, b):
            d.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=fill)
        self.image = Image.alpha_composite(self.image, layer)
        self.dirty = True
        self.dirty_tag.configure(text="unsaved edits", text_color="#e0a33e")
        self._redraw_canvas()

    def _canvas_wheel(self, event):
        if self.image is None:
            return
        before = self._tex_pixel(*self._canvas_pos(event))
        self._zoom = max(0.05, min(24.0, self._zoom * (1.15 if event.delta > 0
                                                      else 1 / 1.15)))
        if before:
            ox, oy = self._origin()
            self._pan = (self._pan[0] + (before[0] * self._zoom - (event.x - ox)),
                         self._pan[1] + (before[1] * self._zoom - (event.y - oy)))
        self._redraw_canvas()

    def _pan_start(self, event, stop=False):
        if stop:
            return
        self._pan_anchor = (event.x, event.y, self._pan)

    def _canvas_pan(self, event):
        if not getattr(self, "_pan_anchor", None):
            return
        ax, ay, pan = self._pan_anchor
        self._pan = (pan[0] + event.x - ax, pan[1] + event.y - ay)
        self._redraw_canvas()

    def _redraw_canvas(self):
        self.canvas.delete("all")
        if self.image is None:
            self.canvas.create_text(
                max(self.canvas.winfo_width(), 1) // 2,
                max(self.canvas.winfo_height(), 1) // 2,
                text="open a bundle to edit its textures",
                fill="#6b7280")
            return
        w, h = self.image.size
        scale = self._zoom
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        view = self.image.resize((dw, dh), Image.NEAREST)
        # Held on the instance: a PhotoImage with no Python reference is
        # collected and the canvas goes blank.
        self._tex_photo = ImageTk.PhotoImage(view)
        self.canvas.create_image(*self._origin(), anchor="nw",
                                 image=self._tex_photo)

    def _revert(self):
        if self.original is None:
            return
        self.image = self.original.copy()
        self.dirty = False
        self.staged.pop(self.texture_id, None)
        self.dirty_tag.configure(text="")
        self._redraw_canvas()

    def _push_edit(self):
        """Hand the working image to the bundle (in memory, not on disk).

        Also kept in self.staged so Build can send it along: the build runs on
        a worker against its own copy of the bundle, so an edit that only lived
        in this bundle would silently not be in the result.
        """
        if self.bundle is None or self.texture_id is None or not self.dirty:
            return
        self.staged[self.texture_id] = self.image.copy()
        try:
            self.bundle.set_texture(self.texture_id, self.image)
        except Exception as exc:
            self.say("!! could not stage that texture: %s" % exc)
        self._refresh_model_texture(self.texture_id)
        self.dirty = False
        self.dirty_tag.configure(text="")
        if self.model is not None:
            self._render_model(force=True)

    def _refresh_model_texture(self, path_id):
        """Keep the 3D view showing what the user just drew.

        meshdata decodes the texture bytes once at open time, so without this
        the preview would keep showing the shipped pixels after a paint.
        """
        if self.model is None or path_id not in self.staged:
            return
        held = self.model.get("textures", {}).get(path_id)
        if not held:
            return
        import io
        buf = io.BytesIO()
        self.staged[path_id].save(buf, format="PNG")
        self.model["textures"][path_id] = (buf.getvalue(), held[1])

    # ------------------------------------------------------------------
    # recolour
    # ------------------------------------------------------------------
    def _set_preset(self, name):
        self.preset = name
        spec = A.PRESETS.get(name)
        for key, (slider, val) in self.sliders.items():
            v = spec[key] if spec else 0.0
            slider.set(v)
            val.configure(text="%+.3f" % v)
        if spec and spec.get("tint"):
            self.tint = tuple(spec["tint"])
            self.tint_amount.set(spec.get("tint_amount", 0.6))
            self.tint_button.configure(
                fg_color="#%02x%02x%02x" % self.tint[:3])
        else:
            self.tint = None
            self.tint_button.configure(fg_color="#5a5f6b", text="none")

    def _preset_moved(self, _value=None):
        for key, (slider, val) in self.sliders.items():
            v = slider.get()
            val.configure(text="%+.3f" % v)
        self.preset = "custom"

    def _tint_moved(self, _value=None):
        if self.tint is not None:
            self.preset = "custom"

    def _pick_tint(self):
        from tkinter import colorchooser
        rgb = colorchooser.askcolor(title="Tint colour")
        if rgb and rgb[0]:
            self.tint = tuple(rgb[0])
            self.tint_button.configure(fg_color="#%02x%02x%02x" % self.tint[:3])
            self.preset = "custom"

    def _recolour_args(self):
        spec = A.PRESETS.get(self.preset) or {}
        return {
            "dh": self.sliders["dh"][0].get(),
            "ks": self.sliders["ks"][0].get(),
            "kv": self.sliders["kv"][0].get(),
            "tint": self.tint,
            "tint_amount": self.tint_amount.get(),
        }

    def _apply_preset_here(self):
        if self.image is None:
            return
        self.image = A.recolour(self.image, **self._recolour_args())
        self.dirty = True
        self.dirty_tag.configure(text="unsaved edits", text_color="#e0a33e")
        self._redraw_canvas()

    # ------------------------------------------------------------------
    # 3D
    # ------------------------------------------------------------------
    def _reset_view(self):
        self.yaw, self.pitch = 32.0, -12.0
        self._render_model(force=True)

    def _orbit_start(self, event):
        self._orbit_from = (event.x, event.y)

    def _orbit_move(self, event):
        if not getattr(self, "_orbit_from", None):
            return
        dx = event.x - self._orbit_from[0]
        dy = event.y - self._orbit_from[1]
        self._orbit_from = (event.x, event.y)
        self.yaw = (self.yaw + dx * 0.7) % 360.0
        # Sign: dy is positive when the mouse moves DOWN, and a positive pitch
        # swings the model's front down and its top toward the camera, so adding
        # dy here makes the model follow the mouse. It used to subtract, which put
        # the model on the opposite side of the pointer -- dragging up sent it
        # down. Measured against a two-marker model: pitch -25..+25 moves the front
        # marker 79 rows, and the sign is the one that keeps the model under the
        # cursor.
        self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.7))
        self._render_model()

    def _orbit_wheel(self, event):
        self._zoom_model = max(0.4, min(3.0, getattr(
            self, "_zoom_model", 1.0) * (1.1 if event.delta > 0 else 1 / 1.1)))
        self._render_model(force=True)

    def _render_model(self, force=False):
        """Ask for a preview frame. `force` is accepted for call-site clarity.

        At most one frame is ever rasterised at a time. A drag or a scroll wheel
        fires far more events than the renderer can finish, and every request used
        to start its own thread, so ten events meant ten concurrent full-canvas
        rasterisations all fighting over the CPU and the preview chased the mouse
        seconds behind. Now a request arriving while a frame is in flight simply
        replaces the pending one, and _handle_render picks it up the moment the
        current frame lands. The newest camera always wins and nothing piles up.
        """
        if self.model is None or self.bundle is None:
            return
        w = max(self.model_canvas.winfo_width(), 120)
        h = max(self.model_canvas.winfo_height(), 120)
        self._full_size = max(w, h)
        self._render_req = (self._full_size, self.yaw, self.pitch,
                            getattr(self, "_zoom_model", 1.0))
        # Any request means the user is still moving. Keep the cheap draft size
        # coming and push the sharp one out until the mouse goes quiet.
        self._interacting = True
        self._cancel_settle()
        if getattr(self, "_render_pending", False):
            return
        self._start_render()

    def _cancel_settle(self):
        if getattr(self, "_settle_id", None) is not None:
            try:
                self.after_cancel(self._settle_id)
            except Exception:
                pass
            self._settle_id = None

    def _settle_render(self):
        """Interaction stopped: drop back to full resolution once."""
        self._settle_id = None
        self._interacting = False
        if self._render_req is not None or self._render_pending:
            return
        # Queued directly rather than via _render_model, which would set
        # _interacting back to True and make this very frame a draft one.
        w = max(self.model_canvas.winfo_width(), 120)
        h = max(self.model_canvas.winfo_height(), 120)
        self._full_size = max(w, h)
        self._render_req = (self._full_size, self.yaw, self.pitch,
                            getattr(self, "_zoom_model", 1.0))
        self._start_render()

    def _start_render(self):
        """Start the pending frame, if there is one and nothing else is running."""
        req = getattr(self, "_render_req", None)
        if not req:
            return
        self._render_req = None
        size, yaw, pitch, zoom = req
        if getattr(self, "_interacting", False):
            size = max(160, int(size * self.DRAFT_SCALE))
        self._render_pending = True
        self.submit(self._render_job, size, yaw, pitch, zoom)

    def _render_job(self, size, yaw, pitch, zoom):
        return render3d.render(self.model, width=size, height=size, yaw=yaw,
                               pitch=pitch, margin=0.08 / max(zoom, 0.2))

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------
    def _replace_toggled(self):
        if self.replace_check.get() and not config.allow_replace():
            self.replace_check.deselect()
            from tkinter import messagebox
            messagebox.showwarning(
                "Replace is off",
                "Replacing a genuine bundle needs assets.allow_replace = true in\n"
                "%s\n\nVariants are always allowed and are the safer option."
                % config.settings_path())
        self.build_button.configure(
            text="Replace base bundle" if self.replace_check.get()
            else "Build variant")

    def _build(self):
        if self.bundle is None:
            return
        self._push_edit()
        self._set_busy(True)
        self.say("building...")
        # The worker opens its own copy of the bundle, so the painted pixels
        # have to travel with the request; they are not on disk yet.
        override = [{"path_id": pid, "png": img.copy()}
                    for pid, img in sorted(self.staged.items())]
        self.submit(self._build_job, self.bundle_name,
                    self.variant_entry.get().strip(), self.preset,
                    bool(self.replace_check.get()), self._recolour_args(),
                    override)

    def _build_job(self, name, variant, preset, replace, tweak, override):
        if preset == "none":
            base, custom = None, None          # keep the shipped pixels
        elif preset == "custom":
            base, custom = "shiny", dict(tweak, tips=True)
        else:
            base, custom = preset, None
        return A.build_variant(name, variant, preset=base, custom=custom,
                               replace=replace, override=override,
                               progress=lambda *a: None)

    def _export_gltf(self):
        if self.bundle is None:
            return
        name = self.bundle_name
        self.say("exporting glTF for %s..." % name)
        self.submit(self._export_job, name)

    def _export_job(self, name):
        data = A.export_gltf(name, self.platform, with_textures=True)
        base, suffix = A.split_name(name)
        out = os.path.join(config.data_dir("exports"),
                           (base + ("_" + suffix if suffix else "")) + ".glb")
        with open(out, "wb") as fh:
            fh.write(data)
        return out, A.glb_stats(data), len(data)

    def _verify(self):
        adir = A.assets_dir(self.platform)
        self.say("verifying every bundle in %s ..." % adir)
        self.submit(self._verify_job, adir)

    def _verify_job(self, adir):
        from asset_tools import bundle_crypto as C
        return C.verify_all(adir, quiet=True, out=None)

    # ------------------------------------------------------------------
    # job results
    # ------------------------------------------------------------------
    def _finish(self, job, out):
        kind = job.fn.__name__ if hasattr(job.fn, "__name__") else ""
        if kind == "_open_job":
            self._handle_open(job, out)
        elif kind == "_render_job":
            self._handle_render(out)
        elif kind == "_build_job":
            self._handle_build(out)
        elif kind == "_export_job":
            self._handle_export(out)
        elif kind == "_verify_job":
            self._handle_verify(out)

    def _handle_open(self, job, out):
        if not isinstance(out, tuple) or len(out) != 2:
            return
        bundle, model = out
        name = job.args[0] if job.args else "bundle"
        self.bundle = bundle
        self.bundle_name = name
        self.model = model
        self.dirty = False
        self.staged = {}
        self._orig_png = {}
        self.image = None
        self.original = None
        self.texture_id = None
        self.dirty_tag.configure(text="")
        self.model_canvas.delete("all")
        self.canvas.delete("all")

        tex = sorted(bundle.textures(), key=lambda t: t.get("name") or "")
        labels = [(t.get("name") or str(t["path_id"])) for t in tex] or ["(none)"]
        self._tex_by_label = {(t.get("name") or str(t["path_id"])): t
                              for t in tex}
        self.texture_menu.configure(values=labels)
        self.texture_menu.set(labels[0])
        self._all = A.list_bundles(self.platform)     # variants may have appeared
        self.say("%s: %d textures%s"
                 % (name, len(tex), "" if model else ", no 3D skin"))
        if model:
            self.say("  %d bones, %d vertices"
                     % (model["bone_count"], model["vertex_count"]))
        if tex:
            self._pick_texture(labels[0])
        self._render_model(force=True)

    def _soften(self, image):
        """Stretch a draft frame back up to the full canvas footprint, softly.

        A drag renders at DRAFT_SCALE, and showing that bitmap as-is left a
        smaller square sitting in the middle of the canvas, which read as the
        preview window shrinking and resizing on every drag. Upscaling to the
        same footprint the full-resolution frame uses keeps the model a constant
        size and position; the only thing that changes is sharpness.
        Full-resolution frames pass through untouched.

        Bilinear, measured on a worst-case noise image: 2.7 ms and sharpness 19.7,
        against 8.8 ms / 28.1 for bicubic and 20.1 ms / 13.1 for bicubic plus a
        Gaussian. So bilinear is both cheaper and softer than bicubic here -- the
        resampling filter is already the blur, and adding a real one costs more
        than the render it is hiding.
        """
        full = getattr(self, "_full_size", 0)
        if not full or image.size[0] >= full:
            return image
        return image.resize((full, full), Image.Resampling.BILINEAR)

    def _handle_render(self, image):
        self._render_pending = False
        try:
            if image is None:
                return
            image = self._soften(image)
            self._model_photo = ImageTk.PhotoImage(image)
            self.model_canvas.delete("all")
            self.model_canvas.create_image(
                max(self.model_canvas.winfo_width(), 1) // 2,
                max(self.model_canvas.winfo_height(), 1) // 2,
                image=self._model_photo)
            if self.model is not None:
                self.model_info.configure(
                    text="%d parts, %d bones, %d vertices, nearest sampling"
                         % (len(self.model["parts"]), self.model["bone_count"],
                            self.model["vertex_count"]))
        finally:
            # Pick up whatever the mouse did while this frame was being drawn.
            # In the finally, so a failed frame cannot wedge the preview: if this
            # is skipped the pending flag stays False with a request still queued
            # and the canvas silently stops updating for the rest of the session.
            self._start_render()
            # Nothing queued and the mouse is quiet: schedule the sharp frame.
            # Skipped while another request is still waiting, because that
            # request will re-arm the timer anyway.
            if (self._settle_id is None and self._interacting
                    and self._render_req is None and not self._render_pending):
                self._settle_id = self.after(self.SETTLE_MS,
                                             self._settle_render)

    def _handle_build(self, result):
        self._set_busy(False)
        if not isinstance(result, dict):
            return
        if not result.get("ok"):
            # assetlib refuses builds (name taken, replace disabled, no such
            # texture) by returning ok=False, so this must be checked before
            # the success lines or a refusal reads as a successful build.
            self.say("!! build failed: %s" % result.get("error", "unknown error"))
            return
        self.say("built %s  (%s bytes)  checksum %#010x  key %s"
                 % (result.get("name"), result.get("size"),
                    result.get("checksum") or 0, result.get("key")))
        self.say("  textures: %s" % ", ".join(result.get("textures") or []))
        self.say("  registered in extra_digest.json: %s"
                 % bool(result.get("listed", True)))
        if result.get("backup"):
            self.say("  original backed up to %s" % result["backup"])
        self._load_list(self.bundle_name)

    def _handle_export(self, out):
        path, stats, size = out
        self.say("wrote %s (%.1f KB, %d meshes, %d skins, %d images)"
                 % (path, size / 1024.0, stats["meshes"], stats["skins"],
                    stats["images"]))

    def _handle_verify(self, out):
        ok, bad = out
        self.say("verified: %s" % ("all good" if ok else "PROBLEMS FOUND"))
        for name, why in (bad or []):
            self.say("  %s: %s" % (name, why))

    def _open_settings(self):
        path = config.settings_path()
        self.say("settings file: %s" % path)
        self.say("assets dir: %s" % config.describe(self.platform)["dir"])
        try:
            os.startfile(os.path.dirname(path))        # noqa: S606
        except Exception:
            pass


def launch(platform="android", name=None):
    """Start the editor. Returns a process exit code."""
    reason = A.available()
    if reason:
        print("the editor needs the asset engine: %s" % reason, file=sys.stderr)
        print("pip install UnityPy pycryptodome crcmod Pillow numpy "
              "customtkinter", file=sys.stderr)
        return 2
    app = Editor(platform=platform, name=name)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch(sys.argv[1] if len(sys.argv) > 1 else "android"))
