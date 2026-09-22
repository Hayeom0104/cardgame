"""Read-only production preflight. Never creates players or Discord threads."""
import httpx
from app.config import settings
from app.central.client import CentralClient


def main() -> int:
    required = {
        "DECKOUT_CENTRAL_URL": settings.central_base_url,
        "DECKOUT_API_KEY": settings.central_api_key,
        "DECKOUT_INGRESS_SECRET": settings.ingress_secret,
        "DECKOUT_CHANNEL_ID": settings.parent_channel_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print("Missing settings: " + ", ".join(missing))
        return 1
    if settings.skip_capability_check or settings.allow_unauthenticated_local:
        print("Disable development bypasses before production.")
        return 1
    try:
        with httpx.Client(timeout=5, follow_redirects=False) as transport:
            response = transport.get(settings.central_base_url.rstrip('/') + '/healthz')
            response.raise_for_status()
            CentralClient(settings.central_base_url, settings.central_api_key,
                          client=transport).check_capabilities()
    except Exception:
        print("Central preflight failed: check routing, API key and capability contract.")
        return 1
    print("Central health and capabilities passed. Reverse routing and Discord remain untested.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
