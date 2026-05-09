project(
  'shoebox',
  version: '0.1.0',
  meson_version: '>= 1.4.0',
  default_options: ['warning_level=2'],
)

i18n = import('i18n')
gnome = import('gnome')
python = import('python')

py = python.find_installation('python3', modules: ['gi'])

app_id = 'land.rob.shoebox'
app_name = 'Shoebox'

prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()
moduledir = py.get_install_dir() / 'shoebox'
localedir = prefix / get_option('localedir')

conf = configuration_data()
conf.set('PYTHON', py.full_path())
conf.set('VERSION', meson.project_version())
conf.set('APP_ID', app_id)
conf.set('PKGDATADIR', pkgdatadir)
conf.set('MODULEDIR', py.get_install_dir())
conf.set('LOCALEDIR', localedir)

subdir('data')
subdir('src')
subdir('po')
subdir('tests')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
