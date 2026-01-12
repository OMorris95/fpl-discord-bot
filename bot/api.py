"""FPL API helper functions for the Discord bot."""

import asyncio
import json
import time
from pathlib import Path
import aiohttp

from bot.logging_config import get_logger

logger = get_logger('api')

# --- Constants ---
BASE_API_URL = "https://fantasy.premierleague.com/api/"
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
}
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache TTLs in seconds (None = never expires)
CACHE_TTLS = {
    'bootstrap': 6 * 60 * 60,         # 6 hours
    'fixtures': 6 * 60 * 60,          # 6 hours
    'live_event': 60,                 # 60 seconds during live matches
    'league_standings': 5 * 60,       # 5 minutes
    'league_picks': 5 * 60,           # 5 minutes during live GWs
    'league_history': 5 * 60,         # 5 minutes during live GWs
    'finished_gw': None,              # Never expires for completed GWs
}

# Module-level semaphore reference (set by the bot during initialization)
_api_semaphore = None


def set_api_semaphore(semaphore):
    """Set the API semaphore for rate limiting. Called by the bot during setup."""
    global _api_semaphore
    _api_semaphore = semaphore


# --- Cache Helpers ---
def _load_cached_json_sync(path: Path):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


async def load_cached_json(path: Path):
    return await asyncio.to_thread(_load_cached_json_sync, path)


def _save_cached_json_sync(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f)


async def save_cached_json(path: Path, payload: dict):
    await asyncio.to_thread(_save_cached_json_sync, path, payload)


async def is_cache_fresh(meta_path: Path, cache_type: str) -> bool:
    """Check if cache is still valid based on TTL."""
    meta = await load_cached_json(meta_path)
    if not meta:
        return False
    ttl = CACHE_TTLS.get(cache_type)
    if ttl is None:  # Never expires
        return True
    age = time.time() - meta.get('timestamp', 0)
    return age < ttl


async def save_cache_with_meta(cache_path: Path, data: dict, gameweek: int = None):
    """Save cache data and a separate meta file with timestamp."""
    await save_cached_json(cache_path, data)
    meta_path = cache_path.with_suffix('.meta.json')
    await save_cached_json(meta_path, {'timestamp': time.time(), 'gw': gameweek})


