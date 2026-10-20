"""Product defaults; no upstream address is exposed in setup."""
DOMAIN = "alphaess_relay"
CONF_SERIAL = "serial"
CONF_LISTEN_HOST = "listen_host"
CONF_LISTEN_PORT = "listen_port"
CONF_FALLBACK_ENABLED = "fallback_enabled"
DEFAULT_FALLBACK_ENABLED = True
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 17778
UPSTREAM_HOST = "cloudiot.alphaess.com"
UPSTREAM_PORT = 17777
FRESHNESS_SECONDS = 45
