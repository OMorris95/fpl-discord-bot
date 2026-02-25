"""FPL live scoring computation for the Discord bot."""

from bot.logging_config import get_logger

logger = get_logger('api')


def predict_bonus(gw_fixtures):
    """Predict bonus points (3/2/1) for in-progress fixtures based on live BPS data.

    Uses the fixture stats array which provides per-fixture BPS.
    Only predicts for fixtures that have started but are not yet finished_provisional
    (once provisional, bonus is already baked into total_points by the FPL API).

    Returns dict of player_id -> predicted bonus (1, 2, or 3).
    """
    bonus_map = {}
    if not gw_fixtures:
        return bonus_map

    for fixture in gw_fixtures:
        if not fixture.get('started') or fixture.get('finished_provisional'):
            continue

        # Find BPS stat in fixture stats array
        bps_stat = None
        for stat in fixture.get('stats', []):
            if stat.get('identifier') == 'bps':
                bps_stat = stat
                break
        if not bps_stat:
            continue

        # Combine home and away players
        all_players = (bps_stat.get('h', []) or []) + (bps_stat.get('a', []) or [])
        if not all_players:
            continue

        # Sort by BPS descending
        all_players.sort(key=lambda p: p['value'], reverse=True)

        # Assign 3/2/1 bonus with tie-aware allocation
        rank = 1
        i = 0
        while i < len(all_players) and rank <= 3:
            current_bps = all_players[i]['value']
            if current_bps <= 0:
                break

            # Count players tied at this BPS
            tie_count = 0
            while i + tie_count < len(all_players) and all_players[i + tie_count]['value'] == current_bps:
                tie_count += 1

            # Bonus for this rank: rank 1→3, rank 2→2, rank 3→1
            bonus = max(0, 4 - rank)

            for j in range(tie_count):
                bonus_map[all_players[i + j]['element']] = bonus

            rank += tie_count
            i += tie_count

    return bonus_map


async def get_live_manager_details(session, manager_entry, current_gw, live_points_map, all_players_map, live_data,
                                    is_finished=False, cached_picks=None, cached_history=None):
    """Fetches picks/history for a manager and calculates their score, handling auto-subs for finished GWs.

    Args:
        session: aiohttp session (unused when cached data is provided)
        manager_entry: Manager dict from league standings (must have 'entry', 'player_name', 'entry_name')
        current_gw: Current gameweek number
        live_points_map: Dict mapping player_id -> stats dict
        all_players_map: Dict mapping player_id -> player info dict
        live_data: Live data dict with 'fixtures' key
        is_finished: Whether the gameweek is finished
        cached_picks: Dict mapping manager_id (int) -> picks data
        cached_history: Dict mapping manager_id (int) -> history data
    """
    manager_id = manager_entry['entry']

    if cached_picks is None or cached_history is None:
        logger.warning(f"No cached data provided for manager {manager_id}")
        return None

    picks_data = cached_picks.get(manager_id)
    history_data = cached_history.get(manager_id)

    if not picks_data:
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

        # Helper: check if ALL of a player's team's fixtures have finished (handles DGW)
        gw_fixtures = live_data.get('fixtures', [])

        # Predict bonus points for in-progress fixtures (BPS-based 3/2/1 allocation)
        bonus_predictions = predict_bonus(gw_fixtures)

        def has_team_finished(team_id):
            team_fixtures = [f for f in gw_fixtures if f['team_h'] == team_id or f['team_a'] == team_id]
            if not team_fixtures:
                return False
            return all(f.get('finished', False) for f in team_fixtures)

        # Determine captain status
        captain_pick = next((p for p in picks_data['picks'] if p['is_captain']), None)
        captain_played = True
        if captain_pick:
            captain_id = captain_pick['element']
            captain_minutes = live_points_map.get(captain_id, {}).get('minutes', 0)
            captain_team_id = all_players_map.get(captain_id, {}).get('team')

            # Captain hasn't played only if 0 minutes AND all their games are finished
            if captain_minutes == 0 and captain_team_id and has_team_finished(captain_team_id):
                captain_played = False

        if active_chip == 'bboost':
            scoring_picks = picks_data['picks']
        else:
            starters = [p for p in picks_data['picks'] if p['position'] <= 11]
            bench = sorted([p for p in picks_data['picks'] if p['position'] > 11], key=lambda x: x['position'])

            squad = list(starters)  # This is the list of players we will modify

            # 1. Substitute goalkeeper if needed
            starting_gk = next((p for p in squad if all_players_map[p['element']]['element_type'] == 1), None)
            if starting_gk:
                gk_team_id = all_players_map[starting_gk['element']]['team']
                gk_minutes = live_points_map.get(starting_gk['element'], {}).get('minutes', 0)
                if gk_minutes == 0 and has_team_finished(gk_team_id):
                    sub_gk = next((p for p in bench if all_players_map[p['element']]['element_type'] == 1), None)
                    if sub_gk and live_points_map.get(sub_gk['element'], {}).get('minutes', 0) > 0:
                        squad = [sub_gk if p == starting_gk else p for p in squad]

            # 2. Substitute outfield players
            for sub_in_player in bench:
                if all_players_map[sub_in_player['element']]['element_type'] == 1 or live_points_map.get(sub_in_player['element'], {}).get('minutes', 0) == 0:
                    continue

                # Find a starter to replace with this bench player
                for i, player_to_replace in enumerate(squad):
                    is_outfield = all_players_map[player_to_replace['element']]['element_type'] != 1
                    player_minutes = live_points_map.get(player_to_replace['element'], {}).get('minutes', 0)
                    player_team_id = all_players_map[player_to_replace['element']]['team']
                    # Only sub out if player has 0 minutes AND their game has finished
                    did_not_play = player_minutes == 0 and has_team_finished(player_team_id)

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
                            squad = potential_squad
                            break  # Sub successful, move to next bench player

            scoring_picks = squad

        # Calculate points from the determined scoring players
        for p in scoring_picks:
            player_stats = live_points_map.get(p['element'], {})
            player_points = player_stats.get('total_points', 0)
            # Only add predicted bonus if FPL hasn't already confirmed bonus for this player.
            # When bonus is confirmed, stats.bonus > 0 and total_points already includes it.
            if player_stats.get('bonus', 0) == 0:
                player_points += bonus_predictions.get(p['element'], 0)

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
    # Use entry_history from picks data (same logic as the website):
    # pre_gw_total = entry_history.total_points - entry_history.points
    # This is the official total BEFORE this GW's points are added
    entry_history = picks_data.get('entry_history', {})
    pre_gw_total = entry_history.get('total_points', 0) - entry_history.get('points', 0)

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
