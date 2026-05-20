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
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.version = version
        self.settings = settings.get()
        self.db = Database()
        self._backend: Optional[Backend] = None
        self._account: Optional[Account] = None
        self._background = False
        self._held = False
        self._sync_timer_id: Optional[int] = None

        self.add_main_option(
            'background', ord('b'), GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            'Run without showing a window (periodic sync only)', None,
        )

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

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        opts = command_line.get_options_dict().end().unpack()
        if opts.get('background'):
            self._background = True
            self._start_periodic_sync()
            self.hold()  # keep alive without a window
            self._held = True
        else:
            self.activate()
        return 0

    def do_activate(self) -> None:
        if self._background and self.get_active_window() is None:
            # Headless launch: nothing to present. Periodic sync was
            # already armed by do_command_line. A second activate (e.g.
            # via DBus) will fall through and build a window.
            return
        win = self.get_active_window()
        if win is None:
            win = ShoeboxWindow(application=self)
        win.set_visible(True)
        win.present()
        # Returning to foreground after a close-to-background: release
        # the hold and stop the periodic timer; the visible window will
        # request sync interactively.
        if self._held:
            self.release()
            self._held = False
        self._stop_periodic_sync()

    def hold_for_background(self) -> None:
        """Called by the window when it hides itself in close-to-bg
        mode. Arms the periodic sync timer and holds the app alive."""
        if not self._held:
            self.hold()
            self._held = True
        self._start_periodic_sync()

    def _start_periodic_sync(self) -> None:
        if self._sync_timer_id is not None:
            return
        # 30-minute cadence. The sync manager's conditions module
        # gates whether the run actually proceeds based on the user's
        # Wi-Fi-only / charging-only preferences, so we can fire
        # blindly here without re-checking conditions.
        self._sync_timer_id = GLib.timeout_add_seconds(
            30 * 60, self._on_sync_tick)
        # Also fire one immediately so the first tick isn't 30 min away.
        GLib.idle_add(self._on_sync_tick)

    def _stop_periodic_sync(self) -> None:
        if self._sync_timer_id is not None:
            GLib.source_remove(self._sync_timer_id)
            self._sync_timer_id = None

    def _on_sync_tick(self) -> bool:
        if not self.settings.get_boolean('sync-auto'):
            return True  # keep the timer armed; user may flip it back on
        # Fire-and-forget. The sync manager logs its own progress;
        # since there's no window in --background mode the user only
        # sees the result through notifications (future) or by
        # reopening the app.
        account = self.primary_account()
        if account is None:
            return True
        backend = self.primary_backend()
        if backend is None:
            return True
        try:
            from .sync.manager import SyncManager
            mgr = SyncManager(self, account, backend)
            mgr.run()
        except Exception:
            # Don't let a transient sync failure tear down the timer.
            import logging
            logging.getLogger(__name__).exception('background sync failed')
        return True

    def do_shutdown(self) -> None:
        self._stop_periodic_sync()
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
        # Sync-now has a Sync button in the gallery header; bind a key
        # too so it's reachable without aiming the cursor.
        self.set_accels_for_action('app.sync-now', ['<Primary>r', 'F5'])

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
