"""Application version; release tags and artifacts must match this constant."""
VERSION = '1.13.1'
REPOSITORY = 'wujiezero/ps5-jb-server'
# Front-end files a release may ship: flat under web/, lowercase, js/css/html only.
# The web updater (update_worker.WEB_FILE_PATTERN, kept self-contained), the
# release builder, the installer and the admin static server all use this rule.
WEB_FILE_PATTERN = r'[a-z0-9][a-z0-9._-]{0,63}\.(?:js|css|html)'


def version_info():
    return {'version': VERSION, 'repository': REPOSITORY}
