"""First-run setup wizard.

Each step is an Adw.NavigationPage pushed onto the main window's
NavigationView, so the user gets back-button navigation for free.
The four pages (welcome / server / login / dirs) each carry their
own Blueprint template; they share a `_Wizard` state object passed
through their constructors.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, Gtk

from .. import secrets
from ..backends import BACKENDS, BackendError
from ..database import Account
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


class _Wizard:
    """Holds state across wizard pages."""

    def __init__(self, window: 'ShoeboxWindow'):
        self.window = window
        self.backend_name: str = next(iter(BACKENDS))
        self.server_url: str = ''
        self.account: Optional[Account] = None
        self.sync_dirs: list[str] = []


# ---- step 1: welcome ---------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/setup-welcome.ui')
class _SetupWelcome(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxSetupWelcome'

    backend_row: Adw.ComboRow = Gtk.Template.Child()

    def __init__(self, wizard: _Wizard):
        super().__init__()
        self.wizard = wizard

        self._backend_keys = list(BACKENDS.keys())
        model = Gtk.StringList.new([cls.display_name for cls in BACKENDS.values()])
        self.backend_row.set_model(model)
        self.backend_row.connect('notify::selected', self._on_backend_selected)

    def _on_backend_selected(self, row: Adw.ComboRow, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._backend_keys):
            self.wizard.backend_name = self._backend_keys[idx]

    @Gtk.Template.Callback()
    def _on_continue(self, *_args) -> None:
        self.wizard.window.push(_SetupServer(self.wizard))


# ---- step 2: server URL ------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/setup-server.ui')
class _SetupServer(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxSetupServer'

    heading:     Gtk.Label    = Gtk.Template.Child()
    url_row:     Adw.EntryRow = Gtk.Template.Child()
    error_label: Gtk.Label    = Gtk.Template.Child()

    def __init__(self, wizard: _Wizard):
        super().__init__()
        self.wizard = wizard

        backend_cls = BACKENDS[wizard.backend_name]
        self.heading.set_label(f'Enter your {backend_cls.display_name} server')
        self.url_row.set_text(wizard.server_url or 'https://')

    @Gtk.Template.Callback()
    def _on_continue(self, *_args) -> None:
        url = self.url_row.get_text().strip().rstrip('/')
        if not url.startswith(('http://', 'https://')):
            self.error_label.set_text('URL must start with http:// or https://')
            self.error_label.set_visible(True)
            return
        self.wizard.server_url = url
        self.wizard.window.push(_SetupLogin(self.wizard))


# ---- step 3: login -----------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/setup-login.ui')
class _SetupLogin(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxSetupLogin'

    email_row:        Adw.EntryRow        = Gtk.Template.Child()
    pw_row:           Adw.PasswordEntryRow = Gtk.Template.Child()
    error_label:      Gtk.Label           = Gtk.Template.Child()
    spinner:          Adw.Spinner         = Gtk.Template.Child()
    sign_in_button:   Gtk.Button          = Gtk.Template.Child()

    def __init__(self, wizard: _Wizard):
        super().__init__()
        self.wizard = wizard

    @Gtk.Template.Callback()
    def _on_sign_in(self, *_args) -> None:
        email = self.email_row.get_text().strip()
        password = self.pw_row.get_text()
        if not email or not password:
            self.error_label.set_text('Email and password are required')
            self.error_label.set_visible(True)
            return

        self.error_label.set_visible(False)
        self.sign_in_button.set_sensitive(False)
        self.spinner.set_visible(True)

        backend_cls = BACKENDS[self.wizard.backend_name]
        backend = backend_cls(self.wizard.server_url)

        def do_login() -> tuple[str, object]:
            token, user = backend.login(email, password)
            return token, user

        def on_done(result):
            self.spinner.set_visible(False)
            self.sign_in_button.set_sensitive(True)
            token, user = result
            account = self.wizard.window.app.db.add_account(
                backend=self.wizard.backend_name,
                server_url=self.wizard.server_url,
                username=user.username,
                user_id=user.user_id,
                display_name=user.display_name,
            )
            secrets.store_token(account.id, account.backend, token)
            self.wizard.account = account
            self.wizard.window.push(_SetupDirs(self.wizard))

        def on_error(exc):
            self.spinner.set_visible(False)
            self.sign_in_button.set_sensitive(True)
            msg = exc.args[0] if isinstance(exc, BackendError) and exc.args else str(exc)
            self.error_label.set_text(f'Login failed: {msg}')
            self.error_label.set_visible(True)

        run_async(do_login, on_done=on_done, on_error=on_error)


# ---- step 4: pick sync directories ------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/setup-dirs.ui')
class _SetupDirs(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxSetupDirs'

    folders_group: Adw.PreferencesGroup = Gtk.Template.Child()

    def __init__(self, wizard: _Wizard):
        super().__init__()
        self.wizard = wizard
        self._rows: dict[str, Adw.ActionRow] = {}

    def _add_row(self, path: str) -> None:
        if path in self._rows:
            return
        self.wizard.sync_dirs.append(path)
        row = Adw.ActionRow()
        row.set_title(Path(path).name or path)
        row.set_subtitle(path)
        remove = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        remove.add_css_class('flat')
        remove.set_valign(Gtk.Align.CENTER)
        remove.connect('clicked', lambda *_: self._remove_row(path))
        row.add_suffix(remove)
        self.folders_group.add(row)
        self._rows[path] = row

    def _remove_row(self, path: str) -> None:
        self.wizard.sync_dirs.remove(path)
        row = self._rows.pop(path, None)
        if row is not None:
            self.folders_group.remove(row)

    @Gtk.Template.Callback()
    def _on_add_folder(self, *_args) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title('Choose folder to sync')
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures:
            dialog.set_initial_folder(Gio.File.new_for_path(pictures))

        def on_picked(d, res):
            try:
                folder = d.select_folder_finish(res)
            except GLib.Error:
                return
            if folder:
                self._add_row(folder.get_path())

        dialog.select_folder(self.wizard.window, None, on_picked)

    @Gtk.Template.Callback()
    def _on_finish(self, *_args) -> None:
        if self.wizard.account is not None:
            for path in self.wizard.sync_dirs:
                self.wizard.window.app.db.add_sync_dir(self.wizard.account.id, path)
            self.wizard.window.app.settings.set_strv(
                'sync-directories', self.wizard.sync_dirs,
            )
        self.wizard.window.setup_finished()


# ---- public entry point ------------------------------------------------------


def SetupPage(window: 'ShoeboxWindow') -> Adw.NavigationPage:
    """Backward-compatible factory returning the welcome page."""
    return _SetupWelcome(_Wizard(window))
