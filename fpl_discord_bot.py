import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import os
import json
import time
from pathlib import Path
import asyncio
from dotenv import load_dotenv

# Load environment variables from .env file before importing modules that read env at import time
load_dotenv()

from bot.logging_config import get_logger

logger = get_logger('bot')

# Import from bot modules
from bot.database import (
    init_database, upsert_league_teams, get_fpl_id_for_user,
    get_linked_user_for_team, link_user_to_team, get_unclaimed_teams,
    get_all_teams_for_autocomplete, get_team_by_fpl_id, get_linked_users,
    get_all_league_teams, is_live_alert_subscribed, add_live_alert_subscription,
    remove_live_alert_subscription, get_all_live_alert_subscriptions,
    is_transfer_alert_subscribed, set_transfer_alert_subscription,
    get_auto_post_subscriptions, is_auto_post_enabled,
    set_auto_post_subscription, get_bot_state, set_bot_state,
    get_all_bot_state_keys,
    upsert_dm_subscription, get_dm_subscription, get_all_dm_subscriptions,
    delete_dm_subscription, update_dm_last_notified, update_dm_channel_id,
)
# Keep get_live_manager_details for live scoring computation (pure logic, no API calls when cached)
from bot.api import get_live_manager_details
from bot.backend_api import (
    get_bootstrap, get_live_data as backend_get_live_data,
    get_fixtures as backend_get_fixtures,
    get_league_standings, get_league_picks, get_league_history,
    get_league_transfers, get_manager_picks, get_manager_transfers,
    get_current_gameweek, get_last_completed_gameweek, get_gameweek_info,
    get_element_summary,
    FplUnavailableError,
    get_user_by_discord, get_deadline_info, get_injury_alerts,
    get_captain_suggestion, get_transfer_suggestions,
)
from bot.dm_features import (
    DMQueue, build_confirmation_embed, build_deadline_embed,
    build_injury_embed, build_transfer_embed,
)
from bot.image_generator import (
    generate_team_image, generate_dreamteam_image, format_manager_link,
    build_manager_url, generate_gw_summary_image, generate_recap_image,
    _format_short_name, generate_player_ownership_image,
    generate_fixtures_single_image, generate_fixtures_all_image,
)


# --- CONFIGURATION ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WEBSITE_URL = os.getenv("WEBSITE_URL", "http://192.168.1.109:5173")
BOT_LAUNCH_PUBLIC_COMMANDS_ONLY = os.getenv("BOT_LAUNCH_PUBLIC_COMMANDS_ONLY", "true").lower() == "true"
CONFIG_PATH = Path("config/league_config.json")

def load_league_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"guilds": {}, "channels": {}}

def save_league_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(league_config, f, indent=2)

league_config = load_league_config()

def set_league_mapping(scope: str, scope_id: int, league_id: int):
    key = "channels" if scope == "channel" else "guilds"
    league_config.setdefault(key, {})
    league_config[key][str(scope_id)] = {"league_id": str(league_id)}
    save_league_config()

def get_configured_league_id(channel_id: int | None, guild_id: int | None):
    if channel_id is not None:
        channel_entry = league_config.get("channels", {}).get(str(channel_id))
        if channel_entry and channel_entry.get("league_id"):
            return channel_entry["league_id"]
    if guild_id is not None:
        guild_entry = league_config.get("guilds", {}).get(str(guild_id))
        if guild_entry and guild_entry.get("league_id"):
            return guild_entry["league_id"]
    return None

async def ensure_league_id(interaction: discord.Interaction):
    league_id = get_configured_league_id(interaction.channel_id, getattr(interaction, "guild_id", None))
    if league_id:
        return league_id

    await interaction.followup.send(
        "No league is configured for this channel or server. "
        "An admin can set one with `/setleague`."
    )
    return None

def get_league_id_for_context(interaction: discord.Interaction):
    return get_configured_league_id(interaction.channel_id, getattr(interaction, "guild_id", None))

