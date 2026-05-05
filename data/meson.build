desktop_file = configure_file(
  input: app_id + '.desktop.in.in',
  output: app_id + '.desktop',
  configuration: conf,
  install: true,
  install_dir: datadir / 'applications',
)

metainfo_file = configure_file(
  input: app_id + '.metainfo.xml.in.in',
  output: app_id + '.metainfo.xml',
  configuration: conf,
  install: true,
  install_dir: datadir / 'metainfo',
)

install_data(
  app_id + '.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

gnome.compile_resources(
  meson.project_name(),
  'shoebox.gresource.xml',
  gresource_bundle: true,
  source_dir: meson.current_source_dir(),
  install: true,
  install_dir: pkgdatadir,
)

install_data(
  'icons/hicolor/scalable/apps' / app_id + '.svg',
  install_dir: datadir / 'icons/hicolor/scalable/apps',
)

# Validators (optional; only run if tools are available).
desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('validate-desktop',
       desktop_validate,
       args: [desktop_file],
       suite: 'data')
endif

appstream_util = find_program('appstreamcli', required: false)
if appstream_util.found()
  test('validate-metainfo',
       appstream_util,
       args: ['validate', '--no-net', '--explain', metainfo_file],
       suite: 'data')
endif
