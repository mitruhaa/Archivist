import gi
gi.require_version("Poppler", "0.18")
import cairo

from gi.repository import Adw, Gdk, Gio, Gtk, Poppler, GLib

PAGE_GAP     = 20
PAGE_PAD     = 20
ZOOM_SCROLL  = 0.10   # zoom multiplier per scroll tick (≈ Evince's 10%)
ZOOM_MIN     = 0.10
ZOOM_MAX     = 5.00
CACHE_BEHIND = 2    # pages to keep rendered behind viewport
CACHE_AHEAD  = 2    # pages to pre-render ahead of viewport

# Discrete zoom levels used by the +/- buttons, mirroring Evince's preset list
ZOOM_LEVELS  = [0.25, 0.33, 0.50, 0.67, 0.75, 1.00, 1.25, 1.50, 2.00, 4.00]

@Gtk.Template(resource_path='/io/github/mitruhaa/Archivist/gtk/window.ui')
class ArchivistWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ArchivistWindow'

    open_file_button    = Gtk.Template.Child()
    open_welcome_button = Gtk.Template.Child()
    main_stack          = Gtk.Template.Child()
    scrolled_window     = Gtk.Template.Child()
    drawing_area        = Gtk.Template.Child()
    zoom_controls       = Gtk.Template.Child()
    zoom_out_button     = Gtk.Template.Child()
    zoom_in_button      = Gtk.Template.Child()
    zoom_label          = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.document        = None
        self.pages           = []   # [{page, width, height}, …] loaded once on open
        self.zoom            = 1.0
        self.base_scale      = 1.0
        self.scale           = 1.0
        self.last_width      = -1
        self.resize_tid      = None
        self.render_tid      = None
        self.page_y          = []   # precomputed top-y of each page at current scale
        self.content_width   = 0    # drawing area width from last reflow
        self.content_height  = 0    # drawing area height from last reflow
        self.cache           = {}   # {page_index: cairo.ImageSurface} rendered at base_scale
        self.cache_scale     = 1.0  # base_scale at which cache entries were rendered

        self.open_file_button.connect('clicked', self.open_dialog)
        self.open_welcome_button.connect('clicked', self.open_dialog)
        self.zoom_in_button.connect('clicked',  lambda *_: self.apply_zoom(self._next_zoom_level(+1)))
        self.zoom_out_button.connect('clicked', lambda *_: self.apply_zoom(self._next_zoom_level(-1)))

        self.drawing_area.set_draw_func(self.draw)

        hadj = self.scrolled_window.get_hadjustment()
        hadj.connect('notify::page-size', self.on_viewport_resized)

        vadj = self.scrolled_window.get_vadjustment()
        vadj.connect('notify::value', lambda *_: self.schedule_render())

        scroll_ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll_ctrl.connect('scroll', self.on_scroll)
        self.scrolled_window.add_controller(scroll_ctrl)

        self._cursor_x = 0.0
        self._cursor_y = 0.0
        motion_ctrl = Gtk.EventControllerMotion.new()
        motion_ctrl.connect('motion', lambda _c, x, y: self._track_cursor(x, y))
        self.scrolled_window.add_controller(motion_ctrl)

    # ── file dialog ───────────────────────────────────────────────────────────

    def open_dialog(self, *_):
        pdf_filter = Gtk.FileFilter()
        pdf_filter.set_name("PDF Files")
        pdf_filter.add_mime_type("application/pdf")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(pdf_filter)
        dialog = Gtk.FileDialog()
        dialog.set_title("Open PDF")
        dialog.set_filters(filters)
        dialog.set_default_filter(pdf_filter)
        dialog.open(self, None, self.on_file_chosen)

    def on_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except Exception:
            return
        try:
            self.document = Poppler.Document.new_from_file(file.get_uri(), None)
        except GLib.Error:
            return

        self.pages = []
        for i in range(self.document.get_n_pages()):
            page = self.document.get_page(i)
            w, h = page.get_size()
            self.pages.append({"page": page, "width": w, "height": h})

        self.zoom = 1.0
        self.cache.clear()
        self.page_y = []
        self.zoom_controls.set_visible(True)
        self.main_stack.set_visible_child_name("document")
        GLib.idle_add(self.deferred_layout)

    def deferred_layout(self):
        w = int(self.scrolled_window.get_hadjustment().get_page_size())
        if w > 0:
            self.last_width = -1
            self.update_layout(w)
        else:
            GLib.idle_add(self.deferred_layout)
        return GLib.SOURCE_REMOVE

    # ── zoom ──────────────────────────────────────────────────────────────────

    def _track_cursor(self, x, y):
        self._cursor_x = x
        self._cursor_y = y

    def _next_zoom_level(self, direction):
        """Return the next preset zoom level above (+1) or below (-1) the current zoom."""
        if direction > 0:
            for level in ZOOM_LEVELS:
                if level > self.zoom + 0.01:
                    return level
            return ZOOM_LEVELS[-1]
        else:
            for level in reversed(ZOOM_LEVELS):
                if level < self.zoom - 0.01:
                    return level
            return ZOOM_LEVELS[0]

    def _content_anchor_v(self, viewport_y):
        """Return (page_idx, frac_within_page) for the content point at viewport_y.

        Uses page positions so the anchor is exact regardless of fixed PAGE_GAP offsets.
        """
        if not self.page_y:
            return (0, 0.0)
        doc_y = self.scrolled_window.get_vadjustment().get_value() + viewport_y
        for i, py in enumerate(self.page_y):
            sh = self.pages[i]["height"] * self.scale
            if doc_y < py + sh:
                return (i, max(0.0, (doc_y - py) / sh) if sh > 0 else 0.0)
        return (len(self.page_y) - 1, 1.0)

    def apply_zoom(self, new_zoom, anchor_vx=None, anchor_vy=None):
        """Zoom to new_zoom, keeping the point at (anchor_vx, anchor_vy) fixed.

        Scroll is restored synchronously from the newly computed page_y so that
        no intermediate frame is drawn at the wrong position (no flicker).
        """
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(new_zoom, 4)))
        if abs(new_zoom - self.zoom) < 0.001:
            return

        vadj = self.scrolled_window.get_vadjustment()
        hadj = self.scrolled_window.get_hadjustment()

        if anchor_vx is None:
            anchor_vx = hadj.get_page_size() / 2
        if anchor_vy is None:
            anchor_vy = vadj.get_page_size() / 2

        # Page-accurate vertical anchor — immune to fixed PAGE_GAP distortion
        v_anchor = self._content_anchor_v(anchor_vy)
        # Proportional horizontal anchor (pages are horizontally centred, scaling is symmetric)
        old_upper_h = hadj.get_upper()
        frac_h = (hadj.get_value() + anchor_vx) / old_upper_h if old_upper_h > 0 else 0

        self.zoom  = new_zoom
        self.scale = self.base_scale * self.zoom
        self.reflow()  # updates self.page_y, self.content_height, self.content_width

        # Compute correct scroll directly from the freshly-computed page positions
        page_idx, frac = v_anchor
        page_idx = min(page_idx, len(self.page_y) - 1)
        ph = self.pages[page_idx]["height"]
        new_doc_y   = self.page_y[page_idx] + frac * ph * self.scale
        new_scroll_v = max(0.0, min(new_doc_y - anchor_vy,
                                    self.content_height - vadj.get_page_size()))
        new_scroll_h = max(0.0, min(frac_h * self.content_width - anchor_vx,
                                    self.content_width - hadj.get_page_size()))

        # Use configure() to update upper and value atomically so GTK cannot clamp
        # new_scroll_v against the stale upper bound before the allocation is processed.
        vadj.configure(new_scroll_v, vadj.get_lower(), self.content_height,
                       vadj.get_step_increment(), vadj.get_page_increment(), vadj.get_page_size())
        hadj.configure(new_scroll_h, hadj.get_lower(), self.content_width,
                       hadj.get_step_increment(), hadj.get_page_increment(), hadj.get_page_size())

    def on_scroll(self, ctrl, dx, dy):
        if ctrl.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.apply_zoom(self.zoom * (1.0 - dy * ZOOM_SCROLL),
                            anchor_vx=self._cursor_x, anchor_vy=self._cursor_y)
            return True
        return False

    # ── layout ────────────────────────────────────────────────────────────────

    def on_viewport_resized(self, *_):
        if not self.document:
            return
        if self.resize_tid is not None:
            GLib.source_remove(self.resize_tid)
        self.resize_tid = GLib.timeout_add(20, self.do_resize)

    def do_resize(self):
        self.resize_tid = None
        w = int(self.scrolled_window.get_hadjustment().get_page_size())
        if w > 0 and w != self.last_width:
            anchor = self.page_anchor()
            self.update_layout(w)
            GLib.idle_add(self.restore_anchor, anchor)
        return GLib.SOURCE_REMOVE

    def page_anchor(self):
        """Return (page_idx, frac_within_page) for the page at the viewport top."""
        if not self.page_y:
            return (0, 0.0)
        top = self.scrolled_window.get_vadjustment().get_value()
        for i, py in enumerate(self.page_y):
            sh = self.pages[i]["height"] * self.scale
            if py + sh > top:
                return (i, (top - py) / sh if sh > 0 else 0.0)
        return (len(self.page_y) - 1, 0.0)

    def restore_anchor(self, anchor):
        page_idx, frac = anchor
        page_idx = min(page_idx, len(self.page_y) - 1)
        ph = self.pages[page_idx]["height"]
        target = self.page_y[page_idx] + frac * ph * self.scale
        vadj = self.scrolled_window.get_vadjustment()
        vadj.set_value(max(0.0, min(target, vadj.get_upper() - vadj.get_page_size())))
        return GLib.SOURCE_REMOVE

    def update_layout(self, viewport_width):
        if not self.document or viewport_width <= 0:
            return
        self.last_width = viewport_width
        max_pw = max(p["width"] for p in self.pages)
        self.base_scale = (viewport_width - 2 * PAGE_PAD) / max_pw
        self.scale       = self.base_scale * self.zoom
        self.reflow()

    def reflow(self):
        if not self.document:
            return
        max_pw = max(p["width"] for p in self.pages)
        vw     = self.last_width if self.last_width > 0 else self.scrolled_window.get_width()

        self.page_y = []
        y = PAGE_GAP
        for p in self.pages:
            self.page_y.append(y)
            y += p["height"] * self.scale + PAGE_GAP

        self.content_width  = max(vw, int(max_pw * self.scale + 2 * PAGE_PAD))
        self.content_height = int(y)
        self.drawing_area.set_size_request(self.content_width, self.content_height)
        self.zoom_label.set_label(f"{round(self.zoom * 100)}%")
        self.schedule_render()
        self.drawing_area.queue_draw()

    # ── page cache ────────────────────────────────────────────────────────────

    def needed_pages(self):
        """Indices of pages that should be in the cache."""
        n = len(self.pages)
        if not self.page_y:
            return list(range(min(CACHE_AHEAD + CACHE_BEHIND, n)))

        vadj = self.scrolled_window.get_vadjustment()
        top  = vadj.get_value()
        bot  = top + vadj.get_page_size()

        first_vis = last_vis = None
        for i, py in enumerate(self.page_y):
            if py + self.pages[i]["height"] * self.scale >= top and py <= bot:
                if first_vis is None:
                    first_vis = i
                last_vis = i
            elif py > bot:
                break

        if first_vis is None:
            first_vis = last_vis = 0

        return list(range(
            max(0, first_vis - CACHE_BEHIND),
            min(n, last_vis  + CACHE_AHEAD + 1)
        ))

    def schedule_render(self):
        if self.render_tid is not None:
            GLib.source_remove(self.render_tid)
        self.render_tid = GLib.idle_add(self.render_next)

    def render_next(self):
        self.render_tid = None
        if not self.document:
            return GLib.SOURCE_REMOVE

        if abs(self.base_scale - self.cache_scale) > 0.001:
            self.cache.clear()
            self.cache_scale = self.base_scale

        needed = self.needed_pages()
        self.evict(set(needed))

        to_render = [i for i in needed if i not in self.cache]
        if not to_render:
            return GLib.SOURCE_REMOVE

        self.render_page(to_render[0])
        print(f"cache: {len(self.cache)}/{len(needed)} pages")
        self.drawing_area.queue_draw()

        if len(to_render) > 1:
            self.render_tid = GLib.idle_add(self.render_next)

        return GLib.SOURCE_REMOVE

    def render_page(self, i):
        p      = self.pages[i]
        sw, sh = max(1, int(p["width"] * self.base_scale)), max(1, int(p["height"] * self.base_scale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        ctx     = cairo.Context(surface)
        ctx.scale(self.base_scale, self.base_scale)
        p["page"].render(ctx)
        self.cache[i] = surface
        self.cache_scale = self.base_scale

    def evict(self, keep):
        for k in list(self.cache):
            if k not in keep:
                del self.cache[k]

    # ── drawing ───────────────────────────────────────────────────────────────

    def draw(self, area, cr, width, height):
        if not self.document:
            return

        cr.set_source_rgb(0.18, 0.18, 0.18)
        cr.paint()

        clip = cr.clip_extents()

        y = PAGE_GAP
        for i, p in enumerate(self.pages):
            sw, sh = p["width"] * self.scale, p["height"] * self.scale
            x      = (width - sw) / 2

            if y + sh < clip[1]:
                y += sh + PAGE_GAP
                continue
            if y > clip[3]:
                break

            surface = self.cache.get(i)
            if surface is not None:
                cr.save()
                cr.translate(x, y)
                cr.scale(self.zoom, self.zoom)
                cr.set_source_surface(surface, 0, 0)
                cr.paint()
                cr.restore()
            else:
                cr.set_source_rgb(1, 1, 1)
                cr.rectangle(x, y, sw, sh)
                cr.fill()

            y += sh + PAGE_GAP
