configure_file(
  input: 'shoebox.in',
  output: 'shoebox',
  configuration: conf,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

shoebox_sources = [
  'shoebox/__init__.py',
  'shoebox/main.py',
  'shoebox/application.py',
  'shoebox/background_portal.py',
  'shoebox/window.py',
  'shoebox/database.py',
  'shoebox/exif_writer.py',
  'shoebox/secrets.py',
  'shoebox/settings.py',
  'shoebox/worker.py',
]

backend_sources = [
  'shoebox/backends/__init__.py',
  'shoebox/backends/base.py',
  'shoebox/backends/immich.py',
]

sync_sources = [
  'shoebox/sync/__init__.py',
  'shoebox/sync/manager.py',
  'shoebox/sync/scanner.py',
  'shoebox/sync/conditions.py',
]

ui_sources = [
  'shoebox/ui/__init__.py',
  'shoebox/ui/setup.py',
  'shoebox/ui/gallery.py',
  'shoebox/ui/detail.py',
  'shoebox/ui/edit_dialog.py',
  'shoebox/ui/bulk_date_dialog.py',
  'shoebox/ui/preferences.py',
  'shoebox/ui/widgets.py',
]

py.install_sources(shoebox_sources, subdir: 'shoebox')
py.install_sources(backend_sources, subdir: 'shoebox/backends')
py.install_sources(sync_sources, subdir: 'shoebox/sync')
py.install_sources(ui_sources, subdir: 'shoebox/ui')