# --- API Functions ---
async def fetch_fpl_api(session, url, cache_key=None, cache_gw=None, force_refresh=False,
                        max_retries=3, base_backoff=1.0):
    """
    Fetches data from the FPL API asynchronously with optional caching and retry logic.

    Args:
        session: aiohttp ClientSession
        url: API URL to fetch
        cache_key: Optional cache key for storing response
        cache_gw: Optional gameweek for cache key suffix
        force_refresh: Force refresh even if cache exists
        max_retries: Maximum number of retry attempts (default 3)
        base_backoff: Base delay in seconds for exponential backoff (default 1.0)

    Returns:
        JSON data or None on failure
    """
    cache_path = None
    if cache_key:
        cache_suffix = f"_gw{cache_gw}" if cache_gw is not None else ""
        cache_path = CACHE_DIR / f"{cache_key}{cache_suffix}.json"
        cached = await load_cached_json(cache_path)
        if cached and not force_refresh:
            return cached.get("data", cached)

    last_error = None
    for attempt in range(max_retries):
        try:
            # Use semaphore if available for rate limiting
            if _api_semaphore:
                await _api_semaphore.acquire()
            try:
                async with session.get(url, headers=REQUEST_HEADERS) as response:
                    if response.status == 200:
                        data = await response.json()
                        if cache_path:
                            payload = {"data": data, "gameweek": cache_gw}
                            await save_cached_json(cache_path, payload)
                        return data
                    elif response.status >= 500:
                        # Server error - retry with backoff
                        last_error = f"Server error {response.status}"
                        logger.warning(f"API server error: {url} returned {response.status} (attempt {attempt + 1}/{max_retries})")
                    elif response.status == 429:
                        # Rate limited - wait longer and retry
                        last_error = "Rate limited"
                        backoff = base_backoff * (4 ** attempt)  # More aggressive backoff for rate limits
                        logger.warning(f"Rate limited on {url}, waiting {backoff:.1f}s before retry")
                        await asyncio.sleep(backoff)
                        continue
                    else:
                        # Client error (4xx except 429) - don't retry
                        logger.warning(f"API request failed: {url} returned status {response.status}")
                        return None
            finally:
                if _api_semaphore:
                    _api_semaphore.release()

            # Apply exponential backoff before retry for server errors
            if attempt < max_retries - 1:
                backoff = base_backoff * (2 ** attempt)
                logger.debug(f"Retrying {url} in {backoff:.1f}s (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(backoff)

        except aiohttp.ClientError as e:
            last_error = str(e)
            logger.warning(f"Network error fetching {url}: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                backoff = base_backoff * (2 ** attempt)
                await asyncio.sleep(backoff)

    # All retries exhausted
    logger.error(f"Failed to fetch {url} after {max_retries} attempts. Last error: {last_error}")
    return None


async def get_current_gameweek(session):
    """Determines the current FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        current_event = next((event for event in bootstrap_data.get('events', []) if event['is_current']), None)
        return current_event['id'] if current_event else None
    return None


async def get_last_completed_gameweek(session):
    """Determines the most recently completed FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        completed_events = [event for event in bootstrap_data.get('events', []) if event['finished']]
        if completed_events:
            return max(completed_events, key=lambda x: x['id'])['id']
    return None


async def get_bootstrap_data(session, cache_key: str = "bootstrap"):
    """Fetches and returns bootstrap data with caching."""
    return await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key=cache_key
    )


async def get_gameweek_info(session, bootstrap_data=None):
    """
    Gets the current or last finished gameweek info.

    Returns:
        dict with keys: 'gw' (int), 'is_finished' (bool), 'event' (dict)
        or None if no gameweek found
    """
    if not bootstrap_data:
        bootstrap_data = await get_bootstrap_data(session)

    if not bootstrap_data:
        return None

    events = bootstrap_data.get('events', [])

    # Try to find current gameweek first
    gw_event = next((event for event in events if event.get('is_current')), None)

    # Fall back to last finished gameweek
    if not gw_event:
        finished_events = [e for e in events if e.get('finished')]
        if finished_events:
            gw_event = max(finished_events, key=lambda x: x['id'])

    if not gw_event:
        return None

    return {
        'gw': gw_event['id'],
        'is_finished': gw_event.get('finished', False),
        'event': gw_event
    }


async def get_live_data(session, gameweek: int, bot_cache=None):
    """
    Fetches live event data for a gameweek.

    Args:
        session: aiohttp session
        gameweek: Gameweek number
        bot_cache: Optional bot.live_fpl_data cache to check first

    Returns:
        Live data dict or None
    """
    # Try to use bot cache if available and for the correct gameweek
    if bot_cache and bot_cache.get('gw') == gameweek:
        return bot_cache

    return await fetch_fpl_api(
        session,
        f"{BASE_API_URL}event/{gameweek}/live/"
    )


async def get_league_data(session, league_id: int, gameweek: int = None, force_refresh: bool = False):
    """
    Fetches league standings data.

    Args:
        session: aiohttp session
        league_id: FPL league ID
        gameweek: Optional gameweek for cache key
        force_refresh: Force refresh cache

    Returns:
        League data dict or None
    """
    return await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=gameweek,
        force_refresh=force_refresh
    )


async def get_league_managers(session, league_id):
    """Fetches all manager names and IDs for the specified league."""
    league_url = f"{BASE_API_URL}leagues-classic/{league_id}/standings/?page_standings=1"
    league_data = await fetch_fpl_api(session, league_url, cache_key=f"league_{league_id}_standings_p1")
    if league_data and 'standings' in league_data and 'results' in league_data['standings']:
        return {
            manager['player_name']: manager['entry']
            for manager in league_data['standings']['results']
        }
    return {}


