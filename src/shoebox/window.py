"""Main application window with adaptive navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, GLib, GObject

if TYPE_CHECKING:
    from .application import ShoeboxApplication


class ShoeboxWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'ShoeboxWindow'

    compact = GObject.Property(type=bool, default=False)

    def __init__(self, application: ShoeboxApplication):
        super().__init__(application=application)
        self.app = application
        self.set_title('Shoebox')

        s = application.settings
        self.set_default_size(s.get_int('window-width'), s.get_int('window-height'))
        if s.get_boolean('window-maximized'):
            self.maximize()
        self.connect('close-request', self._on_close_request)

        self._toast_overlay = Adw.ToastOverlay()
        self._nav = Adw.NavigationView()
        self._toast_overlay.set_child(self._nav)
        self.set_content(self._toast_overlay)

        # Adaptive: anything narrower than ~600sp is "compact" (phone-like).
        breakpoint_ = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse('max-width: 600sp')
        )
        breakpoint_.add_setter(self, 'compact', True)
        self.add_breakpoint(breakpoint_)

        # Suite-standard window action: any child widget can fire a
        # toast via widget.activate_action("win.toast", GLib.Variant("s", msg)).
        toast_action = Gio.SimpleAction.new('toast', GLib.VariantType.new('s'))
        toast_action.connect('activate',
            lambda _a, p: self._toast_overlay.add_toast(Adw.Toast.new(p.get_string())))
        self.add_action(toast_action)

        self._show_initial_page()

    # ----- navigation helpers -----

    @property
    def nav(self) -> Adw.NavigationView:
        return self._nav

    def push(self, page: Adw.NavigationPage) -> None:
        self._nav.push(page)

    def replace_root(self, page: Adw.NavigationPage) -> None:
        self._nav.replace([page])

    def toast(self, text: str, timeout: int = 3) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(text))

    # ----- initial page -----

    def _show_initial_page(self) -> None:
        if self.app.settings.get_boolean('setup-complete') and self.app.primary_account():
            self._open_gallery()
        else:
            self._open_setup()

    def _open_setup(self) -> None:
        from .ui.setup import SetupPage
        self.replace_root(SetupPage(self))

    def _open_gallery(self) -> None:
        from .ui.gallery import GalleryPage
        self.replace_root(GalleryPage(self))

    # ----- public API -----

    def setup_finished(self) -> None:
        self.app.settings.set_boolean('setup-complete', True)
        self.app.reset_backend()
        self._open_gallery()

    def request_sync(self) -> None:
        # Wired up by the gallery page when it owns a SyncManager.
        page = self._nav.get_visible_page()
        if hasattr(page, 'request_sync'):
            page.request_sync()
        else:
            self.toast('No active sync target')

    # ----- cleanup -----

    def _on_close_request(self, *_args) -> bool:
        s = self.app.settings
        s.set_int('window-width', self.get_width())
        s.set_int('window-height', self.get_height())
        s.set_boolean('window-maximized', self.is_maximized())
        # If the user has opted into close-to-background, hide and
        # hold so the periodic sync timer keeps running. Otherwise
        # let the default close-and-quit happen.
        if s.get_boolean('run-in-background') and self.app.primary_account() is not None:
            self.set_visible(False)
            self.app.hold_for_background()
            return True
        return False
