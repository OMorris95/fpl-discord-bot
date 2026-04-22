"""
Backend API client for the Discord bot.
All FPL data is fetched through the website backend which handles
caching in PostgreSQL, proxy rotation, and rate limiting.
"""

import os
from datetime import datetime, timezone
import aiohttp
from bot.logging_config import get_logger

logger = get_logger('backend_api')

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:3001")


def _get_bot_api_key():
    return os.getenv("BOT_API_KEY", "")


class FplUnavailableError(Exception):
    """Raised when the FPL API appears to be updating or unavailable."""
    pass


async def _get(session: aiohttp.ClientSession, path: str, params: dict = None):
    """Make a GET request to the backend API."""
    url = f"{BACKEND_URL}{path}"
    try:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
            elif response.status in (502, 503, 504):
                logger.warning(f"FPL unavailable ({response.status}) for {path}")
                raise FplUnavailableError()
            else:
                logger.warning(f"Backend returned {response.status} for {path}")
                return None
    except FplUnavailableError:
        raise
    except (aiohttp.ClientError, ConnectionError) as e:
        error_str = str(e)
        # Detect FPL API update errors (truncated responses, connection resets)
        if any(hint in error_str for hint in ('ContentLengthError', 'ConnectionReset', 'network name is no longer available')):
            logger.warning(f"FPL appears to be updating for {path}: {e}")
            raise FplUnavailableError()
        logger.error(f"Backend request failed for {path}: {e}")
        return None


# =====================================================
# CORE FPL DATA (proxied through backend with DB caching)
# =====================================================

async def get_bootstrap(session: aiohttp.ClientSession) -> dict | None:
    """Fetch bootstrap data (teams, players, gameweeks). Cached 10 min in backend DB."""
    return await _get(session, "/api/fpl/bootstrap-static/")


async def get_live_data(session: aiohttp.ClientSession, gameweek: int) -> dict | None:
    """Fetch live GW data. Backend syncs every 30s via liveDataCron."""
    return await _get(session, f"/api/fpl/event/{gameweek}/live/")


async def get_fixtures(session: aiohttp.ClientSession) -> list | None:
    """Fetch all fixtures. Cached 1hr (5min during live GW) in backend DB."""
    return await _get(session, "/api/fpl/fixtures/")


async def get_element_summary(session: aiohttp.ClientSession, player_id: int) -> dict | None:
    """Fetch player element-summary (GW history + upcoming fixtures)."""
    return await _get(session, f"/api/fpl/element-summary/{player_id}/")


# =====================================================
# LEAGUE AGGREGATED ENDPOINTS (single-call, DB-backed)
# =====================================================

async def get_league_standings(session: aiohttp.ClientSession, league_id: int) -> dict | None:
    """Fetch league standings. Cached 5 min in backend DB."""
    return await _get(session, f"/api/fpl/leagues-classic/{league_id}/standings/")


async def get_league_picks(
    session: aiohttp.ClientSession,
    league_id: int,
    gameweek: int,
    limit: int = None,
) -> dict | None:
    """
    Fetch ALL manager picks for a league in one call.
    Backend checks DB first, fetches missing from FPL API and stores.

    Args:
        limit: Return first N managers immediately, fetch rest in background.

    Returns:
        Dict mapping manager_id (str) -> picks data (FPL API shape)
    """
    params = {"limit": limit} if limit else None
    return await _get(session, f"/api/league/{league_id}/picks/{gameweek}", params=params)


async def get_league_history(session: aiohttp.ClientSession, league_id: int) -> dict | None:
    """
    Fetch ALL manager history for a league in one call.
    Returns dict mapping manager_id (str) -> history data (FPL API shape with current + chips).
    """
    return await _get(session, f"/api/league/{league_id}/history")


async def get_league_transfers(
    session: aiohttp.ClientSession, league_id: int, gameweek: int
) -> dict | None:
    """
    Fetch ALL manager transfers for a league for a specific gameweek.
    Returns dict mapping manager_id (str) -> { transfers, chip, transfer_cost }.
    """
    return await _get(session, f"/api/league/{league_id}/transfers/{gameweek}")


# =====================================================
# INDIVIDUAL MANAGER DATA
# =====================================================

async def get_manager_picks(
    session: aiohttp.ClientSession, manager_id: int, gameweek: int
) -> dict | None:
    """Fetch individual manager picks from DB-backed endpoint."""
    return await _get(session, f"/api/db/picks/{manager_id}/{gameweek}")