async def get_live_manager_details(session, manager_entry, current_gw, live_points_map, all_players_map, live_data,
                                    is_finished=False, cached_picks=None, cached_history=None):
    """Fetches picks/history for a manager and calculates their score, handling auto-subs for finished GWs.

    Args:
        cached_picks: Optional pre-fetched picks data (from get_league_picks_cached)
        cached_history: Optional pre-fetched history data (from get_league_history_cached)
    """
    manager_id = manager_entry['entry']

    # Use cached data if provided, otherwise fetch
    if cached_picks is not None and cached_history is not None:
        picks_data = cached_picks.get(manager_id)
        history_data = cached_history.get(manager_id)
    else:
        picks_task = fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/",
            cache_key=f"picks_entry_{manager_id}",
            cache_gw=current_gw,
            force_refresh=is_finished  # Refresh if the gameweek is over to get final subs/points
        )
        history_task = fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/history/",
            cache_key=f"history_entry_{manager_id}",
            cache_gw=current_gw
        )
        picks_data, history_data = await asyncio.gather(picks_task, history_task)

    if not picks_data or not history_data:
        return None

    # --- Determine final GW points ---
    final_gw_points = 0
    scoring_picks = []

    # The API's official points are the source of truth if available for a finished GW
    if is_finished and picks_data.get('automatic_subs'):
        final_gw_points = picks_data['entry_history']['points']

        # Determine scoring picks for the image based on auto-subs
        automatic_subs = picks_data.get('automatic_subs', [])
        subs_in = {sub['element_in'] for sub in automatic_subs}
        subs_out = {sub['element_out'] for sub in automatic_subs}

        for p in picks_data['picks']:
            is_starter = p['position'] <= 11
            if (is_starter and p['element'] not in subs_out) or \
               (not is_starter and p['element'] in subs_in):
                scoring_picks.append(p)
    else:
        # --- Manual calculation (for live GWs or when official points are not ready) ---
        gw_points = 0
        active_chip = picks_data.get('active_chip')

        # Determine captain status first
        captain_pick = next((p for p in picks_data['picks'] if p['is_captain']), None)
        captain_played = True
        if captain_pick:
            captain_id = captain_pick['element']
            captain_minutes = live_points_map.get(captain_id, {}).get('minutes', 0)

            # Find the captain's team ID from bootstrap data
            captain_player_details = all_players_map.get(captain_id)
            captain_team_id = captain_player_details['team'] if captain_player_details else None

            # Find the captain's fixture from the live data
            captain_fixture = None
            if captain_team_id and 'fixtures' in live_data:
                captain_fixture = next((f for f in live_data['fixtures'] if f['team_h'] == captain_team_id or f['team_a'] == captain_team_id), None)

            # Captain is considered not to have played if his minutes are 0 AND his game is over
            if captain_minutes == 0 and captain_fixture and captain_fixture.get('finished', False):
                captain_played = False
            # If the captain's game hasn't finished, he's still considered 'playing' for captaincy purposes
            elif captain_minutes == 0 and (not captain_fixture or not captain_fixture.get('finished', False)):
                captain_played = True

        # --- MANUAL SUBSTITUTION LOGIC ---
        if active_chip == 'bboost':
            scoring_picks = picks_data['picks']
        else:
            starters = [p for p in picks_data['picks'] if p['position'] <= 11]
            bench = sorted([p for p in picks_data['picks'] if p['position'] > 11], key=lambda x: x['position'])

            squad = list(starters)  # This is the list of players we will modify

            # 1. Substitute goalkeeper if needed
            starting_gk = next((p for p in squad if all_players_map[p['element']]['element_type'] == 1), None)
            if starting_gk and live_points_map.get(starting_gk['element'], {}).get('minutes', 0) == 0:
                sub_gk = next((p for p in bench if all_players_map[p['element']]['element_type'] == 1), None)
                if sub_gk and live_points_map.get(sub_gk['element'], {}).get('minutes', 0) > 0:
                    squad = [sub_gk if p == starting_gk else p for p in squad]

            # 2. Substitute outfield players
            for sub_in_player in bench:
                if all_players_map[sub_in_player['element']]['element_type'] == 1 or live_points_map.get(sub_in_player['element'], {}).get('minutes', 0) == 0:
                    continue

                player_subbed_out = None

                # Find a player to replace
                for i, player_to_replace in enumerate(squad):
                    is_outfield = all_players_map[player_to_replace['element']]['element_type'] != 1
                    did_not_play = live_points_map.get(player_to_replace['element'], {}).get('minutes', 0) == 0

                    if is_outfield and did_not_play:
                        # Create a potential new squad with the sub
                        potential_squad = list(squad)
                        potential_squad[i] = sub_in_player

                        # Validate formation
                        counts = {1: 0, 2: 0, 3: 0, 4: 0}
                        for p in potential_squad:
                            player_type = all_players_map[p['element']]['element_type']
                            counts[player_type] += 1

                        if counts[1] == 1 and counts[2] >= 3 and counts[3] >= 2 and counts[4] >= 1:
                            player_subbed_out = player_to_replace
                            squad = potential_squad
                            break  # Sub successful, move to next bench player

                if player_subbed_out:
                    break

            scoring_picks = squad

        # Calculate points from the determined scoring players
        for p in scoring_picks:
            player_points = live_points_map.get(p['element'], {}).get('total_points', 0)

            # Start with a base multiplier of 1 for any player in the scoring list
            effective_multiplier = 1

            # Apply captaincy rules
            if p['is_captain']:
                if captain_played:
                    effective_multiplier = 3 if active_chip == '3xc' else 2
                else:  # Captain didn't play and their game is over
                    effective_multiplier = 1
            elif p['is_vice_captain'] and not captain_played:
                vice_captain_minutes = live_points_map.get(p['element'], {}).get('minutes', 0)
                if vice_captain_minutes > 0:
                    # Promote VC only if they are in the final scoring picks and have played
                    if any(sp['element'] == p['element'] for sp in scoring_picks):
                        effective_multiplier = 2

            p['final_multiplier'] = effective_multiplier

            gw_points += player_points * effective_multiplier

        transfer_cost = picks_data['entry_history']['event_transfers_cost']
        final_gw_points = gw_points - transfer_cost

    # --- Calculate total points ---
    pre_gw_total = 0
    if current_gw > 1:
        prev_gw_history = next((gw for gw in history_data['current'] if gw['event'] == current_gw - 1), None)
        if prev_gw_history:
            pre_gw_total = prev_gw_history['total_points']

    live_total_points = pre_gw_total + final_gw_points

    # --- Final data ---
    # The 'picks' data needs to be passed for image generation
    picks_data['scoring_picks'] = scoring_picks

    # Calculate players played for the table view (always just the starting XI for simplicity)
    starters = [p for p in picks_data['picks'] if p['position'] <= 11]
    players_played_count = sum(1 for p in starters if live_points_map.get(p['element'], {}).get('minutes', 0) > 0)

    return {
        "id": manager_id,
        "name": manager_entry['player_name'],
        "team_name": manager_entry['entry_name'],
        "live_total_points": live_total_points,
        "final_gw_points": final_gw_points,
        "players_played": players_played_count,
        "picks_data": picks_data
    }