class FPLBot(commands.Bot):
    """A Discord bot for displaying FPL league and team information."""

    # Autocomplete cache settings
    AUTOCOMPLETE_CACHE_TTL = 300  # 5 minutes

    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.session = None
        self.last_known_goals = {}
        self.last_known_assists = {}
        self.last_known_red_cards = {}
        self.picks_cache = {}  # Cache for manager picks
        self.transfers_cache = {}  # Cache for manager transfers
        self.live_fpl_data = None  # In-memory cache for live GW data
        self._auto_posted = {}  # Tracks auto-posted GW events (loaded from DB on startup)
        # In-memory autocomplete cache to avoid excessive API calls
        self._autocomplete_cache = None
        self._autocomplete_cache_time = 0

    async def get_autocomplete_bootstrap(self):
        """Get bootstrap data for autocomplete with in-memory caching."""
        now = time.time()
        if self._autocomplete_cache and (now - self._autocomplete_cache_time) < self.AUTOCOMPLETE_CACHE_TTL:
            return self._autocomplete_cache

        data = await get_bootstrap(self.session)
        if data:
            self._autocomplete_cache = data
            self._autocomplete_cache_time = now
            logger.debug("Autocomplete cache refreshed")
        return data

    async def setup_hook(self):
        init_database()
        # Load persisted auto-post state
        for key in get_all_bot_state_keys("gw_"):
            self._auto_posted[key] = True
        self.session = aiohttp.ClientSession()
        self.dm_queue = DMQueue(self)
        self.live_data_loop.start()
        self.live_alert_loop.start()
        self.gw_state_loop.start()
        self.notification_loop.start()
        self.injury_check_loop.start()
        if BOT_LAUNCH_PUBLIC_COMMANDS_ONLY:
            self.tree.remove_command("notify")
        await self.tree.sync()
        logger.info(f"Synced slash commands for {self.user}.")

    @tasks.loop(seconds=60)
    async def live_data_loop(self):
        """Periodically fetches live FPL data for the current gameweek."""
        await self.wait_until_ready()
        try:
            bootstrap_data = await get_bootstrap(self.session)
            if not bootstrap_data or 'events' not in bootstrap_data:
                self.live_fpl_data = None
                return

            current_event = next((event for event in bootstrap_data['events'] if event['is_current']), None)

            if not current_event:
                if self.live_fpl_data is not None:
                    logger.debug("No current gameweek found. Clearing live data cache.")
                    self.live_fpl_data = None
                return

            current_gw = current_event['id']

            fixtures = await backend_get_fixtures(self.session)
            if not fixtures:
                self.live_fpl_data = None
                return

            # Filter to current GW fixtures
            gw_fixtures = [f for f in fixtures if f.get('event') == current_gw]
            live_fixtures = [f for f in gw_fixtures if f.get('started', False) and not f.get('finished_provisional', False)]

            if not live_fixtures:
                if self.live_fpl_data is not None:
                    logger.debug("No live fixtures. Clearing live data cache.")
                    self.live_fpl_data = None
                return

            live_data = await backend_get_live_data(self.session, current_gw)
            if live_data:
                live_data['gw'] = current_gw
                live_data['fixtures'] = gw_fixtures
                live_data['is_finished'] = current_event.get('finished', False) and current_event.get('data_checked', False)
                self.live_fpl_data = live_data
                logger.debug(f"Live data updated for GW {current_gw}. {len(live_fixtures)} fixture(s) in progress.")
            else:
                self.live_fpl_data = None
        except FplUnavailableError:
            logger.warning("FPL API unavailable during live data poll, keeping existing cache.")
        except Exception as e:
            logger.error(f"Error in live_data_loop: {e}", exc_info=True)
            self.live_fpl_data = None

    @tasks.loop(seconds=60)
    async def live_alert_loop(self):
        await self.wait_until_ready()

        try:
            live_data = self.live_fpl_data
            if not live_data:
                return  # Don't wipe caches — just skip this cycle

            current_gw = live_data.get('gw')
            if not current_gw:
                return

            # Initialize caches on GW change
            if self.last_known_goals.get("gw") != current_gw:
                logger.info(f"Initializing live alert caches for GW {current_gw}.")
                self.last_known_goals = {"gw": current_gw}
                self.last_known_assists = {"gw": current_gw}
                self.last_known_red_cards = {"gw": current_gw}
                for player_stats in live_data.get('elements', []):
                    pid = player_stats['id']
                    self.last_known_goals[pid] = player_stats['stats']['goals_scored']
                    self.last_known_assists[pid] = player_stats['stats']['assists']
                    self.last_known_red_cards[pid] = player_stats['stats']['red_cards']
                return

            try:
                bootstrap_data = await get_bootstrap(self.session)
            except FplUnavailableError:
                logger.warning("FPL unavailable during live alert check, skipping this cycle.")
                return
            if not bootstrap_data:
                return

            # Build lookup maps once
            all_players = {p['id']: p for p in bootstrap_data.get('elements', [])}
            all_teams = {t['id']: t for t in bootstrap_data.get('teams', [])}

            # Detect all new events in a single pass
            new_goal_events = []
            new_assist_events = []
            new_red_card_events = []

            for player_stats in live_data.get('elements', []):
                player_id = player_stats['id']
                stats = player_stats['stats']

                # Goals
                new_goals = stats['goals_scored']
                old_goals = self.last_known_goals.get(player_id, 0)
                if new_goals > old_goals:
                    self.last_known_goals[player_id] = new_goals
                    new_goal_events.append((player_id, new_goals - old_goals))

                # Assists
                new_assists = stats['assists']
                old_assists = self.last_known_assists.get(player_id, 0)
                if new_assists > old_assists:
                    self.last_known_assists[player_id] = new_assists
                    new_assist_events.append((player_id, new_assists - old_assists))

                # Red cards
                new_reds = stats['red_cards']
                old_reds = self.last_known_red_cards.get(player_id, 0)
                if new_reds > old_reds:
                    self.last_known_red_cards[player_id] = new_reds
                    new_red_card_events.append((player_id,))

            if not new_goal_events and not new_assist_events and not new_red_card_events:
                return

            # Log detected events
            if new_goal_events:
                names = [all_players.get(pid, {}).get('web_name', f'ID:{pid}') for pid, _ in new_goal_events]
                logger.info(f"Detected {len(new_goal_events)} new goal(s): {names}")
            if new_assist_events:
                names = [all_players.get(pid, {}).get('web_name', f'ID:{pid}') for pid, _ in new_assist_events]
                logger.info(f"Detected {len(new_assist_events)} new assist(s): {names}")
            if new_red_card_events:
                names = [all_players.get(pid, {}).get('web_name', f'ID:{pid}') for pid, in new_red_card_events]
                logger.info(f"Detected {len(new_red_card_events)} new red card(s): {names}")

            # Get all subscriptions once
            all_subs = await asyncio.to_thread(get_all_live_alert_subscriptions)
            if not all_subs:
                logger.debug("No live alert subscriptions found, skipping.")
                return

            logger.debug(f"Found {len(all_subs)} live alert subscription(s).")

            # Reset picks/transfers cache if gameweek changed
            if self.picks_cache.get('gw') != current_gw:
                self.picks_cache = {'gw': current_gw}
                self.transfers_cache = {'gw': current_gw}

            # Pre-fetch picks/transfers for all leagues we need
            for sub in all_subs:
                league_id = sub['league_id']
                channel = self.get_channel(int(sub['channel_id']))
                if not channel or not channel.guild:
                    logger.debug(f"Channel {sub['channel_id']} not found or no guild, skipping.")
                    continue

                cache_key = (league_id, channel.guild.id)
                if cache_key not in self.picks_cache:
                    self.picks_cache[cache_key] = {}
                    self.transfers_cache[cache_key] = {}
                    try:
                        linked_users = await asyncio.to_thread(get_linked_users, channel.guild.id, league_id)
                        logger.debug(f"Found {len(linked_users)} linked user(s) for league {league_id} in guild {channel.guild.id}.")
                        for user in linked_users:
                            try:
                                picks, transfers = await asyncio.gather(
                                    get_manager_picks(self.session, user['fpl_team_id'], current_gw),
                                    get_manager_transfers(self.session, user['fpl_team_id'])
                                )
                                if picks:
                                    self.picks_cache[cache_key][user['discord_user_id']] = picks
                                if transfers:
                                    self.transfers_cache[cache_key][user['discord_user_id']] = transfers
                            except FplUnavailableError:
                                logger.warning(f"FPL unavailable fetching picks for user {user['fpl_team_id']}, skipping.")
                            except Exception as e:
                                logger.warning(f"Failed to fetch data for user {user['fpl_team_id']}: {e}")
                    except Exception as e:
                        logger.warning(f"Failed to fetch linked users for league {league_id}: {e}")

            # --- Helper to resolve player context ---
            def _get_player_context(player_id):
                player_info = all_players.get(player_id)
                if not player_info:
                    return None
                team_id = player_info['team']
                fixture = next((f for f in live_data.get('fixtures', []) if f['team_h'] == team_id or f['team_a'] == team_id), None)
                if not fixture:
                    return None
                opponent_id = fixture['team_a'] if fixture['team_h'] == team_id else fixture['team_h']
                return {
                    'player': player_info,
                    'team': all_teams.get(team_id),
                    'opponent_name': all_teams.get(opponent_id, {}).get('name', 'Unknown'),
                }

            # --- Helper to find owners/benched/captains for a player in a channel ---
            def _find_managers(player_id, cache_key):
                owners, captains, triple_captains, benched = [], [], [], []
                for user_id, picks in self.picks_cache.get(cache_key, {}).items():
                    active_chip = picks.get('active_chip')
                    for pick in picks.get('picks', []):
                        if pick['element'] == player_id:
                            if pick['position'] <= 11:
                                if pick.get('is_captain'):
                                    if active_chip == '3xc':
                                        triple_captains.append(f"<@{user_id}>")
                                    else:
                                        captains.append(f"<@{user_id}>")
                                else:
                                    owners.append(f"<@{user_id}>")
                            else:
                                benched.append(f"<@{user_id}>")
                return owners, captains, triple_captains, benched

            # --- Build and send plain text alerts ---
            async def _broadcast_alert(event_type, player_id, ctx, all_subs):
                name = ctx['player']['web_name']
                opponent = ctx['opponent_name']

                for sub in all_subs:
                    channel = self.get_channel(int(sub['channel_id']))
                    if not channel or not channel.guild:
                        continue

                    transfer_alerts_on = sub['transfer_alerts_enabled']
                    league_id = sub['league_id']
                    cache_key = (league_id, channel.guild.id)

                    owners, captains, triple_captains, benched = _find_managers(player_id, cache_key)

                    transferors = []
                    if event_type == 'goal' and transfer_alerts_on:
                        for user_id, transfers in self.transfers_cache.get(cache_key, {}).items():
                            for transfer in [t for t in transfers if t.get('event') == current_gw]:
                                if transfer['element_out'] == player_id:
                                    transferors.append(f"<@{user_id}>")

                    if not (owners or captains or triple_captains or benched or transferors):
                        continue

                    lines = []

                    if event_type == 'goal':
                        lines.append(f"💥 **{name} scores against {opponent}** 💥")
                        if captains:
                            lines.append(f"Captained by {', '.join(captains)} 🤑")
                        if triple_captains:
                            lines.append(f"👑 **TRIPLE CAPTAINED** 👑 by {', '.join(triple_captains)}")
                        if owners:
                            lines.append(f"Owned by {', '.join(owners)}")
                        if transferors:
                            lines.append(f"Shouldn't have sold him {', '.join(transferors)} 🫵 🤣")
                        if benched:
                            lines.append(f"Benched by {', '.join(benched)} 🤡")

                    elif event_type == 'assist':
                        lines.append(f"🔥 **Assist for {name}** 🔥")
                        if triple_captains:
                            lines.append(f"👑 **TRIPLE CAPTAINED** 👑 by {', '.join(triple_captains)}")
                        if captains:
                            lines.append(f"Captained by {', '.join(captains)} 🤑")
                        if owners:
                            lines.append(f"Owned by {', '.join(owners)}")
                        if benched:
                            lines.append(f"Benched by {', '.join(benched)} 🤡")

                    elif event_type == 'red_card':
                        everyone = owners + captains + triple_captains
                        lines.append(f"🚨 **RED CARD {name}** 🚨 Not looking great for {', '.join(everyone)} 😬" if everyone else f"🚨 **RED CARD {name}** 🚨")
                        if benched:
                            lines.append(f"Lucky escape for {', '.join(benched)} 😅")

                    msg = "\n".join(lines)
                    try:
                        await channel.send(msg)
                    except discord.HTTPException as exc:
                        logger.warning(f"Failed to send alert to channel {channel.id}: {exc}")

            # Process goal events
            for player_id, goals_scored in new_goal_events:
                ctx = _get_player_context(player_id)
                if ctx:
                    await _broadcast_alert('goal', player_id, ctx, all_subs)

            # Process assist events
            for player_id, assists_count in new_assist_events:
                ctx = _get_player_context(player_id)
                if ctx:
                    await _broadcast_alert('assist', player_id, ctx, all_subs)

            # Process red card events
            for (player_id,) in new_red_card_events:
                ctx = _get_player_context(player_id)
                if ctx:
                    await _broadcast_alert('red_card', player_id, ctx, all_subs)

        except Exception as e:
            logger.error(f"Error in live_alert_loop: {e}", exc_info=True)

    @tasks.loop(seconds=60)
    async def gw_state_loop(self):
        """Detects GW start/finish transitions and auto-posts summaries."""
        await self.wait_until_ready()
        try:
            bootstrap_data = await get_bootstrap(self.session)
            if not bootstrap_data:
                return

            current_event = next((e for e in bootstrap_data.get('events', []) if e['is_current']), None)
            if not current_event:
                return

            gw = current_event['id']
            is_finished = current_event.get('finished', False)

            # GW Started detection
            started_key = f"gw_started_{gw}"
            if started_key not in self._auto_posted:
                self._auto_posted[started_key] = True
                set_bot_state(started_key, "1")
                logger.info(f"GW {gw} started — auto-posting GW summary")
                await self._auto_post_gw_summary(gw)

            # GW Finished detection
            finished_key = f"gw_finished_{gw}"
            if is_finished and finished_key not in self._auto_posted:
                self._auto_posted[finished_key] = True
                set_bot_state(finished_key, "1")
                logger.info(f"GW {gw} finished — auto-posting recap")
                await self._auto_post_recap(gw)

        except Exception as e:
            logger.error(f"Error in gw_state_loop: {e}", exc_info=True)

    async def _auto_post_gw_summary(self, gw):
        """Auto-post GW summary to subscribed channels."""
        subs = get_auto_post_subscriptions('gw')
        for sub in subs:
            try:
                channel = self.get_channel(int(sub['channel_id']))
                if not channel:
                    continue
                league_id = int(sub['league_id'])
                image_data = await self._build_gw_summary(gw, league_id)
                if image_data:
                    file = discord.File(image_data, filename="gw_summary.png")
                    await channel.send(content=f"**Gameweek {gw} has started!**", file=file)
            except Exception as e:
                logger.warning(f"Failed to auto-post GW summary to channel {sub['channel_id']}: {e}")

    async def _auto_post_recap(self, gw):
        """Auto-post GW recap to subscribed channels."""
        subs = get_auto_post_subscriptions('recap')
        for sub in subs:
            try:
                channel = self.get_channel(int(sub['channel_id']))
                if not channel:
                    continue
                league_id = int(sub['league_id'])
                image_data = await self._build_recap(gw, league_id)
                if image_data:
                    file = discord.File(image_data, filename="gw_recap.png")
                    await channel.send(content=f"**Gameweek {gw} Recap**", file=file)
            except Exception as e:
                logger.warning(f"Failed to auto-post recap to channel {sub['channel_id']}: {e}")

    async def _build_gw_summary(self, gw, league_id):
        """Build GW summary image data. Shared by /gw command and auto-post."""
        session = self.session
        bootstrap_data = await get_bootstrap(session)
        if not bootstrap_data:
            return None

        league_data = await get_league_standings(session, league_id)
        if not league_data:
            return None

        raw_picks, raw_transfers = await asyncio.gather(
            get_league_picks(session, league_id, gw),
            get_league_transfers(session, league_id, gw)
        )
        all_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}
        all_transfers = {int(k): v for k, v in (raw_transfers or {}).items() if str(k).isdigit()}
        all_players = {p['id']: p for p in bootstrap_data.get('elements', [])}
        all_teams = {t['id']: t for t in bootstrap_data.get('teams', [])}

        managers = league_data.get('standings', {}).get('results', [])

        # Group captains by player
        captain_groups = {}
        for manager in managers:
            mid = manager['entry']
            picks_data = all_picks.get(mid)
            if not picks_data:
                continue
            captain_pick = next((p for p in picks_data.get('picks', []) if p['is_captain']), None)
            if not captain_pick:
                continue
            pid = captain_pick['element']
            player = all_players.get(pid)
            if not player:
                continue
            if pid not in captain_groups:
                team = all_teams.get(player['team'], {})
                captain_groups[pid] = {
                    'player_name': player['web_name'],
                    'team_name': team.get('name', ''),
                    'managers': []
                }
            captain_groups[pid]['managers'].append(_format_short_name(manager['player_name']))

        captains_data = sorted(captain_groups.values(), key=lambda x: len(x['managers']), reverse=True)

        # Group transfers in/out by player
        transfers_in_groups = {}
        transfers_out_groups = {}
        for manager in managers:
            mid = manager['entry']
            transfer_info = all_transfers.get(mid, {})
            for t in transfer_info.get('transfers', []):
                # Transfer IN
                pin = t.get('element_in')
                player_in = all_players.get(pin)
                if player_in:
                    if pin not in transfers_in_groups:
                        team = all_teams.get(player_in['team'], {})
                        transfers_in_groups[pin] = {
                            'player_name': player_in['web_name'],
                            'team_name': team.get('name', ''),
                            'managers': []
                        }
                    transfers_in_groups[pin]['managers'].append(_format_short_name(manager['player_name']))
                # Transfer OUT
                pout = t.get('element_out')
                player_out = all_players.get(pout)
                if player_out:
                    if pout not in transfers_out_groups:
                        team = all_teams.get(player_out['team'], {})
                        transfers_out_groups[pout] = {
                            'player_name': player_out['web_name'],
                            'team_name': team.get('name', ''),
                            'managers': []
                        }
                    transfers_out_groups[pout]['managers'].append(_format_short_name(manager['player_name']))

        transfers_in_data = sorted(transfers_in_groups.values(), key=lambda x: len(x['managers']), reverse=True)[:6]
        transfers_out_data = sorted(transfers_out_groups.values(), key=lambda x: len(x['managers']), reverse=True)[:6]

        league_name = league_data.get('league', {}).get('name', 'League')
        return generate_gw_summary_image(gw, league_name, captains_data, transfers_in_data, transfers_out_data)

    async def _build_recap(self, gw, league_id):
        """Build GW recap image data. Shared by /recap command and auto-post."""
        session = self.session
        bootstrap_data = await get_bootstrap(session)
        if not bootstrap_data:
            return None

        live_data = await backend_get_live_data(session, gw)
        if not live_data:
            return None

        league_data = await get_league_standings(session, league_id)
        if not league_data:
            return None

        raw_picks, raw_transfers = await asyncio.gather(
            get_league_picks(session, league_id, gw),
            get_league_transfers(session, league_id, gw)
        )
        all_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}
        all_transfers = {int(k): v for k, v in (raw_transfers or {}).items() if str(k).isdigit()}
        all_players = {p['id']: p for p in bootstrap_data.get('elements', [])}
        live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}

        managers = league_data.get('standings', {}).get('results', [])
        league_name = league_data.get('league', {}).get('name', 'League')

        # Compute metrics for each manager (lists to capture all ties)
        shame = {'most_benched': [], 'worst_captain': [], 'transfer_flop': []}
        praise = {'highest_score': [], 'best_captain': [], 'best_transfer': []}

        for manager in managers:
            mid = manager['entry']
            mgr_name = manager['player_name']
            picks_data = all_picks.get(mid)
            if not picks_data:
                continue

            # GW score from entry_history
            entry_hist = picks_data.get('entry_history', {})
            gw_score = entry_hist.get('points', 0) - entry_hist.get('event_transfers_cost', 0)

            # Highest GW score (praise)
            cur = praise['highest_score']
            if not cur or gw_score > cur[0]['value']:
                praise['highest_score'] = [{'manager_name': mgr_name, 'value': gw_score}]
            elif gw_score == cur[0]['value']:
                cur.append({'manager_name': mgr_name, 'value': gw_score})

            # Captain analysis
            captain_pick = next((p for p in picks_data.get('picks', []) if p['is_captain']), None)
            if captain_pick:
                captain_pts = live_points_map.get(captain_pick['element'], {}).get('total_points', 0)
                captain_player = all_players.get(captain_pick['element'], {})
                captain_name = captain_player.get('web_name', '?')

                # Worst captain (shame) — lower is worse
                cur = shame['worst_captain']
                if not cur or captain_pts < cur[0]['value']:
                    shame['worst_captain'] = [{'manager_name': mgr_name, 'value': captain_pts, 'player_name': captain_name}]
                elif captain_pts == cur[0]['value']:
                    cur.append({'manager_name': mgr_name, 'value': captain_pts, 'player_name': captain_name})

                # Best captain (praise) — higher is better
                cur = praise['best_captain']
                if not cur or captain_pts > cur[0]['value']:
                    praise['best_captain'] = [{'manager_name': mgr_name, 'value': captain_pts, 'player_name': captain_name}]
                elif captain_pts == cur[0]['value']:
                    cur.append({'manager_name': mgr_name, 'value': captain_pts, 'player_name': captain_name})

            # Bench points (shame: most benched)
            bench_pts = sum(
                live_points_map.get(p['element'], {}).get('total_points', 0)
                for p in picks_data.get('picks', []) if p['position'] > 11
            )
            if bench_pts > 0:
                cur = shame['most_benched']
                if not cur or bench_pts > cur[0]['value']:
                    shame['most_benched'] = [{'manager_name': mgr_name, 'value': bench_pts}]
                elif bench_pts == cur[0]['value']:
                    cur.append({'manager_name': mgr_name, 'value': bench_pts})

            # Transfer analysis
            transfer_info = all_transfers.get(mid, {})
            for t in transfer_info.get('transfers', []):
                # Transfer flop (shame): highest points scored by player sold
                pout = t.get('element_out')
                out_pts = live_points_map.get(pout, {}).get('total_points', 0)
                out_player = all_players.get(pout, {})
                if out_pts > 0:
                    cur = shame['transfer_flop']
                    if not cur or out_pts > cur[0]['value']:
                        shame['transfer_flop'] = [{'manager_name': mgr_name, 'value': out_pts, 'player_name': out_player.get('web_name', '?')}]
                    elif out_pts == cur[0]['value']:
                        cur.append({'manager_name': mgr_name, 'value': out_pts, 'player_name': out_player.get('web_name', '?')})

                # Best transfer in (praise): highest points scored by player bought
                pin = t.get('element_in')
                in_pts = live_points_map.get(pin, {}).get('total_points', 0)
                in_player = all_players.get(pin, {})
                if in_pts > 0:
                    cur = praise['best_transfer']
                    if not cur or in_pts > cur[0]['value']:
                        praise['best_transfer'] = [{'manager_name': mgr_name, 'value': in_pts, 'player_name': in_player.get('web_name', '?')}]
                    elif in_pts == cur[0]['value']:
                        cur.append({'manager_name': mgr_name, 'value': in_pts, 'player_name': in_player.get('web_name', '?')})

        return generate_recap_image(gw, league_name, shame, praise)

    # =====================================================
    # DM NOTIFICATION LOOPS (Phase 5)
    # =====================================================

    @tasks.loop(seconds=60)
    async def notification_loop(self):
        """Send deadline reminders with captain/transfer suggestions to DM subscribers."""
        await self.wait_until_ready()
        try:
            info = await get_deadline_info(self.session)
            if not info or not info.get('next'):
                return

            next_gw = info['next']
            deadline_str = next_gw.get('deadline')
            if not deadline_str:
                return

            from datetime import datetime, timezone
            try:
                deadline = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                return

            now = datetime.now(timezone.utc)
            hours_left = (deadline - now).total_seconds() / 3600

            # Only act in specific windows
            window = None
            if 2.9 <= hours_left <= 3.1:
                window = '3h'
            elif 0.9 <= hours_left <= 1.1:
                window = '1h'

            if not window:
                return

            gw_num = next_gw['gameweek']
            state_key = f"deadline_{window}_gw{gw_num}"

            # Idempotency: skip if already sent
            if get_bot_state(state_key):
                return

            # Set state BEFORE sending (prevents duplicates on crash/restart)
            set_bot_state(state_key, '1')

            subs = get_all_dm_subscriptions()
            if not subs:
                return

            logger.info(f"Sending {window} deadline reminders for GW{gw_num} to {len(subs)} subscriber(s)")

            for sub in subs:
                try:
                    if not sub.get('deadline_reminder'):
                        continue

                    user_id = sub['discord_user_id']

                    # Verify premium_plus
                    user_data = await get_user_by_discord(self.session, user_id)
                    if not user_data or user_data.get('tier') != 'premium_plus':
                        continue

                    manager_id = sub['fpl_manager_id']
                    captain_data = None
                    transfer_data = None

                    if window == '3h':
                        # Fetch captain + transfer suggestions for 3h window
                        if sub.get('captain_suggestion'):
                            captain_data = await get_captain_suggestion(self.session, manager_id)
                        if sub.get('transfer_suggestion'):
                            transfer_data = await get_transfer_suggestions(self.session, manager_id)

                    embed = build_deadline_embed(info, captain_data, transfer_data)

                    self.dm_queue.enqueue(
                        user_id=int(user_id),
                        embed=embed,
                        dm_channel_id=sub.get('dm_channel_id'),
                        guild_id=sub.get('guild_id'),
                    )
                except Exception as e:
                    logger.error(f"Error preparing deadline DM for {sub.get('discord_user_id')}: {e}")

        except Exception as e:
            logger.error(f"Error in notification_loop: {e}", exc_info=True)

    @notification_loop.before_loop
    async def before_notification_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=30)
    async def injury_check_loop(self):
        """Check for injury status changes and DM subscribers."""
        await self.wait_until_ready()
        try:
            subs = get_all_dm_subscriptions()
            if not subs:
                return

            for sub in subs:
                try:
                    if not sub.get('injury_alerts'):
                        continue

                    user_id = sub['discord_user_id']

                    # Verify premium_plus
                    user_data = await get_user_by_discord(self.session, user_id)
                    if not user_data or user_data.get('tier') != 'premium_plus':
                        continue

                    manager_id = sub['fpl_manager_id']
                    result = await get_injury_alerts(self.session, manager_id)
                    if not result:
                        continue

                    alerts = result.get('alerts', [])
                    gw = result.get('gameweek', 0)

                    # Build current flagged set for change detection
                    current_set = frozenset(
                        f"{a['playerId']}:{a['status']}" for a in alerts
                    )

                    # Compare against last known state
                    state_key = f"injuries_{user_id}_{manager_id}"
                    last_state = get_bot_state(state_key)

                    current_state_str = ','.join(sorted(current_set)) if current_set else ''

                    if last_state == current_state_str:
                        continue  # No change

                    # State changed — update and notify
                    set_bot_state(state_key, current_state_str)

                    if not alerts:
                        continue  # Don't DM when all players become available

                    embed = build_injury_embed(alerts, gw)
                    self.dm_queue.enqueue(
                        user_id=int(user_id),
                        embed=embed,
                        dm_channel_id=sub.get('dm_channel_id'),
                        guild_id=sub.get('guild_id'),
                    )

                except Exception as e:
                    logger.error(f"Error checking injuries for {sub.get('discord_user_id')}: {e}")

        except Exception as e:
            logger.error(f"Error in injury_check_loop: {e}", exc_info=True)

    @injury_check_loop.before_loop
    async def before_injury_check_loop(self):
        await self.wait_until_ready()

    async def close(self):
        if self.session:
            await self.session.close()
        self.live_data_loop.cancel()
        self.live_alert_loop.cancel()
        self.gw_state_loop.cancel()
        self.notification_loop.cancel()
        self.injury_check_loop.cancel()
        await super().close()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="www.livefplstats.com"))
        logger.info("Bot is ready and online.")

