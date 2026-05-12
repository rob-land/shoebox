blueprint_compiler = find_program('blueprint-compiler')

blueprint_sources = files(
  'detail.blp',
  'thumbnail-tile.blp',
  'preferences.blp',
  'gallery.blp',
)

blueprints = custom_target('blueprints',
  input:  blueprint_sources,
  output: '.',
  command: [blueprint_compiler, 'batch-compile',
            '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

# Copy style.css into the build dir so it can be bundled alongside the
# compiled blueprints under a single source_dir.
style_css = configure_file(
  input:  'style.css',
  output: 'style.css',
  copy:   true,
)

gresource = gnome.compile_resources(
  application_id,
  application_id + '.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: [blueprints, style_css],
  source_dir: meson.current_build_dir(),
)
