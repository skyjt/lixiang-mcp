"""Public protocol identifiers from ha-lixiang v1.3.2 (7e9726bb).

Copyright (c) 2026 ha-lixiang contributors. MIT: licenses/ha-lixiang-MIT.txt.
These identify the application/API audiences, not an account, device or signing key.
No secrets.py/DEFAULT_HAC_KEY/DEFAULT_XDEV/DEFAULT_APP_TOKEN values belong here.
"""

from urllib.parse import urlencode

from .config import Profile
from .transport import ACCOUNT

PROFILE_ID = "ha-lixiang-v1.3.2"


def builtin_profile() -> Profile:
    return Profile(
        profile_id=PROFILE_ID,
        client_id="2AQClOaegaA7XecMSFx1p",
        redirect_uri=ACCOUNT + "/app-auth",
        login_audience="5iIapSfVJlln0vU0OzUCH9",
        login_scope="iam:client:type:app openid",
        vehicles_audience="7gbeHMwBPMZA5SU1b2awIo",
        vss_audience="1j0vgTqagJUHuT6nLmbTGx",
        mesh_audience="1j0vgTqagJUHuT6nLmbTGx",
        vat_audience="5Tc7yDrnMzALwc9Rytl9sp",
        login_app_version="8.22.0",
        sdk_version="0.0.0-snapshot-20240614055722",
        sign_app_version="8.25.4-10463",
        login_user_agent=(
            "Mozilla/5.0 (Phone; OpenHarmony 6.1) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36  ArkWeb/6.1.0.117 Mobile m01/8.22.0"
        ),
        api_user_agent="m01/8.25.4-10463 (iPad; iOS 16.7.12; Scale/2.00)",
    )


def official_verification_url(profile: Profile, device_id: str) -> str:
    """Local UI only. No phone/password/code/token; never follow an untrusted returned URL."""
    return (
        ACCOUNT
        + "/app-auth?"
        + urlencode(
            {
                "mode": "h5",
                "client_id": profile.client_id,
                "redirect_uri": ACCOUNT + "/app-auth",
                "response_type": "code",
                "scope": profile.login_scope,
                "audience": profile.login_audience,
                "device_id": device_id,
            }
        )
    )
