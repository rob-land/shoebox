"""Shoebox application: lifecycle, actions, shared services."""

from __future__ import annotations

from typing import Optional


from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import secrets, settings
from .backends import BACKENDS, Backend, get as get_backend_cls
from .database import Account, Database
from .window import ShoeboxWindow


class ShoeboxApplication(Adw.Application):
    def __init__(self, version: str = '0.0.0'):
        super().__init__(
            application_id='land.rob.shoebox',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.version = version
        self.settings = settings.get()
        self.db = Database()
        self._backend: Optional[Backend] = None
        self._account: Optional[Account] = None

    # ----- lifecycle -----

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        css = Gtk.CssProvider()
        try:
            css.load_from_resource('/land/rob/shoebox/ui/style.css')
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )
        except GLib.Error:
            pass  # css resource missing in dev tree is non-fatal

        self._install_actions()

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            win = ShoeboxWindow(application=self)
        win.present()

    def do_shutdown(self) -> None:
        try:
            self.db.close()
        finally:
            Adw.Application.do_shutdown(self)

    # ----- actions -----

    def _install_actions(self) -> None:
        for name, cb in (
            ('quit', lambda *_: self.quit()),
            ('about', lambda *_: self._show_about()),
            ('preferences', lambda *_: self._show_preferences()),
            ('sync-now', lambda *_: self._trigger_sync()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', cb)
            self.add_action(action)

        self.set_accels_for_action('app.quit', ['<Primary>q'])
        self.set_accels_for_action('app.preferences', ['<Primary>comma'])

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name='Shoebox',
            application_icon='land.rob.shoebox',
            developer_name='Shoebox contributors',
            version=self.version,
            website='https://codeberg.org/robland/shoebox',
            issue_url='https://codeberg.org/robland/shoebox/issues',
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self.get_active_window())

    def _show_preferences(self) -> None:
        from .ui.preferences import PreferencesDialog
        win = self.get_active_window()
        PreferencesDialog(self).present(win)

    def _trigger_sync(self) -> None:
        win = self.get_active_window()
        if isinstance(win, ShoeboxWindow):
            win.request_sync()

    # ----- backend access -----

    def primary_account(self) -> Optional[Account]:
        if self._account is None:
            accounts = self.db.list_accounts()
            self._account = accounts[0] if accounts else None
        return self._account

    def backend_for(self, account: Account) -> Backend:
        token = secrets.lookup_token(account.id, account.backend)
        cls = get_backend_cls(account.backend)
        return cls(account.server_url, token=token)

    def primary_backend(self) -> Optional[Backend]:
        if self._backend is None:
            account = self.primary_account()
            if account is None:
                return None
            self._backend = self.backend_for(account)
        return self._backend

    def reset_backend(self) -> None:
        self._backend = None
        self._account = None

    @staticmethod
    def available_backends() -> dict[str, type[Backend]]:
        return dict(BACKENDS)
