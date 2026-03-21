from gi.repository import Adw, Gtk

@Gtk.Template(resource_path='/io/github/mitruhaa/Archivist/gtk/window.ui')
class ArchivistWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ArchivistWindow'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
