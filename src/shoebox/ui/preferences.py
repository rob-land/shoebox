"""Preferences dialog: account, sync conditions, sync directories."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, GLib, Gtk

from .. import secrets

if TYPE_CHECKING:
    from ..application import ShoeboxApplication


_NETWORK_PREFS = [
    ('any', 'Any connection'),
    ('wifi', 'Wi-Fi only'),
    ('unmetered', 'Unmetered connections'),
]


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = 'ShoeboxPreferencesDialog'

    def __init__(self, app: 'ShoeboxApplication'):
        super().__init__()
        self.app = app
        self.add(self._account_page())
        self.add(self._sync_page())

    # ----- account page -----

    def _account_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title='Account', icon_name='avatar-default-symbolic')

        group = Adw.PreferencesGroup(title='Signed in')
        page.add(group)

        account = self.app.primary_account()
        if account is not None:
            row = Adw.ActionRow()
            row.set_title(account.display_name or account.username)
            row.set_subtitle(f'{account.username} · {account.server_url}')
            sign_out = Gtk.Button(label='Sign out')
            sign_out.add_css_class('destructive-action')
            sign_out.set_valign(Gtk.Align.CENTER)
            sign_out.connect('clicked', lambda *_: self._sign_out(account))
            row.add_suffix(sign_out)
            group.add(row)
        else:
            row = Adw.ActionRow()
            row.set_title('Not signed in')
            group.add(row)

        return page

    def _sign_out(self, account) -> None:
        secrets.clear_token(account.id, account.backend)
        self.app.db.delete_account(account.id)
        self.app.settings.set_boolean('setup-complete', False)
        self.app.reset_backend()
        self.close()
        win = self.app.get_active_window()
        if win is not None and hasattr(win, '_open_setup'):
            win._open_setup()

    # ----- sync page -----

    def _sync_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title='Sync',
                                   icon_name='emblem-synchronizing-symbolic')

        conditions = Adw.PreferencesGroup(title='When to sync')
        page.add(conditions)

        s = self.app.settings

        auto_row = Adw.SwitchRow()
        auto_row.set_title('Sync automatically')
        auto_row.set_subtitle('Periodic background sync when conditions are met')
        s.bind('sync-auto', auto_row, 'active', Gio.SettingsBindFlags.DEFAULT)
        conditions.add(auto_row)

        network_row = Adw.ComboRow()
        network_row.set_title('Network')
        model = Gtk.StringList.new([label for _, label in _NETWORK_PREFS])
        network_row.set_model(model)
        current = s.get_string('sync-network')
        for i, (key, _label) in enumerate(_NETWORK_PREFS):
            if key == current:
                network_row.set_selected(i)
                break

        def on_network(row, _pspec):
            idx = row.get_selected()
            if 0 <= idx < len(_NETWORK_PREFS):
                s.set_string('sync-network', _NETWORK_PREFS[idx][0])

        network_row.connect('notify::selected', on_network)
        conditions.add(network_row)

        charging_row = Adw.SwitchRow()
        charging_row.set_title('Only while charging')
        charging_row.set_subtitle('Pause uploads when on battery')
        s.bind('sync-charging-only', charging_row, 'active',
               Gio.SettingsBindFlags.DEFAULT)
        conditions.add(charging_row)

        # ---- folders ----

        folders = Adw.PreferencesGroup(title='Folders')
        folders.set_description('Photos in these folders will be uploaded.')
        page.add(folders)
        self._folders_group = folders
        self._folder_rows: dict[str, Adw.ActionRow] = {}

        add_row = Adw.ButtonRow.new() if hasattr(Adw, 'ButtonRow') else None
        if add_row is not None:
            add_row.set_title('Add folder…')
            add_row.set_start_icon_name('list-add-symbolic')
            add_row.connect('activated', lambda *_: self._pick_folder())
            folders.add(add_row)
        else:
            btn = Gtk.Button(label='Add folder…')
            btn.set_halign(Gtk.Align.CENTER)
            btn.set_margin_top(6)
            btn.connect('clicked', lambda *_: self._pick_folder())
            folders.add(btn)

        self._reload_folders()
        return page

    # ----- folder management -----

    def _reload_folders(self) -> None:
        for path, row in list(self._folder_rows.items()):
            self._folders_group.remove(row)
        self._folder_rows.clear()

        account = self.app.primary_account()
        if account is None:
            return
        for path, _recursive in self.app.db.list_sync_dirs(account.id):
            self._add_folder_row(path)

    def _add_folder_row(self, path: str) -> None:
        if path in self._folder_rows:
            return
        row = Adw.ActionRow()
        row.set_title(Path(path).name or path)
        row.set_subtitle(path)
        remove = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        remove.add_css_class('flat')
        remove.set_valign(Gtk.Align.CENTER)
        remove.connect('clicked', lambda *_: self._remove_folder(path))
        row.add_suffix(remove)
        self._folders_group.add(row)
        self._folder_rows[path] = row

    def _remove_folder(self, path: str) -> None:
        account = self.app.primary_account()
        if account is None:
            return
        self.app.db.remove_sync_dir(account.id, path)
        row = self._folder_rows.pop(path, None)
        if row is not None:
            self._folders_group.remove(row)
        self._sync_setting_strv()

    def _sync_setting_strv(self) -> None:
        account = self.app.primary_account()
        if account is None:
            return
        paths = [p for p, _ in self.app.db.list_sync_dirs(account.id)]
        self.app.settings.set_strv('sync-directories', paths)

    def _pick_folder(self) -> None:
        account = self.app.primary_account()
        if account is None:
            return
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
            if folder is None:
                return
            path = folder.get_path()
            self.app.db.add_sync_dir(account.id, path)
            self._add_folder_row(path)
            self._sync_setting_strv()

        win = self.app.get_active_window()
        dialog.select_folder(win, None, on_picked)
