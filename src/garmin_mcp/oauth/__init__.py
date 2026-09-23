"""Official Garmin Connect Developer Program OAuth 2.0 (PKCE) path.

Enabled with ``GARMIN_AUTH_MODE=oauth``. The unofficial garminconnect session
path (``GARMIN_AUTH_MODE=session``, the default) is unchanged.

Endpoints and token shapes follow Garmin's published OAuth 2.0 PKCE
specification (developerportal.garmin.com) and the Health/Activity wellness
REST API under ``https://apis.garmin.com/wellness-api/rest``.
"""

from garmin_mcp.oauth.config import OAuthConfig, auth_mode, load_oauth_config
from garmin_mcp.oauth.errors import OfficialApiUnavailableError

__all__ = [
    "OAuthConfig",
    "OfficialApiUnavailableError",
    "auth_mode",
    "load_oauth_config",
]