bot = FPLBot()


# --- GLOBAL ERROR HANDLER ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global error handler for all slash commands."""

    # Handle already-responded interactions
    async def send_error(message: str, ephemeral: bool = True):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except discord.HTTPException:
            logger.warning(f"Failed to send error message to user: {message}")

    if isinstance(error, app_commands.MissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await send_error(f"You need the following permission(s) to use this command: `{missing}`")

    elif isinstance(error, app_commands.CommandOnCooldown):
        await send_error(f"This command is on cooldown. Try again in {error.retry_after:.1f} seconds.")

    elif isinstance(error, app_commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await send_error(f"I need the following permission(s) to run this command: `{missing}`")

    elif isinstance(error, app_commands.NoPrivateMessage):
        await send_error("This command can only be used in a server, not in DMs.")

    elif isinstance(error, app_commands.CheckFailure):
        await send_error("You don't have permission to use this command.")

    else:
        # Check if the underlying cause is FPL being unavailable
        original = getattr(error, 'original', None) or error.__cause__
        if isinstance(original, FplUnavailableError):
            await send_error("FPL is currently updating. Please try again shortly.", ephemeral=False)
            return

        # Log unexpected errors with full traceback
        command_name = interaction.command.name if interaction.command else "Unknown"
        logger.error(
            f"Unhandled error in command '{command_name}' "
            f"(User: {interaction.user}, Guild: {interaction.guild_id})",
            exc_info=error
        )
        await send_error("An unexpected error occurred. Please try again later.")


# --- DISCORD SLASH COMMANDS ---

@bot.tree.command(name="toggle_live_alerts", description="Enable or disable live match alerts (goals, assists, red cards) in this channel.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_live_alerts(interaction: discord.Interaction):
    """Toggles live match alerts for the current channel."""
    await interaction.response.defer(ephemeral=True)

    league_id = get_league_id_for_context(interaction)
    if not league_id:
        await interaction.followup.send("A league must be configured for this channel or server first. Use `/setleague`.")
        return

    channel_id = interaction.channel_id
    if await asyncio.to_thread(is_live_alert_subscribed, channel_id):
        await asyncio.to_thread(remove_live_alert_subscription, channel_id)
        await interaction.followup.send("🔴 Live match alerts disabled for this channel.")
    else:
        await asyncio.to_thread(add_live_alert_subscription, channel_id, league_id)
        await interaction.followup.send("🟢 Live match alerts enabled — goals, assists, and red cards will be posted when a linked manager owns the player.")

@bot.tree.command(name="toggle_transfer_alerts", description="Enable or disable transfer flop alerts in this channel.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_transfer_alerts(interaction: discord.Interaction):
    """Toggles transfer flop alerts for the current channel."""
    await interaction.response.defer(ephemeral=True)

    # This alert depends on live alerts being enabled first
    if not await asyncio.to_thread(is_live_alert_subscribed, interaction.channel_id):
        await interaction.followup.send("Live alerts must be enabled first with `/toggle_live_alerts` before you can enable this.", ephemeral=True)
        return

    is_subscribed = await asyncio.to_thread(is_transfer_alert_subscribed, interaction.channel_id)

    if is_subscribed:
        await asyncio.to_thread(set_transfer_alert_subscription, interaction.channel_id, False)
        await interaction.followup.send("🔴 Transfer flop alerts disabled for this channel.")
    else:
        await asyncio.to_thread(set_transfer_alert_subscription, interaction.channel_id, True)
        await interaction.followup.send("🟢 Transfer flop alerts enabled for this channel.")


@bot.tree.command(name="toggle_auto_gw", description="Toggle auto-posting of GW summary when a new gameweek starts.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_auto_gw(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    league_id = get_league_id_for_context(interaction)
    if not league_id:
        await interaction.followup.send("A league must be configured for this channel or server first. Use `/setleague`.", ephemeral=True)
        return
    # Ensure a subscription row exists (create with goal alerts off if needed)
    if not await asyncio.to_thread(is_live_alert_subscribed, interaction.channel_id):
        await asyncio.to_thread(add_live_alert_subscription, interaction.channel_id, league_id)
    enabled = await asyncio.to_thread(is_auto_post_enabled, interaction.channel_id, 'gw')
    await asyncio.to_thread(set_auto_post_subscription, interaction.channel_id, 'gw', not enabled)
    if enabled:
        await interaction.followup.send("🔴 Auto GW summary posting disabled for this channel.")
    else:
        await interaction.followup.send("🟢 Auto GW summary posting enabled — a summary image will be posted when each gameweek starts.")

@bot.tree.command(name="toggle_auto_recap", description="Toggle auto-posting of GW recap when a gameweek finishes.")
@app_commands.default_permissions(manage_channels=True)
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_auto_recap(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    league_id = get_league_id_for_context(interaction)
    if not league_id:
        await interaction.followup.send("A league must be configured for this channel or server first. Use `/setleague`.", ephemeral=True)
        return
    # Ensure a subscription row exists (create with goal alerts off if needed)
    if not await asyncio.to_thread(is_live_alert_subscribed, interaction.channel_id):
        await asyncio.to_thread(add_live_alert_subscription, interaction.channel_id, league_id)
    enabled = await asyncio.to_thread(is_auto_post_enabled, interaction.channel_id, 'recap')
    await asyncio.to_thread(set_auto_post_subscription, interaction.channel_id, 'recap', not enabled)
    if enabled:
        await interaction.followup.send("🔴 Auto GW recap posting disabled for this channel.")
    else:
        await interaction.followup.send("🟢 Auto GW recap posting enabled — a recap image will be posted when each gameweek finishes.")

@bot.tree.command(name="setleague", description="Configure which FPL league this server or channel uses.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(league_id="The FPL league ID (numbers only).",
                       scope="Apply this league to the whole server or just this channel.")
@app_commands.choices(scope=[
    app_commands.Choice(name="Server-wide", value="server"),
    app_commands.Choice(name="This channel only", value="channel")
])
async def setleague(interaction: discord.Interaction, league_id: int, scope: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild:
        await interaction.followup.send("This command can only be used inside a server.")
        return

    scope_value = scope.value
    permissions = interaction.user.guild_permissions
    # Check permissions: Allow Manage Guild/Channels, Administrator, or Server Owner
    is_server_owner = interaction.user.id == interaction.guild.owner_id
    base_perm = permissions.manage_guild if scope_value == "server" else permissions.manage_channels
    has_permission = base_perm or permissions.administrator or is_server_owner

    if not has_permission and not await interaction.client.is_owner(interaction.user):
        required = "Manage Server" if scope_value == "server" else "Manage Channels"
        await interaction.followup.send(f"You need the **{required}** permission to set the league in this scope.")
        return

    league_data = await get_league_standings(bot.session, league_id)

    if not league_data or "league" not in league_data:
        await interaction.followup.send("Could not verify that league ID. Please double-check the number and try again.")
        return

    target_id = interaction.guild_id if scope_value == "server" else interaction.channel_id
    set_league_mapping(scope_value, target_id, league_id)

    # --- New User Linking Logic ---
    standings_data = league_data.get('standings', {}).get('results', [])
    location = "this server" if scope_value == "server" else f"{interaction.channel.mention}"
    if standings_data:
        upsert_league_teams(league_id, standings_data)
        feedback_message = (
            f"League set to **{league_data['league']['name']}** ({league_id}) for {location}.\n"
            f"Found and synced **{len(standings_data)}** teams. Users can now use `/claim` to link their Discord account."
        )
    else:
        feedback_message = (
            f"League set to **{league_data['league']['name']}** ({league_id}) for {location}, "
            "but no teams were found in the standings."
        )

    await interaction.followup.send(feedback_message)

class AdminApprovalView(discord.ui.View):
    def __init__(self, fpl_team_id: int, new_user_id: int, guild_id: int):
        super().__init__(timeout=86400)  # 24 hours
        self.fpl_team_id = fpl_team_id
        self.new_user_id = new_user_id
        self.guild_id = guild_id

        # Create buttons with callbacks
        approve_button = discord.ui.Button(label="Approve Transfer", style=discord.ButtonStyle.green)
        approve_button.callback = self.approve_callback
        self.add_item(approve_button)

        deny_button = discord.ui.Button(label="Deny Request", style=discord.ButtonStyle.red)
        deny_button.callback = self.deny_callback
        self.add_item(deny_button)

    async def approve_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Use the new guild-aware linking function
        await asyncio.to_thread(link_user_to_team, self.guild_id, self.new_user_id, self.fpl_team_id)

        # Edit message
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.description = f"✅ Approved by {interaction.user.mention}"
        self.clear_items()  # Disable buttons
        await interaction.message.edit(embed=embed, view=self)

        # Notify user
        new_user = await interaction.client.fetch_user(self.new_user_id)
        team_data = await asyncio.to_thread(get_team_by_fpl_id, self.fpl_team_id)
        await new_user.send(f"Your claim for **{team_data['team_name']}** in the server **{interaction.guild.name}** was approved.")

    async def deny_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Edit message
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.description = f"⛔ Denied by {interaction.user.mention}"
        self.clear_items()  # Disable buttons
        await interaction.message.edit(embed=embed, view=self)

        # Notify user
        new_user = await interaction.client.fetch_user(self.new_user_id)
        team_data = await asyncio.to_thread(get_team_by_fpl_id, self.fpl_team_id)
        await new_user.send(f"Your claim for **{team_data['team_name']}** in the server **{interaction.guild.name}** was denied.")

@bot.tree.command(name="setadminchannel", description="Sets the channel for admin notifications.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(channel="The channel to be used for admin notifications.")
@app_commands.checks.has_permissions(manage_guild=True)
async def setadminchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Sets the admin channel for this server."""
    await interaction.response.defer(ephemeral=True)
    league_config.setdefault("admin_channels", {})
    league_config["admin_channels"][str(interaction.guild_id)] = channel.id
    save_league_config()
    await interaction.followup.send(f"Admin channel has been set to {channel.mention}.")

@bot.tree.command(name="claim", description="Claim your FPL team to link it to your Discord account for this server.")
@app_commands.describe(team="The FPL team you want to claim.")
async def claim(interaction: discord.Interaction, team: str):
    """Claim an FPL team and link it to your Discord account for this server."""
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    try:
        fpl_team_id = int(team)
    except ValueError:
        await interaction.followup.send("Invalid team selection. Please choose a team from the autocomplete list.", ephemeral=True)
        return

    user_id = interaction.user.id
    guild_id = interaction.guild_id

    # Check if team is in the configured league
    team_data = await asyncio.to_thread(get_team_by_fpl_id, fpl_team_id)
    if not team_data:
        await interaction.followup.send("That team could not be found. It might not be in the configured league.", ephemeral=True)
        return

    # Check if team is already claimed in this guild
    current_owner_id = await asyncio.to_thread(get_linked_user_for_team, guild_id, fpl_team_id)

    if current_owner_id is None:
        # Team is unclaimed in this guild, link it
        await asyncio.to_thread(link_user_to_team, guild_id, user_id, fpl_team_id)
        await interaction.followup.send(f"✅ Success! You have been linked to **{team_data['team_name']}** for this server.", ephemeral=True)
    else:
        # Team is claimed by someone else, send for admin approval
        if int(current_owner_id) == user_id:
            await interaction.followup.send(f"You have already claimed **{team_data['team_name']}** in this server.", ephemeral=True)
            return

        admin_channel_id = league_config.get("admin_channels", {}).get(str(interaction.guild_id))
        if not admin_channel_id:
            await interaction.followup.send("⚠️ That team is already linked to another user, but no admin channel is configured for this server to handle the conflict.", ephemeral=True)
            return

        admin_channel = bot.get_channel(int(admin_channel_id))
        if not admin_channel:
            await interaction.followup.send("⚠️ The configured admin channel could not be found.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🚨 Claim Conflict",
            description=f"<@{user_id}> wants to claim **{team_data['team_name']}**.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Currently Owned By", value=f"<@{current_owner_id}>", inline=False)
        embed.add_field(name="FPL Team ID", value=str(fpl_team_id), inline=False)

        view = AdminApprovalView(fpl_team_id, user_id, guild_id)
        await admin_channel.send(embed=embed, view=view)
        await interaction.followup.send("⚠️ That team is already linked to another user. An admin approval request has been sent.", ephemeral=True)

@claim.autocomplete('team')
async def claim_autocomplete(interaction: discord.Interaction, current: str):
    league_id = get_league_id_for_context(interaction)
    if not league_id or not interaction.guild_id:
        return []
    
    unclaimed_teams = await asyncio.to_thread(get_unclaimed_teams, league_id, interaction.guild_id, current)
    
    choices = [
        app_commands.Choice(name=f"{team_name} ({manager_name})", value=str(fpl_team_id))
        for fpl_team_id, team_name, manager_name in unclaimed_teams
    ]
    return choices

@bot.tree.command(name="assign", description="Manually assign an FPL team to a Discord user.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(user="The Discord user to assign the team to.", team="The FPL team to assign.")
@app_commands.checks.has_permissions(manage_guild=True)
async def assign(interaction: discord.Interaction, user: discord.User, team: str):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild_id:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    try:
        fpl_team_id = int(team)
    except ValueError:
        await interaction.followup.send("Invalid team selection. Please choose a team from the autocomplete list.", ephemeral=True)
        return
    
    # Use the new guild-aware linking function
    await asyncio.to_thread(link_user_to_team, interaction.guild_id, user.id, fpl_team_id)

    team_data = await asyncio.to_thread(get_team_by_fpl_id, fpl_team_id)
    
    await interaction.followup.send(f"✅ Manually linked {user.mention} to **{team_data['team_name']}** in this server.")

@assign.autocomplete('team')
async def assign_autocomplete(interaction: discord.Interaction, current: str):
    league_id = get_league_id_for_context(interaction)
    if not league_id:
        return []
    
    all_teams = await asyncio.to_thread(get_all_teams_for_autocomplete, league_id, current)
    
    choices = [
        app_commands.Choice(name=f"{team_name} ({manager_name})", value=str(fpl_team_id))
        for fpl_team_id, team_name, manager_name in all_teams
    ]
    return choices

@bot.tree.command(name="team", description="Generates an image of a manager's current FPL team.")
@app_commands.describe(manager="Select the manager's team to view. Leave blank to view your own.")
async def team(interaction: discord.Interaction, manager: str = None):
    await interaction.response.defer()

    manager_id = None
    if manager:
        try:
            manager_id = int(manager)
        except ValueError:
            await interaction.followup.send("Invalid team selection. Please choose a team from the autocomplete list.", ephemeral=True)
            return
    else:
        if not interaction.guild_id:
            await interaction.followup.send("This command must be used in a server to find your team.", ephemeral=True)
            return
        # If no manager is specified, try to get the user's claimed team in this server
        fpl_id = await asyncio.to_thread(get_fpl_id_for_user, interaction.guild_id, interaction.user.id)
        if fpl_id:
            manager_id = fpl_id
        else:
            await interaction.followup.send("You have not claimed a team in this server. Please use `/claim` first, or specify a manager.", ephemeral=True)
            return

    if not manager_id:
        await interaction.followup.send("Could not determine which team to display.", ephemeral=True)
        return
    
    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    # --- Gameweek and Data determination ---
    bootstrap_data = await get_bootstrap(session)
    if not bootstrap_data:
        await interaction.followup.send("Could not fetch FPL bootstrap data.")
        return

    gw_info = await get_gameweek_info(session, bootstrap_data)
    if not gw_info:
        await interaction.followup.send("Could not determine the current or last gameweek.")
        return

    gw_event = gw_info['event']
    current_gw = gw_info['gw']
    is_finished = gw_info['is_finished']

    # Try to use the live cache if it's for the correct gameweek
    live_data = bot.live_fpl_data
    if not live_data or live_data.get('gw') != current_gw:
        live_data = await backend_get_live_data(session, current_gw)
        if live_data:
            live_data['gw'] = current_gw
            # Attach fixtures so unstarted games show fixture text instead of 0 pts
            fixtures = await backend_get_fixtures(session)
            if fixtures:
                live_data['fixtures'] = [f for f in fixtures if f.get('event') == current_gw]

    if not live_data:
        await interaction.followup.send(f"Could not fetch data for Gameweek {current_gw}.")
        return

    # --- Fetch league data ---
    league_data = await get_league_standings(session, int(league_id))
    if not league_data:
        await interaction.followup.send("Failed to fetch FPL league data.")
        return

    standings_results = league_data.get('standings', {}).get('results', [])

    # For settled GWs, mirror the website exactly: use official standings totals.
    # For settled GWs, still run through get_live_manager_details so scoring_picks
    # is populated from official automatic_subs for correct image rendering.
    if is_finished:
        raw_picks = await get_league_picks(session, int(league_id), current_gw)
        cached_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}

        live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}
        all_players_map = {p['id']: p for p in bootstrap_data.get('elements', [])}

        tasks = [
            get_live_manager_details(
                session, manager, current_gw, live_points_map, all_players_map, live_data,
                is_finished=is_finished, cached_picks=cached_picks, cached_history={}
            )
            for manager in standings_results
        ]
        manager_details = [res for res in await asyncio.gather(*tasks) if res]

        official_totals = {
            manager['entry']: {
                'final_gw_points': manager.get('event_total', 0),
                'live_total_points': manager.get('total', 0),
            }
            for manager in standings_results
        }
        for manager in manager_details:
            official = official_totals.get(manager['id'])
            if official:
                manager['final_gw_points'] = official['final_gw_points']
                manager['live_total_points'] = official['live_total_points']
    else:
        # --- Fetch cached picks and history for all managers ---
        raw_picks, raw_history = await asyncio.gather(
            get_league_picks(session, int(league_id), current_gw),
            get_league_history(session, int(league_id))
        )
        cached_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}
        cached_history = {int(k): v for k, v in (raw_history or {}).items() if str(k).isdigit()}

        # --- Process and Display ---
        live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}
        all_players_map = {p['id']: p for p in bootstrap_data.get('elements', [])}

        tasks = [
            get_live_manager_details(
                session, manager, current_gw, live_points_map, all_players_map, live_data,
                is_finished=is_finished, cached_picks=cached_picks, cached_history=cached_history
            )
            for manager in standings_results
        ]
        manager_details = [res for res in await asyncio.gather(*tasks) if res]

        manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

        prev_rank_map = {}
        for entry in standings_results:
            prev_rank_map[entry['entry']] = entry.get('last_rank', 0)

        for manager in manager_details:
            manager['prev_rank'] = prev_rank_map.get(manager['id'], 0)

    selected_manager = next((m for m in manager_details if m['id'] == manager_id), None)
    if not selected_manager:
        await interaction.followup.send("Could not find that manager in the configured league.", ephemeral=True)
        return

    picks_data = selected_manager.get('picks_data') or {}
    if not picks_data.get('picks'):
        await interaction.followup.send("Could not fetch that manager's picks for this gameweek.", ephemeral=True)
        return

    fpl_data = {
        'bootstrap': bootstrap_data,
        'live': live_data,
        'picks': picks_data,
    }
    summary_data = {
        'team_name': selected_manager['team_name'],
        'gw_points': selected_manager['final_gw_points'],
        'total_points': selected_manager['live_total_points'],
    }

    image_data = await asyncio.to_thread(generate_team_image, fpl_data, summary_data, is_finished)

    if image_data:
        file = discord.File(image_data, filename="fpl_team.png")
        manager_url = build_manager_url(manager_id, current_gw, WEBSITE_URL)
        link_text = f"[View manager stats at LiveFPLStats](<{manager_url}>)"
        await interaction.followup.send(content=link_text, file=file, suppress_embeds=True)
    else:
        await interaction.followup.send("Failed to generate team image.", ephemeral=True)


@team.autocomplete('manager')
async def team_autocomplete(interaction: discord.Interaction, current: str):
    league_id = get_league_id_for_context(interaction)
    if not league_id:
        return []

    all_teams = await asyncio.to_thread(get_all_teams_for_autocomplete, league_id, current)

    choices = [
        app_commands.Choice(name=f"{team_name} ({manager_name})", value=str(fpl_team_id))
        for fpl_team_id, team_name, manager_name in all_teams
    ]
    return choices

@bot.tree.command(name="table", description="Displays the live FPL league table.")
async def table(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    bootstrap_data = await get_bootstrap(session)
    if not bootstrap_data:
        await interaction.followup.send("Could not fetch FPL bootstrap data.")
        return

    gw_info = await get_gameweek_info(session, bootstrap_data)
    if not gw_info:
        await interaction.followup.send("Could not determine the current or last gameweek.")
        return

    current_gw = gw_info['gw']
    is_finished = gw_info['is_finished']

    live_data = bot.live_fpl_data
    if not live_data or live_data.get('gw') != current_gw:
        live_data = await backend_get_live_data(session, current_gw)
        if live_data:
            live_data['gw'] = current_gw

    if not live_data:
        await interaction.followup.send(f"Could not fetch data for Gameweek {current_gw}.")
        return

    league_data = await get_league_standings(session, int(league_id))
    if not league_data:
        await interaction.followup.send("Failed to fetch FPL league data.")
        return

    standings_results = league_data.get('standings', {}).get('results', [])

    if is_finished:
        raw_picks = await get_league_picks(session, int(league_id), current_gw)
        cached_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}

        manager_details = []
        for manager in standings_results:
            picks_data = cached_picks.get(manager['entry']) or {}
            manager_details.append({
                'id': manager['entry'],
                'name': manager['player_name'],
                'team_name': manager['entry_name'],
                'live_total_points': manager.get('total', 0),
                'final_gw_points': manager.get('event_total', 0),
                'players_played': 0,
                'picks_data': {
                    'active_chip': picks_data.get('active_chip')
                },
                'prev_rank': manager.get('last_rank', 0),
            })
    else:
        raw_picks, raw_history = await asyncio.gather(
            get_league_picks(session, int(league_id), current_gw),
            get_league_history(session, int(league_id))
        )
        cached_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}
        cached_history = {int(k): v for k, v in (raw_history or {}).items() if str(k).isdigit()}

        live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}
        all_players_map = {p['id']: p for p in bootstrap_data.get('elements', [])}

        tasks = [
            get_live_manager_details(
                session, manager, current_gw, live_points_map, all_players_map, live_data,
                is_finished=is_finished, cached_picks=cached_picks, cached_history=cached_history
            )
            for manager in standings_results
        ]
        manager_details = [res for res in await asyncio.gather(*tasks) if res]
        manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

        prev_rank_map = {}
        for entry in standings_results:
            prev_rank_map[entry['entry']] = entry.get('last_rank', 0)

        for manager in manager_details:
            manager['prev_rank'] = prev_rank_map.get(manager['id'], 0)

    TABLE_LIMIT = 25

    from bot.image_generator import generate_league_table_image

    table_image = generate_league_table_image(
        league_name=league_data['league']['name'],
        current_gw=current_gw,
        managers=manager_details[:TABLE_LIMIT],
        website_url=WEBSITE_URL
    )

    if table_image:
        import discord
        file = discord.File(table_image, filename="league_table.png")
        link_text = f"[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league?{league_id}>)"
        await interaction.followup.send(content=link_text, file=file)
    else:
        await _send_text_table(interaction, league_data, manager_details[:TABLE_LIMIT], current_gw, league_id)

async def _send_text_table(interaction, league_data, manager_details, current_gw, league_id):
    """Fallback text-based table if image generation fails."""
    def format_name(name):
        parts = name.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}. {parts[-1]}"
        return name

    processed = []
    for i, m in enumerate(manager_details):
        processed.append({
            'rank': i + 1,
            'name': format_name(m['name']),
            'total': m['live_total_points'],
            'gw': m['final_gw_points']
        })

    max_len = max((len(m['name']) for m in processed), default=10)
    lines = ["```"]
    lines.append(f"{'#':<3} {'Manager'.ljust(max_len)}  {'GW':>4}  {'Total':>6}")
    lines.append("-" * (max_len + 18))
    for m in processed:
        lines.append(f"{str(m['rank']):<3} {m['name'].ljust(max_len)}  {m['gw']:>4}  {m['total']:>6}")
    lines.append("```")
    lines.append(f"[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league?{league_id}>)")
    await interaction.followup.send("\n".join(lines))


@bot.tree.command(name="player", description="Shows which managers in the league own a specific player.")
@app_commands.describe(player="Select the player to check ownership for.")
async def player(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    try:
        player_id = int(player)
    except ValueError:
        await interaction.followup.send("Invalid player selection. Please choose a player from the autocomplete list.", ephemeral=True)
        return

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    # Fetch bootstrap, league data, and element summary in parallel
    bootstrap_data, league_data, element_summary = await asyncio.gather(
        get_bootstrap(session),
        get_league_standings(session, int(league_id)),
        get_element_summary(session, player_id)
    )

    if not bootstrap_data or not league_data:
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    all_players = {p['id']: p for p in bootstrap_data.get('elements', [])}
    teams_map = {t['id']: t for t in bootstrap_data.get('teams', [])}
    selected_player = all_players.get(player_id)

    if not selected_player:
        await interaction.followup.send("Player not found.")
        return

    # Use backend league picks (DB-cached)
    raw_picks = await get_league_picks(session, int(league_id), current_gw)
    all_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}

    owners = []
    benched = []
    managers = league_data['standings']['results']
    for manager in managers:
        manager_id = manager['entry']
        picks_data = all_picks.get(manager_id)
        if picks_data and 'picks' in picks_data:
            manager_name = manager['player_name']
            for pick in picks_data['picks']:
                if pick['element'] == player_id:
                    if pick['position'] > 11:
                        benched.append(manager_name)
                    else:
                        owners.append(manager_name)
                    break

    # Extract last 5 GW history (aggregate DGW points, detect BGW)
    gw_history = []
    if element_summary and 'history' in element_summary:
        # Only consider fully finished GWs
        completed_gws = sorted(
            e['id'] for e in bootstrap_data.get('events', []) if e.get('finished')
        )
        finished_set = set(completed_gws)

        # Aggregate by round — DGW has multiple entries per round
        # Exclude current/unfinished GW so a 0-pt entry doesn't appear
        round_agg = {}
        for entry in element_summary['history']:
            rnd = entry.get('round')
            if rnd not in finished_set:
                continue
            if rnd not in round_agg:
                round_agg[rnd] = {'round': rnd, 'total_points': 0}
            round_agg[rnd]['total_points'] += entry.get('total_points', 0)
        last_5_gws = completed_gws[-5:] if completed_gws else []

        # Detect BGW — team had no fixture in that GW
        fixtures = await backend_get_fixtures(session)
        player_team_id = selected_player.get('team')
        team_fixture_gws = set()
        if fixtures:
            for f in fixtures:
                if f.get('team_h') == player_team_id or f.get('team_a') == player_team_id:
                    if f.get('event'):
                        team_fixture_gws.add(f['event'])

        for gw in last_5_gws:
            if gw in round_agg:
                gw_history.append(round_agg[gw])
            elif gw not in team_fixture_gws:
                gw_history.append({'round': gw, 'is_bgw': True})
            else:
                # Team had fixture but player wasn't in squad
                gw_history.append({'round': gw, 'total_points': 0})

    team_info = teams_map.get(selected_player.get('team'), {})

    image_data = await asyncio.to_thread(
        generate_player_ownership_image,
        selected_player, team_info, current_gw,
        gw_history, owners, benched
    )

    if image_data:
        file = discord.File(image_data, filename="player_ownership.png")
        link_text = f"[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league?{league_id}>)"
        await interaction.followup.send(content=link_text, file=file)
    else:
        await interaction.followup.send("Failed to generate player image.", ephemeral=True)

@player.autocomplete('player')
async def player_autocomplete(interaction: discord.Interaction, current: str):
    # Use in-memory cached bootstrap data for performance
    bootstrap_data = await bot.get_autocomplete_bootstrap()
    if not bootstrap_data:
        return []

    all_players = bootstrap_data.get('elements', [])
    choices = []
    current_lower = current.lower()

    for player in all_players:
        full_name = f"{player['first_name']} {player['second_name']}"
        web_name = player['web_name']
        if current_lower in full_name.lower() or current_lower in web_name.lower():
            display_name = f"{full_name} ({web_name})"
            choices.append(app_commands.Choice(name=display_name, value=str(player['id'])))

    return sorted(choices, key=lambda x: x.name)[:25]

def find_optimal_dreamteam(all_squad_players):
    """Find the optimal 11 players following FPL formation rules with tie-breaking."""
    # Separate players by position
    goalkeepers = []
    defenders = []
    midfielders = []
    forwards = []
    
    for player_id, player_data in all_squad_players.items():
        element_type = player_data['element_type']
        # Create sorting key: points (desc), goals (desc), assists (desc), minutes (desc)
        sort_key = (-player_data['points'], -player_data['goals'], -player_data['assists'], -player_data['minutes'])
        
        if element_type == 1:  # GK
            goalkeepers.append((player_id, sort_key))
        elif element_type == 2:  # DEF
            defenders.append((player_id, sort_key))
        elif element_type == 3:  # MID
            midfielders.append((player_id, sort_key))
        elif element_type == 4:  # FWD
            forwards.append((player_id, sort_key))
    
    # Sort each position by the tie-breaking criteria
    goalkeepers.sort(key=lambda x: x[1])
    defenders.sort(key=lambda x: x[1])
    midfielders.sort(key=lambda x: x[1])
    forwards.sort(key=lambda x: x[1])
    
    # Must have at least 1 GK, 3 DEF, 3 MID, 1 FWD
    if (len(goalkeepers) < 1 or len(defenders) < 3 or 
        len(midfielders) < 3 or len(forwards) < 1):
        return None, None
    
    # Try all valid formations and find the one with highest total points
    best_team = None
    best_points = -1
    best_formation = None
    
    # Valid formations: (def_count, mid_count, fwd_count)
    # Must sum to 10 (plus 1 GK = 11 total)
    valid_formations = [
        (3, 5, 2), (3, 4, 3), (4, 5, 1), (4, 4, 2), (4, 3, 3), (5, 4, 1), (5, 3, 2)
    ]
    
    for def_count, mid_count, fwd_count in valid_formations:
        # Check if we have enough players for this formation
        if (def_count <= len(defenders) and 
            mid_count <= len(midfielders) and 
            fwd_count <= len(forwards)):
            
            # Build team for this formation
            team = []
            team.append(goalkeepers[0][0])  # Best GK
            
            # Add best players for each position
            for i in range(def_count):
                team.append(defenders[i][0])
            for i in range(mid_count):
                team.append(midfielders[i][0])
            for i in range(fwd_count):
                team.append(forwards[i][0])
            
            # Calculate total points for this formation
            total_points = sum(all_squad_players[pid]['points'] for pid in team)
            
            if total_points > best_points:
                best_points = total_points
                best_team = team
                best_formation = f"{def_count}-{mid_count}-{fwd_count}"
    
    return best_team, best_formation

@bot.tree.command(name="dreamteam", description="Shows the optimal XI from the league for the most recent completed gameweek.")
async def dreamteam(interaction: discord.Interaction):
    await interaction.response.defer()
    
    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    last_completed_gw = await get_last_completed_gameweek(session)
    if not last_completed_gw:
        await interaction.followup.send("Could not determine the last completed gameweek.")
        return

    # Fetch required data
    bootstrap_data, league_data, completed_gw_data = await asyncio.gather(
        get_bootstrap(session),
        get_league_standings(session, int(league_id)),
        backend_get_live_data(session, last_completed_gw)
    )

    if not all([bootstrap_data, league_data, completed_gw_data]):
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    all_players = {p['id']: p for p in bootstrap_data.get('elements', [])}
    completed_gw_stats = {p['id']: p['stats'] for p in completed_gw_data['elements']}

    # Use backend league picks (DB-cached)
    raw_picks = await get_league_picks(session, int(league_id), last_completed_gw)
    all_picks = {int(k): v for k, v in (raw_picks or {}).items() if str(k).isdigit()}

    # Get all unique players from all managers' squads for the completed gameweek
    all_squad_players = {}
    for manager_id, picks_data in all_picks.items():
        if picks_data and 'picks' in picks_data:
            for pick in picks_data['picks']:
                player_id = pick['element']
                if player_id not in all_squad_players:
                    player_stats = completed_gw_stats.get(player_id, {})
                    all_squad_players[player_id] = {
                        'id': player_id,
                        'element_type': all_players[player_id]['element_type'],
                        'points': player_stats.get('total_points', 0),
                        'goals': player_stats.get('goals_scored', 0),
                        'assists': player_stats.get('assists', 0),
                        'minutes': player_stats.get('minutes', 0),
                        'player_info': all_players[player_id]
                    }
    
    # Find optimal formation and team
    optimal_team, best_formation = find_optimal_dreamteam(all_squad_players)
    if not optimal_team:
        await interaction.followup.send("Could not create dream team - insufficient players in each position.")
        return
    
    # Calculate total points and find player of the week
    total_points = sum(all_squad_players[pid]['points'] for pid in optimal_team)
    player_of_week = max([all_squad_players[pid] for pid in optimal_team], 
                       key=lambda x: (x['points'], x['goals'], x['assists'], x['minutes']))
    
    # Create mock picks data for image generation
    dream_picks = []
    for i, player_id in enumerate(optimal_team):
        dream_picks.append({
            'element': player_id,
            'position': i + 1,
            'multiplier': 1,
            'is_captain': False,
            'is_vice_captain': False
        })
    
    # Prepare data for image generation
    summary_data = {
        "formation": best_formation,
        "total_points": total_points,
        "gameweek": last_completed_gw,
        "player_of_week": player_of_week,
        "league_name": league_data['league']['name']
    }

    fpl_data_for_image = {
        "bootstrap": bootstrap_data,
        "live": completed_gw_data,
        "picks": {"picks": dream_picks}
    }
    
    # Generate image
    image_bytes = await asyncio.to_thread(generate_dreamteam_image, fpl_data_for_image, summary_data)
    if image_bytes:
        file = discord.File(fp=image_bytes, filename="fpl_dreamteam.png")
        await interaction.followup.send(
            f"🌟 **Dream Team for GW {last_completed_gw}** 🌟\n[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league>)", file=file
        )
    else:
        await interaction.followup.send("Sorry, there was an error creating the dream team image.")

@bot.tree.command(name="gw", description="Shows captain choices and transfers for the current gameweek as an image.")
async def gw(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    image_data = await bot._build_gw_summary(current_gw, int(league_id))
    if image_data:
        file = discord.File(image_data, filename="gw_summary.png")
        link_text = f"[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league?{league_id}>)"
        await interaction.followup.send(content=link_text, file=file)
    else:
        await interaction.followup.send(f"Could not generate GW {current_gw} summary.")

@bot.tree.command(name="fixtures", description="Shows the upcoming fixtures for a team or all teams.")
@app_commands.describe(team="The team to show fixtures for. Leave blank for all teams.")
async def fixtures(interaction: discord.Interaction, team: str = None):
    await interaction.response.defer()

    session = bot.session
    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    bootstrap_data, fixtures_data = await asyncio.gather(
        get_bootstrap(session),
        backend_get_fixtures(session)
    )

    if not bootstrap_data or not fixtures_data:
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    teams_map = {t['id']: t for t in bootstrap_data.get('teams', [])}

    team_id_to_show = int(team) if team else None

    if team_id_to_show:
        # Specific team: show next 10 fixtures (excluding current live GW)
        next_gw = current_gw + 1
        team_upcoming = [
            f for f in fixtures_data
            if f.get('event') and f['event'] >= next_gw
               and (f['team_h'] == team_id_to_show or f['team_a'] == team_id_to_show)
        ]
        team_upcoming.sort(key=lambda x: x['event'])
        team_upcoming = team_upcoming[:10]

        if not team_upcoming:
            await interaction.followup.send("No upcoming fixtures found for this team.")
            return

        # Build structured fixture list (handles DGWs naturally)
        team_fixture_gws = {f['event'] for f in team_upcoming}
        min_gw = next_gw
        max_gw = team_upcoming[-1]['event']

        fixture_rows = []
        fixture_idx = 0
        for gw in range(min_gw, max_gw + 1):
            if gw in team_fixture_gws:
                # Could be multiple fixtures in same GW (DGW)
                while fixture_idx < len(team_upcoming) and team_upcoming[fixture_idx]['event'] == gw:
                    f = team_upcoming[fixture_idx]
                    is_home = f['team_h'] == team_id_to_show
                    opponent_id = f['team_a'] if is_home else f['team_h']
                    fixture_rows.append({
                        'gw': gw,
                        'opponent': teams_map[opponent_id]['name'],
                        'is_home': is_home,
                        'fdr': f['team_h_difficulty'] if is_home else f['team_a_difficulty'],
                        'is_blank': False,
                    })
                    fixture_idx += 1
            else:
                fixture_rows.append({'gw': gw, 'is_blank': True})

        team_info = teams_map[team_id_to_show]
        image_data = await asyncio.to_thread(
            generate_fixtures_single_image, team_info, fixture_rows, current_gw
        )

    else:
        # All teams — next 5 GWs (excluding current live GW)
        next_gw = current_gw + 1
        all_upcoming = sorted(
            [f for f in fixtures_data if f.get('event') and f['event'] >= next_gw],
            key=lambda x: x['event']
        )

        # Build per-team structured fixture data (supports DGWs)
        team_gw_fixtures = {team_id: {} for team_id in teams_map}
        for f in all_upcoming:
            gw = f['event']
            # Home team
            if gw not in team_gw_fixtures[f['team_h']]:
                team_gw_fixtures[f['team_h']][gw] = []
            team_gw_fixtures[f['team_h']][gw].append({
                'gw': gw,
                'opponent': teams_map[f['team_a']]['short_name'],
                'is_home': True,
                'fdr': f['team_h_difficulty'],
                'is_blank': False,
            })
            # Away team
            if gw not in team_gw_fixtures[f['team_a']]:
                team_gw_fixtures[f['team_a']][gw] = []
            team_gw_fixtures[f['team_a']][gw].append({
                'gw': gw,
                'opponent': teams_map[f['team_h']]['short_name'],
                'is_home': False,
                'fdr': f['team_a_difficulty'],
                'is_blank': False,
            })

        gw_range = list(range(next_gw, next_gw + 5))

        teams_fixtures = []
        for team_id, team_data in sorted(teams_map.items(), key=lambda x: x[1]['name']):
            team_fixture_list = []
            for gw in gw_range:
                if gw in team_gw_fixtures[team_id]:
                    team_fixture_list.extend(team_gw_fixtures[team_id][gw])
                else:
                    team_fixture_list.append({'gw': gw, 'is_blank': True})
            teams_fixtures.append({
                'team_short': team_data['name'],
                'team_name': team_data['name'],
                'fixtures': team_fixture_list,
            })

        image_data = await asyncio.to_thread(
            generate_fixtures_all_image, teams_fixtures, gw_range, current_gw
        )

    if image_data:
        file = discord.File(image_data, filename="fixtures.png")
        await interaction.followup.send(file=file)
    else:
        await interaction.followup.send("Failed to generate fixtures image.", ephemeral=True)

@fixtures.autocomplete('team')
async def fixtures_autocomplete(interaction: discord.Interaction, current: str):
    # Use in-memory cached bootstrap data for performance
    bootstrap_data = await bot.get_autocomplete_bootstrap()
    if not bootstrap_data:
        return []

    all_teams = bootstrap_data.get('teams', [])
    choices = []
    current_lower = current.lower()

    for team in all_teams:
        team_name = team['name']
        if current_lower in team_name.lower():
            choices.append(app_commands.Choice(name=team_name, value=str(team['id'])))

    return sorted(choices, key=lambda x: x.name)[:25]

@bot.tree.command(name="recap", description="Shows the best and worst manager decisions from the last completed gameweek.")
async def recap(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    completed_gw = await get_last_completed_gameweek(session)
    if not completed_gw:
        await interaction.followup.send("Could not determine the last completed gameweek.")
        return

    image_data = await bot._build_recap(completed_gw, int(league_id))
    if image_data:
        file = discord.File(image_data, filename="gw_recap.png")
        link_text = f"[View full league stats at LiveFPLStats](<{WEBSITE_URL}/league?{league_id}>)"
        await interaction.followup.send(content=link_text, file=file)
    else:
        await interaction.followup.send(f"Could not generate recap for GW {completed_gw}.")

# =====================================================
# /NOTIFY — DM notification management (Phase 5)
# =====================================================

@bot.tree.command(name="notify", description="Manage personal DM notifications (Premium+ only).")
@app_commands.describe(action="Enable, disable, or check status of DM notifications")
@app_commands.choices(action=[
    app_commands.Choice(name="enable", value="enable"),
    app_commands.Choice(name="disable", value="disable"),
    app_commands.Choice(name="status", value="status"),
])
async def notify_command(interaction: discord.Interaction, action: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    guild_id = str(interaction.guild_id) if interaction.guild_id else None

    if not guild_id:
        await interaction.followup.send("This command must be used in a server.")
        return

    if action.value == "enable":
        # 1. Check premium_plus via website account
        user_data = await get_user_by_discord(bot.session, user_id)
        if not user_data or user_data.get('tier') != 'premium_plus':
            await interaction.followup.send(
                "DM notifications require a **Premium+** subscription.\n"
                f"Upgrade at {WEBSITE_URL}/pricing"
            )
            return

        # 2. Get FPL manager ID (from website account, fallback to bot's user_links)
        fpl_manager_id = user_data.get('fplManagerId')
        if not fpl_manager_id:
            fpl_manager_id = get_fpl_id_for_user(guild_id, user_id)
        if not fpl_manager_id:
            await interaction.followup.send(
                "No FPL team linked. Use `/claim` to link your team first, "
                "or set your FPL Manager ID on the website."
            )
            return

        # 3. Upsert subscription
        upsert_dm_subscription(user_id, guild_id, fpl_manager_id)

        # 4. Send confirmation DM immediately (creates the warm DM channel)
        try:
            dm_channel = await interaction.user.create_dm()
            await dm_channel.send(embed=build_confirmation_embed())
            # Cache the DM channel ID
            update_dm_channel_id(user_id, guild_id, str(dm_channel.id))
            await interaction.followup.send(
                "DM notifications enabled! Check your DMs for a confirmation message."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I couldn't send you a DM. Please enable DMs from server members "
                "in your Privacy Settings, then try again."
            )
            # Clean up the subscription since we can't DM
            delete_dm_subscription(user_id, guild_id)

    elif action.value == "disable":
        delete_dm_subscription(user_id, guild_id)
        await interaction.followup.send("DM notifications disabled. You won't receive any more DMs.")

    elif action.value == "status":
        sub = get_dm_subscription(user_id, guild_id)
        if not sub:
            await interaction.followup.send("You don't have DM notifications enabled. Use `/notify enable` to opt in.")
            return

        embed = discord.Embed(
            title="DM Notification Settings",
            color=0x3498db,
        )
        embed.add_field(name="Deadline Reminders", value="On" if sub.get('deadline_reminder') else "Off", inline=True)
        embed.add_field(name="Injury Alerts", value="On" if sub.get('injury_alerts') else "Off", inline=True)
        embed.add_field(name="Captain Suggestions", value="On" if sub.get('captain_suggestion') else "Off", inline=True)
        embed.add_field(name="Transfer Suggestions", value="On" if sub.get('transfer_suggestion') else "Off", inline=True)
        embed.add_field(name="FPL Manager ID", value=str(sub.get('fpl_manager_id', '?')), inline=True)
        embed.add_field(name="DM Status", value="Failed" if sub.get('dm_failed') else "Active", inline=True)
        embed.set_footer(text="LiveFPLStats Premium+")
        await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        logger.critical("DISCORD_BOT_TOKEN not found in .env file. Please create a .env file with your bot token.")
    else:
        bot.run(DISCORD_BOT_TOKEN)







