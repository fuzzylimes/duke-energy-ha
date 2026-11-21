"""Constants describing Duke Energy's login and API (see docs/SPEC.md).

Everything Duke-specific that has changed in the past lives here, so a Duke-side
change is usually a one-line edit plus a regression test.
"""

from __future__ import annotations

import re
from typing import Final

import yarl

# --- Auth0 (docs/SPEC.md §2) -------------------------------------------------

AUTH0_HOST: Final = "login.duke-energy.com"
AUTH0_BASE: Final = yarl.URL(f"https://{AUTH0_HOST}")
AUTHORIZE_URL: Final = AUTH0_BASE / "authorize"
TOKEN_URL: Final = AUTH0_BASE / "oauth" / "token"
ISSUER: Final = f"https://{AUTH0_HOST}/"

# Duke's iOS app client. Auth0 only accepts this client's registered redirect.
CLIENT_ID: Final = "PitoKqxMh8thrFF8rRlYGrAs3LbSD2dj"
CALLBACK_PATH: Final = "/ios/com.duke-energy.app/callback"
REDIRECT_URI: Final = f"https://{AUTH0_HOST}{CALLBACK_PATH}"
SCOPE: Final = "openid profile email offline_access"

# base64 of {"name": "Auth0.swift", "env": {"iOS": "27.0", "swift": "6.x"},
# "version": "2.19.0"}, the SDK identity the iOS app sends.
AUTH0_CLIENT: Final = (
    "eyJuYW1lIjogIkF1dGgwLnN3aWZ0IiwgImVudiI6IHsiaU9TIjogIjI3LjAiLCAic3dpZnQiOiAi"
    "Ni54In0sICJ2ZXJzaW9uIjogIjIuMTkuMCJ9"
)

# Hosts a rejected sign-in may bounce to instead of the callback.
PUBLIC_SITE_HOSTS: Final = frozenset({"duke-energy.com", "www.duke-energy.com"})

# --- Duke API (docs/SPEC.md §3, §4) -----------------------------------------

API_BASE: Final = yarl.URL("https://api-v2.cma.duke-energy.app")
AUTH_TOKEN_URL: Final = API_BASE / "login" / "auth-token"
ACCOUNT_LIST_URL: Final = API_BASE / "account" / "list"
ACCOUNT_DETAILS_URL: Final = API_BASE / "account-details-v2"
USAGE_GRAPH_URL: Final = API_BASE / "account" / "usage" / "graph"

# Duke API client credentials from the iOS app (public in every fork).
API_CLIENT_ID: Final = "HO2JKfv2dVuXhLHhleDr1s6fgVlPduGxVBO6GaS3dDjE7Kp8"
API_CLIENT_SECRET: Final = (
    "g4236o8ROFMD4JuVI4tsgLY7NiIEGXQgzzCnH9RiRrvFC6IN4KFg3A6dBmGIIuW6"
)

# --- User-Agents (docs/DESIGN.md §3.4) ---------------------------------------

# Built-in default for the app identity Duke's CDN checks. The remote
# known-good value and the user override take precedence over this.
DEFAULT_APP_USER_AGENT: Final = (
    "Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0"
)
APP_USER_AGENT_PATTERN: Final = re.compile(
    r"^Duke%20Energy/\d{1,6} CFNetwork/\d+(?:\.\d+){0,5} Darwin/\d+(?:\.\d+){0,3}$"
)

# What users set in their browser so Duke's login accepts it (SPEC §2.6).
BROWSER_USER_AGENT: Final = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
)

# --- Data ---------------------------------------------------------------------

# Every known Duke service area is in Eastern time (SPEC §4.4, an assumption).
DUKE_TIMEZONE: Final = "America/New_York"
REQUEST_DATE_FORMAT: Final = "%m/%d/%Y"
MAX_WINDOW_DAYS: Final = 31

ID_TOKEN_REFRESH_MARGIN: Final = 300.0
API_TOKEN_REFRESH_MARGIN: Final = 60.0
DEFAULT_API_TOKEN_LIFETIME: Final = 1800.0
REQUEST_TIMEOUT: Final = 30.0