async def get_manager_history(session: aiohttp.ClientSession, manager_id: int) -> dict | None:
    """Fetch individual manager history via FPL proxy (cached in backend DB)."""
    return await _get(session, f"/api/fpl/entry/{manager_id}/history/")


async def get_manager_transfers(session: aiohttp.ClientSession, manager_id: int) -> list | None:
    """Fetch individual manager transfers via FPL proxy (cached in backend DB)."""
    return await _get(session, f"/api/fpl/entry/{manager_id}/transfers/")


# =====================================================
# UTILITY
# =====================================================

async def get_current_gameweek(session: aiohttp.ClientSession) -> int | None:
    """Get the current gameweek number from bootstrap data."""
    data = await get_bootstrap(session)
    if data:
        current = next((e for e in data.get('events', []) if e['is_current']), None)
        return current['id'] if current else None
    return None


async def get_last_completed_gameweek(session: aiohttp.ClientSession) -> int | None:
    """Get the most recently completed gameweek number."""
    data = await get_bootstrap(session)
    if data:
        completed = [e for e in data.get('events', []) if e['finished']]
        if completed:
            return max(completed, key=lambda x: x['id'])['id']
    return None


async def get_gameweek_info(session: aiohttp.ClientSession, bootstrap_data: dict = None) -> dict | None:
    """
    Match the website behavior:
    - if the current GW deadline has passed, use the current GW
    - otherwise use the last fully settled GW

    Returns: { 'gw': int, 'is_finished': bool, 'event': dict } or None
    """
    if not bootstrap_data:
        bootstrap_data = await get_bootstrap(session)
    if not bootstrap_data:
        return None

    events = bootstrap_data.get('events', [])
    current_event = next((e for e in events if e.get('is_current')), None)
    gw_event = None

    if current_event:
        deadline_time = current_event.get('deadline_time')
        if deadline_time:
            deadline = datetime.fromisoformat(deadline_time.replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > deadline:
                gw_event = current_event

    if not gw_event:
        settled = [e for e in events if e.get('finished') and e.get('data_checked')]
        if settled:
            gw_event = max(settled, key=lambda x: x['id'])

    if not gw_event:
        finished = [e for e in events if e.get('finished')]
        if finished:
            gw_event = max(finished, key=lambda x: x['id'])

    if not gw_event:
        return None

    return {
        'gw': gw_event['id'],
        'is_finished': gw_event.get('finished', False) and gw_event.get('data_checked', False),
        'event': gw_event,
    }


# =====================================================
# BOT-SPECIFIC ENDPOINTS (authenticated with BOT_API_KEY)
# =====================================================

async def _bot_get(session: aiohttp.ClientSession, path: str):
    """Make an authenticated GET request to the bot API endpoints."""
    url = f"{BACKEND_URL}{path}"
    headers = {"Authorization": f"Bearer {_get_bot_api_key()}"}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 404:
                return None
            else:
                logger.warning(f"Bot API returned {response.status} for {path}")
                return None
    except (aiohttp.ClientError, ConnectionError) as e:
        logger.error(f"Bot API request failed for {path}: {e}")
        return None


async def get_user_by_discord(session: aiohttp.ClientSession, discord_user_id: str) -> dict | None:
    """Check if a Discord user has a linked website account."""
    return await _bot_get(session, f"/api/bot/user-by-discord/{discord_user_id}")


async def get_deadline_info(session: aiohttp.ClientSession) -> dict | None:
    """Get current + next gameweek deadline info."""
    return await _bot_get(session, "/api/bot/deadline-info")


async def get_injury_alerts(session: aiohttp.ClientSession, manager_id: int) -> dict | None:
    """Get flagged players in a manager's squad."""
    return await _bot_get(session, f"/api/bot/injury-alerts/{manager_id}")


async def get_captain_suggestion(session: aiohttp.ClientSession, manager_id: int) -> dict | None:
    """Get top 3 captain suggestions for a manager."""
    return await _bot_get(session, f"/api/bot/captain-suggestion/{manager_id}")


async def get_transfer_suggestions(session: aiohttp.ClientSession, manager_id: int) -> dict | None:
    """Get top 3 transfer suggestions for a manager."""
    return await _bot_get(session, f"/api/bot/transfer-suggestions/{manager_id}")


