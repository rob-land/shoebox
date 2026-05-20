"""Preferences dialog: account, sync conditions, sync directories."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, GLib, Gtk

from .. import background_portal, secrets

_APP_ID = 'land.rob.shoebox'
_APP_NAME = 'Shoebox'

if TYPE_CHECKING:
    from ..application import ShoeboxApplication


_NETWORK_PREFS = [
    ('any', 'Any connection'),
    ('wifi', 'Wi-Fi only'),
    ('unmetered', 'Unmetered connections'),
]


@Gtk.Template(resource_path='/land/rob/shoebox/ui/preferences.ui')
class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = 'ShoeboxPreferencesDialog'

    account_row:       Adw.ActionRow  = Gtk.Template.Child()
    sign_out_button:   Gtk.Button     = Gtk.Template.Child()
    auto_row:          Adw.SwitchRow  = Gtk.Template.Child()
    network_row:       Adw.ComboRow   = Gtk.Template.Child()
    charging_row:      Adw.SwitchRow  = Gtk.Template.Child()
    background_switch: Adw.SwitchRow  = Gtk.Template.Child()
    autostart_switch:  Adw.SwitchRow  = Gtk.Template.Child()
    folders_group:     Adw.PreferencesGroup = Gtk.Template.Child()
    add_folder_row:    Adw.ButtonRow  = Gtk.Template.Child()

    def __init__(self, app: ShoeboxApplication):
        super().__init__()
        self.app = app
        self._folder_rows: dict[str, Adw.ActionRow] = {}

        self._populate_account()
        self._bind_sync_settings()
        self._bind_background_settings()
        self._reload_folders()

    # ----- account -----

    def _populate_account(self) -> None:
        account = self.app.primary_account()
        if account is not None:
            self.account_row.set_title(account.display_name or account.username)
            self.account_row.set_subtitle(f'{account.username} · {account.server_url}')
            self.sign_out_button.set_visible(True)

    @Gtk.Template.Callback()
    def _on_sign_out(self, *_args) -> None:
        account = self.app.primary_account()
        if account is None:
            return
        secrets.clear_token(account.id, account.backend)
        self.app.db.delete_account(account.id)
        self.app.settings.set_boolean('setup-complete', False)
        self.app.reset_backend()
        self.close()
        win = self.app.get_active_window()
        if win is not None and hasattr(win, '_open_setup'):
            win._open_setup()

    # ----- sync settings -----

    def _bind_sync_settings(self) -> None:
        s = self.app.settings
        s.bind('sync-auto', self.auto_row, 'active', Gio.SettingsBindFlags.DEFAULT)
        s.bind('sync-charging-only', self.charging_row, 'active',
               Gio.SettingsBindFlags.DEFAULT)

        model = Gtk.StringList.new([label for _, label in _NETWORK_PREFS])
        self.network_row.set_model(model)
        current = s.get_string('sync-network')
        for i, (key, _label) in enumerate(_NETWORK_PREFS):
            if key == current:
                self.network_row.set_selected(i)
                break
        self.network_row.connect('notify::selected', self._on_network_changed)

    def _on_network_changed(self, row, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(_NETWORK_PREFS):
            self.app.settings.set_string('sync-network', _NETWORK_PREFS[idx][0])

    # ----- background settings -----

    def _bind_background_settings(self) -> None:
        s = self.app.settings
        s.bind('run-in-background', self.background_switch, 'active',
               Gio.SettingsBindFlags.DEFAULT)
        # Autostart isn't a plain bind because flipping it has a
        # side-effect (write/remove ~/.config/autostart/<id>.desktop).
        self.autostart_switch.set_active(s.get_boolean('autostart-enabled'))
        self.autostart_switch.connect('notify::active', self._on_autostart_toggled)

    def _on_autostart_toggled(self, sw: Adw.SwitchRow, _pspec) -> None:
        enabled = sw.get_active()
        self.app.settings.set_boolean('autostart-enabled', enabled)

        def on_response(code: int) -> None:
            if code == 0:
                return
            sw.handler_block_by_func(self._on_autostart_toggled)
            sw.set_active(not enabled)
            sw.handler_unblock_by_func(self._on_autostart_toggled)
            self.app.settings.set_boolean('autostart-enabled', not enabled)

        background_portal.request_background(
            autostart=enabled, app_id=_APP_ID, app_name=_APP_NAME,
            on_response=on_response)

    # ----- folder management -----

    def _reload_folders(self) -> None:
        for path, row in list(self._folder_rows.items()):
            self.folders_group.remove(row)
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
        # Insert just before the "Add folder…" row.
        self.folders_group.add(row)
        self._folder_rows[path] = row

    def _remove_folder(self, path: str) -> None:
        account = self.app.primary_account()
        if account is None:
            return
        self.app.db.remove_sync_dir(account.id, path)
        row = self._folder_rows.pop(path, None)
        if row is not None:
            self.folders_group.remove(row)
        self._sync_setting_strv()

    def _sync_setting_strv(self) -> None:
        account = self.app.primary_account()
        if account is None:
            return
        paths = [p for p, _ in self.app.db.list_sync_dirs(account.id)]
        self.app.settings.set_strv('sync-directories', paths)

    @Gtk.Template.Callback()
    def _on_add_folder(self, *_args) -> None:
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
