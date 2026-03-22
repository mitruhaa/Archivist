import gi
gi.require_version("Poppler", "0.18")
import cairo

from gi.repository import Adw, Gdk, Gio, Gtk, Poppler, GLib

PAGE_GAP     = 20
PAGE_PAD     = 20
ZOOM_STEP    = 0.10
ZOOM_SCROLL  = 0.05
ZOOM_MIN     = 0.10
ZOOM_MAX     = 5.00
CACHE_BEHIND = 3    # pages to keep rendered behind viewport
CACHE_AHEAD  = 7    # pages to pre-render ahead of viewport

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

        self.document     = None
        self.zoom         = 1.0
        self.base_scale  = 1.0
        self.scale        = 1.0
        self.last_width  = -1
        self.resize_tid  = None
        self.render_tid  = None
        self.page_y      = []   # precomputed top-y of each page at current scale
        self.cache       = {}   # {page_index: cairo.ImageSurface} rendered at base_scale
        self.cache_scale = 1.0  # base_scale at which cache entries were rendered

        self.open_file_button.connect('clicked', self.open_dialog)
        self.open_welcome_button.connect('clicked', self.open_dialog)
        self.zoom_in_button.connect('clicked',  lambda *_: self.apply_zoom(self.zoom + ZOOM_STEP))
        self.zoom_out_button.connect('clicked', lambda *_: self.apply_zoom(self.zoom - ZOOM_STEP))

        self.drawing_area.set_draw_func(self.draw)

        hadj = self.scrolled_window.get_hadjustment()
        hadj.connect('notify::page-size', self.on_viewport_resized)

        vadj = self.scrolled_window.get_vadjustment()
        vadj.connect('notify::value', lambda *_: self.schedule_render())

        scroll_ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll_ctrl.connect('scroll', self.on_scroll)
        self.scrolled_window.add_controller(scroll_ctrl)

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

    def apply_zoom(self, new_zoom):
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, round(new_zoom, 4)))
        if abs(new_zoom - self.zoom) < 0.001:
            return

        vadj  = self.scrolled_window.get_vadjustment()
        upper = vadj.get_upper()
        frac  = (vadj.get_value() + vadj.get_page_size() / 2) / upper if upper > 0 else 0

        self.zoom  = new_zoom
        self.scale = self.base_scale * self.zoom
        self.reflow()
        GLib.idle_add(self.restore_scroll, frac)

    def restore_scroll(self, frac):
        vadj   = self.scrolled_window.get_vadjustment()
        upper  = vadj.get_upper()
        target = frac * upper - vadj.get_page_size() / 2
        vadj.set_value(max(0.0, min(target, upper - vadj.get_page_size())))
        return GLib.SOURCE_REMOVE

    def on_scroll(self, ctrl, dx, dy):
        if ctrl.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            self.apply_zoom(self.zoom * (1.0 - dy * ZOOM_SCROLL))
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
            _, ph = self.document.get_page(i).get_size()
            sh = ph * self.scale
            if py + sh > top:
                return (i, (top - py) / sh if sh > 0 else 0.0)
        return (len(self.page_y) - 1, 0.0)

    def restore_anchor(self, anchor):
        page_idx, frac = anchor
        page_idx = min(page_idx, len(self.page_y) - 1)
        _, ph = self.document.get_page(page_idx).get_size()
        target = self.page_y[page_idx] + frac * ph * self.scale
        vadj = self.scrolled_window.get_vadjustment()
        vadj.set_value(max(0.0, min(target, vadj.get_upper() - vadj.get_page_size())))
        return GLib.SOURCE_REMOVE

    def update_layout(self, viewport_width):
        if not self.document or viewport_width <= 0:
            return
        self.last_width = viewport_width
        max_pw = max(
            self.document.get_page(i).get_size()[0]
            for i in range(self.document.get_n_pages())
        )
        self.base_scale = (viewport_width - 2 * PAGE_PAD) / max_pw
        self.scale       = self.base_scale * self.zoom
        self.reflow()

    def reflow(self):
        if not self.document:
            return
        n      = self.document.get_n_pages()
        max_pw = max(self.document.get_page(i).get_size()[0] for i in range(n))
        vw     = self.last_width if self.last_width > 0 else self.scrolled_window.get_width()

        self.page_y = []
        y = PAGE_GAP
        for i in range(n):
            self.page_y.append(y)
            _, ph = self.document.get_page(i).get_size()
            y += ph * self.scale + PAGE_GAP

        content_w = max(vw, int(max_pw * self.scale + 2 * PAGE_PAD))
        self.drawing_area.set_size_request(content_w, int(y))
        self.zoom_label.set_label(f"{round(self.zoom * 100)}%")
        self.schedule_render()
        self.drawing_area.queue_draw()

    # ── page cache ────────────────────────────────────────────────────────────

    def needed_pages(self):
        """Indices of pages that should be in the cache."""
        n = self.document.get_n_pages()
        if not self.page_y:
            return list(range(min(CACHE_AHEAD + CACHE_BEHIND, n)))

        vadj = self.scrolled_window.get_vadjustment()
        top  = vadj.get_value()
        bot  = top + vadj.get_page_size()

        first_vis = last_vis = None
        for i, py in enumerate(self.page_y):
            _, ph = self.document.get_page(i).get_size()
            if py + ph * self.scale >= top and py <= bot:
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
        page   = self.document.get_page(i)
        pw, ph = page.get_size()
        sw, sh = max(1, int(pw * self.base_scale)), max(1, int(ph * self.base_scale))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        ctx     = cairo.Context(surface)
        ctx.scale(self.base_scale, self.base_scale)
        page.render(ctx)
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
        for i in range(self.document.get_n_pages()):
            page   = self.document.get_page(i)
            pw, ph = page.get_size()
            sw, sh = pw * self.scale, ph * self.scale
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
