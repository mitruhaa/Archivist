application_id = 'io.github.mitruhaa.Archivist'

foreach size : ['512x512', '256x256']
  icon_dir = 'hicolor' / size / 'apps'
  install_data(
    icon_dir / ('@0@.png').format(application_id),
    install_dir: get_option('datadir') / 'icons' / icon_dir
  )
endforeach
