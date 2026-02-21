"""
Backend API client for the Discord bot.
All FPL data is fetched through the website backend which handles
caching in PostgreSQL, proxy rotation, and rate limiting.
"""

import os
import aiohttp
from bot.logging_config import get_logger

logger = get_logger('backend_api')

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:3001")


async def _get(session: aiohttp.ClientSession, path: str, params: dict = None):
    """Make a GET request to the backend API."""
    url = f"{BACKEND_URL}{path}"
    try:
        async with session.get(url, params=params) as response:
            if response.status == 200:
                return await response.json()
            else:
                logger.warning(f"Backend returned {response.status} for {path}")
                return None
    except aiohttp.ClientError as e:
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
    Get current or last finished gameweek info.
    Returns: { 'gw': int, 'is_finished': bool, 'event': dict } or None
    """
    if not bootstrap_data:
        bootstrap_data = await get_bootstrap(session)
    if not bootstrap_data:
        return None

    events = bootstrap_data.get('events', [])
    gw_event = next((e for e in events if e.get('is_current')), None)

    if not gw_event:
        finished = [e for e in events if e.get('finished')]
        if finished:
            gw_event = max(finished, key=lambda x: x['id'])

    if not gw_event:
        return None

    return {
        'gw': gw_event['id'],
        'is_finished': gw_event.get('finished', False),
        'event': gw_event,
    }
