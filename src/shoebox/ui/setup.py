"""First-run setup wizard.

Each step is an Adw.NavigationPage pushed onto the main window's NavigationView,
so the user gets back-button navigation for free.
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


# ---- shared layout helpers ----------------------------------------------------

def _make_page(title: str, child: Gtk.Widget,
               *, can_pop: bool = True) -> Adw.NavigationPage:
    page = Adw.NavigationPage.new(child, title)
    page.set_can_pop(can_pop)
    return page


def _toolbar_view(content: Gtk.Widget, header_title: str) -> Adw.ToolbarView:
    tv = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle.new(header_title, ''))
    tv.add_top_bar(header)
    tv.set_content(content)
    return tv


def _scrolled(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow()
    sw.set_hexpand(True)
    sw.set_vexpand(True)
    sw.set_child(child)
    return sw


# ---- entry page (also re-used as "settings → reset" target) ------------------

def SetupPage(window: 'ShoeboxWindow') -> Adw.NavigationPage:
    wizard = _Wizard(window)

    clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_margin_top(36)
    box.set_margin_bottom(36)
    box.set_margin_start(18)
    box.set_margin_end(18)

    icon = Gtk.Image.new_from_icon_name('land.rob.Shoebox')
    icon.set_pixel_size(96)
    box.append(icon)

    title = Gtk.Label(label='Welcome to Shoebox')
    title.add_css_class('title-1')
    title.set_wrap(True)
    title.set_justify(Gtk.Justification.CENTER)
    box.append(title)

    subtitle = Gtk.Label(
        label='Connect to a self-hosted photo service, then choose any local '
              'folders you would like to back up.'
    )
    subtitle.set_wrap(True)
    subtitle.set_justify(Gtk.Justification.CENTER)
    subtitle.add_css_class('dim-label')
    box.append(subtitle)

    backend_group = Adw.PreferencesGroup(title='Service')
    backend_row = Adw.ComboRow()
    backend_row.set_title('Backend')
    model = Gtk.StringList.new([cls.display_name for cls in BACKENDS.values()])
    backend_row.set_model(model)
    backend_keys = list(BACKENDS.keys())

    def on_backend_selected(row: Adw.ComboRow, _pspec):
        idx = row.get_selected()
        if 0 <= idx < len(backend_keys):
            wizard.backend_name = backend_keys[idx]

    backend_row.connect('notify::selected', on_backend_selected)
    backend_group.add(backend_row)
    box.append(backend_group)

    continue_btn = Gtk.Button(label='Continue')
    continue_btn.add_css_class('suggested-action')
    continue_btn.add_css_class('pill')
    continue_btn.set_halign(Gtk.Align.CENTER)
    continue_btn.connect('clicked', lambda *_: window.push(_server_page(wizard)))
    box.append(continue_btn)

    clamp.set_child(box)
    return _make_page('Setup', _toolbar_view(_scrolled(clamp), 'Welcome'),
                      can_pop=False)


# ---- step 2: server URL ------------------------------------------------------

def _server_page(wizard: _Wizard) -> Adw.NavigationPage:
    clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_margin_top(24)
    box.set_margin_bottom(24)
    box.set_margin_start(18)
    box.set_margin_end(18)

    backend_cls = BACKENDS[wizard.backend_name]
    label = Gtk.Label(label=f'Enter your {backend_cls.display_name} server')
    label.add_css_class('title-2')
    label.set_wrap(True)
    box.append(label)

    group = Adw.PreferencesGroup()
    entry = Adw.EntryRow()
    entry.set_title('Server URL')
    entry.set_text(wizard.server_url or 'https://')
    entry.set_input_purpose(Gtk.InputPurpose.URL)
    group.add(entry)
    box.append(group)

    error = Gtk.Label(label='')
    error.add_css_class('error')
    error.set_wrap(True)
    error.set_visible(False)
    box.append(error)

    btn = Gtk.Button(label='Continue')
    btn.add_css_class('suggested-action')
    btn.add_css_class('pill')
    btn.set_halign(Gtk.Align.CENTER)

    def on_continue(*_):
        url = entry.get_text().strip().rstrip('/')
        if not url.startswith(('http://', 'https://')):
            error.set_text('URL must start with http:// or https://')
            error.set_visible(True)
            return
        wizard.server_url = url
        wizard.window.push(_login_page(wizard))

    btn.connect('clicked', on_continue)
    entry.connect('entry-activated', on_continue)
    box.append(btn)

    clamp.set_child(box)
    return _make_page('Server', _toolbar_view(_scrolled(clamp), 'Server'))


# ---- step 3: login -----------------------------------------------------------

def _login_page(wizard: _Wizard) -> Adw.NavigationPage:
    clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_margin_top(24)
    box.set_margin_bottom(24)
    box.set_margin_start(18)
    box.set_margin_end(18)

    title = Gtk.Label(label='Sign in')
    title.add_css_class('title-2')
    box.append(title)

    group = Adw.PreferencesGroup()
    email_row = Adw.EntryRow()
    email_row.set_title('Email')
    email_row.set_input_purpose(Gtk.InputPurpose.EMAIL)
    group.add(email_row)

    pw_row = Adw.PasswordEntryRow()
    pw_row.set_title('Password')
    group.add(pw_row)
    box.append(group)

    error = Gtk.Label(label='')
    error.add_css_class('error')
    error.set_wrap(True)
    error.set_visible(False)
    box.append(error)

    spinner = Adw.Spinner()
    spinner.set_visible(False)
    box.append(spinner)

    btn = Gtk.Button(label='Sign in')
    btn.add_css_class('suggested-action')
    btn.add_css_class('pill')
    btn.set_halign(Gtk.Align.CENTER)

    def attempt_login(*_):
        email = email_row.get_text().strip()
        password = pw_row.get_text()
        if not email or not password:
            error.set_text('Email and password are required')
            error.set_visible(True)
            return

        error.set_visible(False)
        btn.set_sensitive(False)
        spinner.set_visible(True)

        backend_cls = BACKENDS[wizard.backend_name]
        backend = backend_cls(wizard.server_url)

        def do_login() -> tuple[str, object]:
            token, user = backend.login(email, password)
            return token, user

        def on_done(result):
            spinner.set_visible(False)
            btn.set_sensitive(True)
            token, user = result
            account = wizard.window.app.db.add_account(
                backend=wizard.backend_name,
                server_url=wizard.server_url,
                username=user.username,
                user_id=user.user_id,
                display_name=user.display_name,
            )
            secrets.store_token(account.id, account.backend, token)
            wizard.account = account
            wizard.window.push(_dirs_page(wizard))

        def on_error(exc):
            spinner.set_visible(False)
            btn.set_sensitive(True)
            msg = exc.args[0] if isinstance(exc, BackendError) and exc.args else str(exc)
            error.set_text(f'Login failed: {msg}')
            error.set_visible(True)

        run_async(do_login, on_done=on_done, on_error=on_error)

    btn.connect('clicked', attempt_login)
    pw_row.connect('entry-activated', attempt_login)
    box.append(btn)

    clamp.set_child(box)
    return _make_page('Sign in', _toolbar_view(_scrolled(clamp), 'Sign in'))


# ---- step 4: pick sync directories ------------------------------------------

def _dirs_page(wizard: _Wizard) -> Adw.NavigationPage:
    clamp = Adw.Clamp(maximum_size=620, tightening_threshold=480)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_margin_top(24)
    box.set_margin_bottom(24)
    box.set_margin_start(18)
    box.set_margin_end(18)

    title = Gtk.Label(label='Folders to sync')
    title.add_css_class('title-2')
    box.append(title)

    subtitle = Gtk.Label(
        label='Photos in these folders will be uploaded. You can change this '
              'later in Preferences. Skip to finish setup without syncing.'
    )
    subtitle.set_wrap(True)
    subtitle.set_justify(Gtk.Justification.CENTER)
    subtitle.add_css_class('dim-label')
    box.append(subtitle)

    group = Adw.PreferencesGroup()
    box.append(group)

    rows: dict[str, Adw.ActionRow] = {}

    def add_row(path: str) -> None:
        if path in rows:
            return
        wizard.sync_dirs.append(path)
        row = Adw.ActionRow()
        row.set_title(Path(path).name or path)
        row.set_subtitle(path)
        remove = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        remove.add_css_class('flat')
        remove.set_valign(Gtk.Align.CENTER)
        def on_remove(_b):
            wizard.sync_dirs.remove(path)
            group.remove(row)
            del rows[path]
        remove.connect('clicked', on_remove)
        row.add_suffix(remove)
        group.add(row)
        rows[path] = row

    add_btn = Gtk.Button(label='Add folder…')
    add_btn.set_halign(Gtk.Align.CENTER)

    def pick_folder(*_):
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
                add_row(folder.get_path())

        dialog.select_folder(wizard.window, None, on_picked)

    add_btn.connect('clicked', pick_folder)
    box.append(add_btn)

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    actions.set_halign(Gtk.Align.CENTER)
    actions.set_margin_top(12)

    skip = Gtk.Button(label='Skip for now')
    skip.connect('clicked', lambda *_: _finish(wizard))
    actions.append(skip)

    finish = Gtk.Button(label='Finish setup')
    finish.add_css_class('suggested-action')
    finish.add_css_class('pill')
    finish.connect('clicked', lambda *_: _finish(wizard))
    actions.append(finish)
    box.append(actions)

    clamp.set_child(box)
    return _make_page('Folders', _toolbar_view(_scrolled(clamp), 'Folders'))


def _finish(wizard: _Wizard) -> None:
    if wizard.account is not None:
        for path in wizard.sync_dirs:
            wizard.window.app.db.add_sync_dir(wizard.account.id, path)
        wizard.window.app.settings.set_strv('sync-directories', wizard.sync_dirs)
    wizard.window.setup_finished()