async def get_manager_transfer_activity(session, manager_entry_id, gameweek):
    """Fetch transfer, chip, and cost info for a manager for the given gameweek."""

    async def fetch_data(refresh=False):
        t_task = fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_entry_id}/transfers/",
            cache_key=f"transfers_entry_{manager_entry_id}",
            cache_gw=gameweek,
            force_refresh=refresh
        )
        p_task = fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_entry_id}/event/{gameweek}/picks/",
            cache_key=f"picks_entry_{manager_entry_id}",
            cache_gw=gameweek,
            force_refresh=refresh
        )
        return await asyncio.gather(t_task, p_task)

    transfers_data, picks_data = await fetch_data(refresh=False)

    if transfers_data is None or picks_data is None:
        return None

    entry_history = picks_data.get("entry_history", {})
    transfers_made_count = entry_history.get("event_transfers", 0)
    transfers_this_week = [t for t in transfers_data if t.get("event") == gameweek]

    # If picks say transfers were made, but transfer history is empty, cache is likely stale.
    if transfers_made_count > 0 and not transfers_this_week:
        transfers_data, picks_data = await fetch_data(refresh=True)
        if transfers_data is None or picks_data is None:
            return None
        # Refresh derived data
        entry_history = picks_data.get("entry_history", {})
        transfers_this_week = [t for t in transfers_data if t.get("event") == gameweek]

    transfers_this_week.sort(key=lambda t: t.get("time", ""))

    chip = picks_data.get("active_chip")
    entry_history = picks_data.get("entry_history", {})
    transfer_cost = entry_history.get("event_transfers_cost", 0)

    return {
        "transfers": transfers_this_week,
        "chip": chip,
        "transfer_cost": transfer_cost
    }


# --- Aggregated League Caching Functions ---

