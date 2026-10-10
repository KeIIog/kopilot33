from aiohttp import web

from . import (
  bluetooth,
  carrot_navi,
  cars,
  dashcam,
  egpu_model,
  intro,
  ko_auto_tune,
  ko_drive_log,
  ko_vehicle,
  mapbox_tokens,
  params,
  screenrecord,
  settings,
  setting_favorites,
  setting_popular_values,
  setting_profiles,
  ssh_keys,
  static,
  stream,
  system,
  terminal,
  tools,
  vision_diag,
  vision_test,
  web_sound,
  web_settings,
  ws,
  xiaoge,
  youtube_live,
)


def register_all(app: web.Application) -> None:
  bluetooth.register(app)
  static.register(app)
  intro.register(app)
  ko_drive_log.register(app)
  ko_auto_tune.register(app)
  ko_vehicle.register(app)
  carrot_navi.register(app)
  stream.register(app)
  ws.register(app)
  settings.register(app)
  params.register(app)
  setting_favorites.register(app)
  setting_popular_values.register(app)
  setting_profiles.register(app)
  web_settings.register(app)
  ssh_keys.register(app)
  cars.register(app)
  system.register(app)
  terminal.register(app)
  dashcam.register(app)
  egpu_model.register(app)
  screenrecord.register(app)
  tools.register(app)
  xiaoge.register(app)
  mapbox_tokens.register(app)
  youtube_live.register(app)
  vision_test.register(app)
  vision_diag.register(app)
  web_sound.register(app)
