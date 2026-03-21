from gi.repository import Gio, Adw
from .window import ArchivistWindow

class ArchivistApplication(Adw.Application):
    __gtype_name__ = 'ArchivistApplication'

    def __init__(self):
        super().__init__(
            application_id='io.github.mitruhaa.Archivist',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )

        self.set_resource_base_path('/io/github/mitruhaa/Archivist')
        self.create_action('quit', lambda *_: self.quit(), ['<primary>q'])

    def do_activate(self):
        win = self.get_active_window()

        if not win:
            win = ArchivistWindow(application=self)

        win.present()

    def create_action(self, name, callback, shortcuts=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect('activate', callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f'app.{name}', shortcuts)