async def get_league_picks_cached(session, league_id: int, gameweek: int,
                                   league_standings: dict = None,
                                   is_finished: bool = False,
                                   force_refresh: bool = False):
    """
    Fetches and caches ALL manager picks for a league in a single cache file.

    Returns: dict mapping manager_id (int) -> picks_data (dict)

    Args:
        session: aiohttp session
        league_id: FPL league ID
        gameweek: Current gameweek number
        league_standings: Pre-fetched standings data (optional, saves an API call)
        is_finished: Whether the gameweek is finished (uses permanent cache)
        force_refresh: Force refresh even if cache is valid
    """
    cache_path = CACHE_DIR / f"league_{league_id}_picks_gw{gameweek}.json"
    meta_path = cache_path.with_suffix('.meta.json')

    # Determine cache type based on GW status
    cache_type = 'finished_gw' if is_finished else 'league_picks'

    # Check if cache is valid
    if not force_refresh and await is_cache_fresh(meta_path, cache_type):
        cached = await load_cached_json(cache_path)
        if cached:
            # Convert string keys back to int (JSON doesn't support int keys)
            return {int(k): v for k, v in cached.items()}

    # Need to fetch - get standings if not provided
    if not league_standings:
        league_standings = await fetch_fpl_api(
            session,
            f"{BASE_API_URL}leagues-classic/{league_id}/standings/"
        )

    if not league_standings:
        return {}

    managers = league_standings.get('standings', {}).get('results', [])

    # Fetch all picks in parallel (rate limited by semaphore)
    async def fetch_manager_picks(manager):
        manager_id = manager['entry']
        picks = await fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/event/{gameweek}/picks/"
        )
        return manager_id, picks

    results = await asyncio.gather(*[fetch_manager_picks(m) for m in managers])

    all_picks = {manager_id: picks for manager_id, picks in results if picks}

    # Save to cache with timestamp
    await save_cache_with_meta(cache_path, all_picks, gameweek)

    return all_picks


async def get_league_history_cached(session, league_id: int, gameweek: int,
                                     league_standings: dict = None,
                                     is_finished: bool = False,
                                     force_refresh: bool = False):
    """
    Fetches and caches ALL manager history for a league in a single cache file.

    Returns: dict mapping manager_id (int) -> history_data (dict)

    Args:
        session: aiohttp session
        league_id: FPL league ID
        gameweek: Current gameweek number
        league_standings: Pre-fetched standings data (optional, saves an API call)
        is_finished: Whether the gameweek is finished (uses permanent cache)
        force_refresh: Force refresh even if cache is valid
    """
    cache_path = CACHE_DIR / f"league_{league_id}_history_gw{gameweek}.json"
    meta_path = cache_path.with_suffix('.meta.json')

    # Determine cache type based on GW status
    cache_type = 'finished_gw' if is_finished else 'league_history'

    # Check if cache is valid
    if not force_refresh and await is_cache_fresh(meta_path, cache_type):
        cached = await load_cached_json(cache_path)
        if cached:
            return {int(k): v for k, v in cached.items()}

    # Need to fetch - get standings if not provided
    if not league_standings:
        league_standings = await fetch_fpl_api(
            session,
            f"{BASE_API_URL}leagues-classic/{league_id}/standings/"
        )

    if not league_standings:
        return {}

    managers = league_standings.get('standings', {}).get('results', [])

    # Fetch all history in parallel
    async def fetch_manager_history(manager):
        manager_id = manager['entry']
        history = await fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/history/"
        )
        return manager_id, history

    results = await asyncio.gather(*[fetch_manager_history(m) for m in managers])

    all_history = {manager_id: history for manager_id, history in results if history}

    # Save to cache with timestamp
    await save_cache_with_meta(cache_path, all_history, gameweek)

    return all_history


async def cleanup_old_caches(current_gw: int, keep_last_n: int = 2):
    """
    Remove cache files from gameweeks more than keep_last_n behind current.
    Call this when gameweek changes.
    """
    import re

    min_gw_to_keep = max(1, current_gw - keep_last_n)

    for cache_file in CACHE_DIR.glob("*_gw*.json"):
        # Extract gameweek number from filename
        match = re.search(r'_gw(\d+)', cache_file.name)
        if match:
            file_gw = int(match.group(1))
            if file_gw < min_gw_to_keep:
                try:
                    cache_file.unlink()
                    logger.debug(f"Cleaned up old cache: {cache_file.name}")
                except OSError as e:
                    logger.warning(f"Failed to delete cache file {cache_file.name}: {e}")
