import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import os
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import io
import asyncio
import sqlite3
from dotenv import load_dotenv
from typing import Literal

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CONFIG_PATH = Path("config/league_config.json")
DB_PATH = Path("config/fpl_bot.db")
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


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


def init_database():
    """Initializes the database and creates/migrates tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()

        # Check if league_teams has the old discord_user_id column
        cur.execute("PRAGMA table_info(league_teams)")
        columns = [row[1] for row in cur.fetchall()]
        if 'discord_user_id' in columns:
            print("Old schema detected. Migrating league_teams table...")
            cur.execute("CREATE TABLE IF NOT EXISTS league_teams_new (fpl_team_id INTEGER PRIMARY KEY, league_id INTEGER NOT NULL, team_name TEXT NOT NULL, manager_name TEXT NOT NULL)")
            cur.execute("INSERT INTO league_teams_new (fpl_team_id, league_id, team_name, manager_name) SELECT fpl_team_id, league_id, team_name, manager_name FROM league_teams")
            cur.execute("DROP TABLE league_teams")
            cur.execute("ALTER TABLE league_teams_new RENAME TO league_teams")
            print("Migration complete.")

        # Ensure league_teams table exists (for fresh setups)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS league_teams (
                fpl_team_id INTEGER PRIMARY KEY,
                league_id INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                manager_name TEXT NOT NULL
            )
        """)

        # Create the new user_links table for per-server linking
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_links (
                guild_id TEXT NOT NULL,
                discord_user_id TEXT NOT NULL,
                fpl_team_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id),
                UNIQUE (guild_id, fpl_team_id)
            )
        """)

        # Create and migrate goal_subscriptions table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS goal_subscriptions (
                channel_id TEXT PRIMARY KEY,
                league_id INTEGER NOT NULL,
                transfer_alerts_enabled BOOLEAN NOT NULL DEFAULT 0
            )
        """)
        # This handles migration for older versions that didn't have the new column
        cur.execute("PRAGMA table_info(goal_subscriptions)")
        columns = [row[1] for row in cur.fetchall()]
        if 'transfer_alerts_enabled' not in columns:
            print("Migrating goal_subscriptions table for transfer alerts...")
            cur.execute("ALTER TABLE goal_subscriptions ADD COLUMN transfer_alerts_enabled BOOLEAN NOT NULL DEFAULT 0")
            print("Migration complete.")

        con.commit()


def upsert_league_teams(league_id, teams):
    """Inserts or updates team information in the database."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        for team in teams:
            cur.execute("""
                INSERT INTO league_teams (fpl_team_id, league_id, team_name, manager_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fpl_team_id) DO UPDATE SET
                    team_name = excluded.team_name,
                    manager_name = excluded.manager_name
            """, (team['entry'], league_id, team['entry_name'], team['player_name']))
        con.commit()


def get_fpl_id_for_user(guild_id: int, user_id: int):
    """Gets the FPL team ID linked to a Discord user in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT fpl_team_id FROM user_links WHERE guild_id = ? AND discord_user_id = ?", (str(guild_id), str(user_id)))
        result = cur.fetchone()
        return result[0] if result else None


def get_linked_user_for_team(guild_id: int, fpl_team_id: int):
    """Gets the Discord user ID linked to an FPL team in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT discord_user_id FROM user_links WHERE guild_id = ? AND fpl_team_id = ?", (str(guild_id), fpl_team_id))
        result = cur.fetchone()
        return result[0] if result else None


def link_user_to_team(guild_id: int, user_id: int, fpl_team_id: int):
    """Links a Discord user to an FPL team in a specific guild, overwriting any previous link for that user in that guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO user_links (guild_id, discord_user_id, fpl_team_id) VALUES (?, ?, ?)", (str(guild_id), str(user_id), fpl_team_id))
        con.commit()


def get_unclaimed_teams(league_id: int, guild_id: int, search_term: str):
    """Gets a list of teams in a league that are not claimed in the specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        # Find all teams in the league that are NOT in the user_links table for the current guild
        cur.execute("""
            SELECT fpl_team_id, team_name, manager_name 
            FROM league_teams
            WHERE league_id = ? 
              AND (team_name LIKE ? OR manager_name LIKE ?)
              AND fpl_team_id NOT IN (
                SELECT fpl_team_id FROM user_links WHERE guild_id = ?
              )
            LIMIT 25
        """, (league_id, f"%{search_term}%", f"%{search_term}%", str(guild_id)))
        return cur.fetchall()

def get_all_teams_for_autocomplete(league_id: int, search_term: str):
    """Gets a list of all teams for autocomplete."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
            SELECT fpl_team_id, team_name, manager_name FROM league_teams
            WHERE league_id = ? AND (team_name LIKE ? OR manager_name LIKE ?)
            LIMIT 25
        """, (league_id, f"%{search_term}%", f"%{search_term}%"))
        return cur.fetchall()


def get_team_by_fpl_id(fpl_team_id: int):
    """Gets all details for a specific FPL team."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM league_teams WHERE fpl_team_id = ?", (fpl_team_id,))
        return cur.fetchone()


def get_linked_users(guild_id: int, league_id: int):
    """Gets a list of all FPL teams that are linked to a Discord user in a specific guild."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT T.fpl_team_id, L.discord_user_id, T.manager_name 
            FROM league_teams T
            INNER JOIN user_links L ON T.fpl_team_id = L.fpl_team_id
            WHERE T.league_id = ? AND L.guild_id = ?
        """, (league_id, str(guild_id)))
        return cur.fetchall()

def get_all_league_teams(guild_id: int, league_id: int):
    """Gets a list of all teams for a league, including the linked discord user if one exists for the guild."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("""
            SELECT T.fpl_team_id, L.discord_user_id, T.manager_name 
            FROM league_teams T
            LEFT JOIN user_links L ON T.fpl_team_id = L.fpl_team_id AND L.guild_id = ?
            WHERE T.league_id = ?
        """, (str(guild_id), league_id))
        return cur.fetchall()


def is_goal_subscribed(channel_id: int):
    """Checks if a channel is subscribed to goal alerts."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        return cur.fetchone() is not None

def add_goal_subscription(channel_id: int, league_id: int):
    """Adds a channel to the goal alert subscription list."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("INSERT INTO goal_subscriptions (channel_id, league_id) VALUES (?, ?)", (str(channel_id), league_id))
        con.commit()

def remove_goal_subscription(channel_id: int):
    """Removes a channel from the goal alert subscription list."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        con.commit()

def get_all_goal_subscriptions():
    """Gets all channel IDs and their league IDs subscribed to goal alerts."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT channel_id, league_id, transfer_alerts_enabled FROM goal_subscriptions")
        return cur.fetchall()


def is_transfer_alert_subscribed(channel_id: int):
    """Checks if a channel is subscribed to transfer flop alerts."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("SELECT transfer_alerts_enabled FROM goal_subscriptions WHERE channel_id = ?", (str(channel_id),))
        result = cur.fetchone()
        return result[0] if result and result[0] else False

def set_transfer_alert_subscription(channel_id: int, status: bool):
    """Sets the transfer alert subscription status for a channel."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("UPDATE goal_subscriptions SET transfer_alerts_enabled = ? WHERE channel_id = ?", (status, str(channel_id)))
        con.commit()


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

# --- FILE PATHS ---
BACKGROUND_IMAGE_PATH = "pitch-graphic-t77-OTdp.png"
FONT_PATH = "font.ttf"
HEADSHOTS_DIR = "player_headshots"
JERSEYS_DIR = "team_jerseys"

# --- API & DATA ---
BASE_API_URL = "https://fantasy.premierleague.com/api/"
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
}

# --- LAYOUT & STYLING (from your original script) ---
PITCH_X_START, PITCH_X_END = 200, 1216
GK_Y, DEF_Y, MID_Y, FWD_Y = 65, 265, 465, 665
BENCH_X, BENCH_Y_START, BENCH_Y_SPACING = 120, 65, 180
SUMMARY_X, SUMMARY_Y_START, SUMMARY_LINE_SPACING = 1300, 40, 35
NAME_FONT_SIZE, POINTS_FONT_SIZE, CAPTAIN_FONT_SIZE, SUMMARY_FONT_SIZE = 22, 20, 22, 24
POINTS_BOX_EXTRA_PADDING = 4


class FPLBot(commands.Bot):
    """A Discord bot for displaying FPL league and team information."""
    def __init__(self):
        intents = discord.Intents.default()
        intents.presences = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.session = None
        self.last_known_goals = {}
        self.picks_cache = {} # Cache for manager picks
        self.transfers_cache = {} # Cache for manager transfers
        self.live_fpl_data = None # In-memory cache for live GW data

    async def setup_hook(self):
        init_database()
        self.session = aiohttp.ClientSession()
        self.api_semaphore = asyncio.Semaphore(10)
        self.live_data_loop.start()
        self.goal_check_loop.start()
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}.")

    @tasks.loop(seconds=60)
    async def live_data_loop(self):
        """Periodically fetches live FPL data for the current gameweek."""
        await self.wait_until_ready()
        try:
            bootstrap_data = await fetch_fpl_api(self.session, f"{BASE_API_URL}bootstrap-static/")
            if not bootstrap_data or 'events' not in bootstrap_data:
                self.live_fpl_data = None
                return

            live_event = next((event for event in bootstrap_data['events'] if event['is_current']), None)
            
            if not live_event or live_event.get('finished', False) or not live_event.get('data_checked', False):
                if self.live_fpl_data is not None:
                    print("Gameweek is no longer live. Clearing live data cache.")
                    self.live_fpl_data = None
                return
            
            current_gw = live_event['id']
            live_data = await fetch_fpl_api(self.session, f"{BASE_API_URL}event/{current_gw}/live/")
            if live_data:
                live_data['gw'] = current_gw
                live_data['is_finished'] = live_event.get('finished', False)
                self.live_fpl_data = live_data
            else:
                self.live_fpl_data = None
        except Exception as e:
            print(f"Error in live_data_loop: {e}")
            self.live_fpl_data = None

    @tasks.loop(seconds=60)
    async def goal_check_loop(self):
        await self.wait_until_ready()
        
        live_data = self.live_fpl_data
        if not live_data:
            self.last_known_goals = {}
            return
        
        current_gw = live_data.get('gw')
        if not current_gw:
            return

        if self.last_known_goals.get("gw") != current_gw:
            print(f"Initializing goal cache for GW {current_gw}.")
            self.last_known_goals = {"gw": current_gw}
            for player_stats in live_data.get('elements', []):
                self.last_known_goals[player_stats['id']] = player_stats['stats']['goals_scored']
            return

        bootstrap_data = await fetch_fpl_api(self.session, f"{BASE_API_URL}bootstrap-static/")
        if not bootstrap_data: return

        for player_stats in live_data.get('elements', []):
            player_id = player_stats['id']
            new_goals = player_stats['stats']['goals_scored']
            old_goals = self.last_known_goals.get(player_id, 0)

            if new_goals > old_goals:
                self.last_known_goals[player_id] = new_goals
                
                goals_scored = new_goals - old_goals
                player_info = next((p for p in bootstrap_data.get('elements', []) if p['id'] == player_id), None)
                if not player_info: continue

                team_id = player_info['team']
                fixture = next((f for f in live_data.get('fixtures', []) if f['team_h'] == team_id or f['team_a'] == team_id), None)
                if not fixture: continue
                
                opponent_id = fixture['team_a'] if fixture['team_h'] == team_id else fixture['team_h']
                opponent_team = next((t for t in bootstrap_data.get('teams', []) if t['id'] == opponent_id), None)
                opponent_name = opponent_team['name'] if opponent_team else "Unknown"
                player_team = next((t for t in bootstrap_data.get('teams', []) if t['id'] == team_id), None)

                # --- Find owners and broadcast ---
                for sub in (await asyncio.to_thread(get_all_goal_subscriptions)):
                    channel = self.get_channel(int(sub['channel_id']))
                    if not channel or not channel.guild: continue

                    goal_alerts_on = True # This is implied by being in the table
                    transfer_alerts_on = sub['transfer_alerts_enabled']
                    league_id = sub['league_id']

                    # --- Caching ---
                    # Ensure picks and transfers are cached for this league and GW
                    if self.picks_cache.get('gw') != current_gw or league_id not in self.picks_cache:
                        self.picks_cache = {'gw': current_gw, league_id: {}}
                        self.transfers_cache = {'gw': current_gw, league_id: {}}
                        linked_users = await asyncio.to_thread(get_linked_users, channel.guild.id, league_id)
                        
                        async def fetch_manager_data(user):
                            picks, transfers = await asyncio.gather(
                                fetch_fpl_api(self.session, f"{BASE_API_URL}entry/{user['fpl_team_id']}/event/{current_gw}/picks/"),
                                fetch_fpl_api(self.session, f"{BASE_API_URL}entry/{user['fpl_team_id']}/transfers/")
                            )
                            if picks: self.picks_cache[league_id][user['discord_user_id']] = picks
                            if transfers: self.transfers_cache[league_id][user['discord_user_id']] = transfers
                        
                        await asyncio.gather(*[fetch_manager_data(u) for u in linked_users])

                    # --- Logic ---
                    owners, benched, transferors = [], [], []
                    
                    # Find owners
                    for user_id, picks in self.picks_cache.get(league_id, {}).items():
                        for pick in picks.get('picks', []):
                            if pick['element'] == player_id:
                                if pick['position'] <= 11: owners.append(f"<@{user_id}>")
                                else: benched.append(f"<@{user_id}>")
                    
                    # Find transferors if alert is enabled
                    if transfer_alerts_on:
                        for user_id, transfers in self.transfers_cache.get(league_id, {}).items():
                            # Check transfers for the current gameweek
                            for transfer in [t for t in transfers if t.get('event') == current_gw]:
                                if transfer['element_out'] == player_id:
                                    transferors.append(f"<@{user_id}>")
                    
                    # --- Send Message ---
                    if (goal_alerts_on and (owners or benched)) or (transfer_alerts_on and transferors):
                        embed = discord.Embed(
                            title=f"⚽ GOAL: {player_info['web_name']} ({player_team['short_name']})",
                            description=f"Scored {goals_scored} goal(s) against **{opponent_name}**!",
                            color=discord.Color.green()
                        )
                        if goal_alerts_on:
                            if owners: embed.add_field(name="Owned By", value=", ".join(owners), inline=False)
                            if benched: embed.add_field(name="Benched By (🤡)", value=", ".join(benched), inline=False)
                        
                        if transfer_alerts_on and transferors:
                            embed.add_field(name="🤣 Transferred Out By", value=", ".join(transferors), inline=False)
                        
                        await channel.send(embed=embed)

    async def close(self):
        if self.session:
            await self.session.close()
        self.live_data_loop.cancel()
        self.goal_check_loop.cancel()
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        await bot.change_presence(status=discord.Status.online, activity=discord.Game(name="Fantasy Premier League"))
        print("Bot is ready and online.")

bot = FPLBot()

# --- FPL API HELPER FUNCTIONS (Async) ---
async def fetch_fpl_api(session, url, cache_key=None, cache_gw=None, force_refresh=False):
    """Fetches data from the FPL API asynchronously."""
    cache_path = None
    if cache_key:
        cache_suffix = f"_gw{cache_gw}" if cache_gw is not None else ""
        cache_path = CACHE_DIR / f"{cache_key}{cache_suffix}.json"
        cached = await load_cached_json(cache_path)
        if cached and not force_refresh:
            return cached.get("data", cached)

    try:
        async with session.get(url, headers=REQUEST_HEADERS) as response:
            if response.status == 200:
                data = await response.json()
                if cache_path and not force_refresh:
                    payload = {"data": data, "gameweek": cache_gw}
                    await save_cached_json(cache_path, payload)
                return data
            else:
                print(f"Error fetching {url}: Status {response.status}")
                return None
        sem = getattr(bot, "api_semaphore", None)
        if sem:
            await sem.acquire()
        try:
            async with session.get(url, headers=REQUEST_HEADERS) as response:
                if response.status == 200:
                    data = await response.json()
                    if cache_path and not force_refresh:
                        payload = {"data": data, "gameweek": cache_gw}
                        await save_cached_json(cache_path, payload)
                    return data
                else:
                    print(f"Error fetching {url}: Status {response.status}")
                    return None
        finally:
            if sem:
                sem.release()
    except aiohttp.ClientError as e:
        print(f"Request error for {url}: {e}")
        return None

async def get_current_gameweek(session):
    """Determines the current FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        current_event = next((event for event in bootstrap_data['events'] if event['is_current']), None)
        return current_event['id'] if current_event else None
    return None

async def get_last_completed_gameweek(session):
    """Determines the most recently completed FPL gameweek."""
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if bootstrap_data:
        completed_events = [event for event in bootstrap_data['events'] if event['finished']]
        if completed_events:
            return max(completed_events, key=lambda x: x['id'])['id']
    return None

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

# --- NEW REFACTORED HELPER FOR LIVE POINT CALCULATION ---
async def get_live_manager_details(session, manager_entry, current_gw, live_points_map, all_players_map, live_data, is_finished=False):
    """Fetches picks/history for a manager and calculates their score, handling auto-subs for finished GWs."""
    manager_id = manager_entry['entry']
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
            
            squad = list(starters) # This is the list of players we will modify

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
                        counts = {1:0, 2:0, 3:0, 4:0}
                        for p in potential_squad:
                            player_type = all_players_map[p['element']]['element_type']
                            counts[player_type] += 1
                        
                        if counts[1] == 1 and counts[2] >= 3 and counts[3] >= 2 and counts[4] >= 1:
                            player_subbed_out = player_to_replace
                            squad = potential_squad
                            break # Sub successful, move to next bench player
                
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
                else: # Captain didn't play and their game is over
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
        "live_total_points": live_total_points,
        "final_gw_points": final_gw_points,
        "players_played": players_played_count,
        "picks_data": picks_data
    }

# --- IMAGE GENERATION LOGIC ---

def format_player_price(player):
    """Return player's price as £X.Xm string."""
    return f"£{player.get('now_cost', 0) / 10:.1f}m"


def build_manager_url(entry_id, gameweek=None):
    """Return the FPL website URL for a manager's team."""
    if gameweek:
        return f"https://fantasy.premierleague.com/entry/{entry_id}/event/{gameweek}"
    return f"https://fantasy.premierleague.com/entry/{entry_id}/history/"


def format_manager_link(label, entry_id, gameweek=None):
    """Wrap a label in Markdown linking to the manager's FPL team."""
    url = build_manager_url(entry_id, gameweek)
    return f"[{label}]({url})"


async def get_manager_transfer_activity(session, manager_entry_id, gameweek):
    """Fetch transfer, chip, and cost info for a manager for the given gameweek."""
    transfers_task = fetch_fpl_api(
        session,
        f"{BASE_API_URL}entry/{manager_entry_id}/transfers/",
        cache_key=f"transfers_entry_{manager_entry_id}",
        cache_gw=gameweek
    )
    picks_task = fetch_fpl_api(
        session,
        f"{BASE_API_URL}entry/{manager_entry_id}/event/{gameweek}/picks/",
        cache_key=f"picks_entry_{manager_entry_id}",
        cache_gw=gameweek
    )
    transfers_data, picks_data = await asyncio.gather(transfers_task, picks_task)
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


def calculate_player_coordinates(picks, all_players):
    starters = [p for p in picks if p['position'] <= 11]
    bench = [p for p in picks if p['position'] > 11]
    positions = {1: [], 2: [], 3: [], 4: []}
    for p in starters:
        player_type = all_players[p['element']]['element_type']
        positions[player_type].append(p)
    coords = {}
    pitch_width = PITCH_X_END - PITCH_X_START
    if positions[1]:
        coords[positions[1][0]['element']] = ((PITCH_X_START + PITCH_X_END) // 2, GK_Y)
    for pos_type, y_coord in [(2, DEF_Y), (3, MID_Y), (4, FWD_Y)]:
        num_players = len(positions[pos_type])
        for i, p in enumerate(positions[pos_type]):
            x = PITCH_X_START + ((i + 1) * pitch_width) / (num_players + 1)
            coords[p['element']] = (int(x), y_coord)
    for i, p in enumerate(bench):
        coords[p['element']] = (BENCH_X, BENCH_Y_START + (i * BENCH_Y_SPACING))
    return coords

POINTS_BOX_EXTRA_PADDING = 4

def generate_team_image(fpl_data, summary_data, is_finished=False):
    try:
        pitch = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        base_layer = Image.new("RGBA", pitch.size, "#1A1A1E")
        background = Image.alpha_composite(base_layer, pitch)
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        captain_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        print(f"Error loading resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats'] for p in fpl_data['live']['elements']}
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players)

    # Determine the final set of scoring players from the provided data
    scoring_picks_data = fpl_data['picks'].get('scoring_picks', [])
    scoring_player_ids = {p['element'] for p in scoring_picks_data}

    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        base_points = live_points.get(player_id, {}).get('total_points', 0)
        
        is_starter = player_pick['position'] <= 11
        was_subbed_out = is_starter and player_id not in scoring_player_ids and is_finished
        was_subbed_in = not is_starter and player_id in scoring_player_ids and is_finished

        # Determine points to display
        display_points = base_points
        scoring_pick_details = next((p for p in scoring_picks_data if p['element'] == player_id), None)

        if scoring_pick_details:
            final_multiplier = scoring_pick_details.get('final_multiplier', 1)
            display_points = base_points * final_multiplier
        
        points_text = f"{display_points} pts"
        
        # For subbed out starters, show their base points but they won't be summed
        if was_subbed_out:
            points_text = f"({base_points}) pts"

        # --- Drawing logic ---
        asset_img = None
        asset_size = (88, 112)
        headshot_path = os.path.join(HEADSHOTS_DIR, f"{player_name.replace(' ', '_')}_{player_id}.png")
        try:
            asset_img = Image.open(headshot_path).convert("RGBA")
        except FileNotFoundError:
            team_id = player_info['team']
            team_name = all_teams[team_id]['name'].replace(' ', '_')
            kit = 'goalkeeper' if player_info['element_type'] == 1 else 'home'
            jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
            asset_size = (110, 110)
            try:
                asset_img = Image.open(jersey_path).convert("RGBA")
            except FileNotFoundError:
                continue
        asset_img = asset_img.resize(asset_size, Image.LANCZOS)
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2
        
        # Add visual indicators for subs
        if was_subbed_out:
            red_overlay = Image.new("RGBA", asset_img.size, (255, 0, 0, 80))
            asset_img = Image.alpha_composite(asset_img, red_overlay)
        if was_subbed_in:
            green_overlay = Image.new("RGBA", asset_img.size, (0, 255, 0, 80))
            asset_img = Image.alpha_composite(asset_img, green_overlay)

        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = max(name_bbox[2], points_bbox[2]) + 10
        name_box_height = fixed_name_box_height
        name_box_x = x - box_width // 2
        name_box_y = y + 55
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill="#015030")
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            active_chip = fpl_data['picks'].get('active_chip')
            captain_text = "TC" if active_chip == '3xc' else "C"
            draw.text((paste_x + 80, paste_y - 5), captain_text, font=captain_font, fill="black", stroke_width=2, stroke_fill="yellow")
        elif player_pick['is_vice_captain']:
            draw.text((paste_x + 80, paste_y - 5), "V", font=captain_font, fill="black", stroke_width=2, stroke_fill="white")

    summary_strings = [f"League Rank: {summary_data['rank']}", f"GW Points: {summary_data['gw_points']}", f"Total Points: {summary_data['total_points']}"]
    summary_text_width = 0
    for text in summary_strings:
        bbox = draw.textbbox((0, 0), text, font=summary_font)
        summary_text_width = max(summary_text_width, bbox[2] - bbox[0])
    summary_padding = 20
    summary_padding_y = 5
    summary_box = [
        SUMMARY_X - summary_text_width - summary_padding,
        SUMMARY_Y_START - summary_padding_y,
        SUMMARY_X + 10,
        SUMMARY_Y_START + len(summary_strings) * SUMMARY_LINE_SPACING + summary_padding_y
    ]
    draw.rounded_rectangle(summary_box, radius=16, fill="#6625ff")

    for i, text in enumerate(summary_strings):
        y_pos = SUMMARY_Y_START + (i * SUMMARY_LINE_SPACING)
        text_bbox = draw.textbbox((0, 0), text, font=summary_font)
        text_width = text_bbox[2] - text_bbox[0]
        x_pos = SUMMARY_X - text_width
        draw.text((x_pos, y_pos), text, font=summary_font, fill="white", stroke_width=1, stroke_fill="black")

    img_byte_arr = io.BytesIO()
    background.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

def generate_dreamteam_image(fpl_data, summary_data):
    """Generate dream team image with Player of the Week graphic."""
    try:
        pitch = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        base_layer = Image.new("RGBA", pitch.size, "#1A1A1E")
        background = Image.alpha_composite(base_layer, pitch)
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
        potw_font = ImageFont.truetype(FONT_PATH, 20)  # Player of the Week font
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        print(f"Error loading resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats']['total_points'] for p in fpl_data['live']['elements']}
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players)

    # Draw players (same as original team image but without captain/vice captain)
    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        base_points = live_points.get(player_id, 0)
        multiplier = player_pick.get('multiplier', 1)
        is_bench_player = player_pick['position'] > 11 and multiplier == 0
        display_points = base_points if is_bench_player else base_points * multiplier
        asset_img = None
        asset_size = (88, 112)
        headshot_path = os.path.join(HEADSHOTS_DIR, f"{player_name.replace(' ', '_')}_{player_id}.png")
        try:
            asset_img = Image.open(headshot_path).convert("RGBA")
        except FileNotFoundError:
            team_id = player_info['team']
            team_name = all_teams[team_id]['name'].replace(' ', '_')
            kit = 'goalkeeper' if player_info['element_type'] == 1 else 'home'
            jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
            asset_size = (110, 110)
            try:
                asset_img = Image.open(jersey_path).convert("RGBA")
            except FileNotFoundError:
                continue
        asset_img = asset_img.resize(asset_size, Image.LANCZOS)
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text, points_text = player_name, f"{display_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = max(name_bbox[2], points_bbox[2]) + 10
        name_box_height = fixed_name_box_height
        name_box_x = x - box_width // 2
        name_box_y = y + 55
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill=(0, 135, 81, 150))
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

    # Draw summary info (modified for dream team)
    summary_strings = [f"Dream Team", f"Total: {summary_data['total_points']} pts", f"Gameweek {summary_data['gameweek']}"]
    dream_text_width = 0
    for text in summary_strings:
        bbox = draw.textbbox((0, 0), text, font=summary_font)
        dream_text_width = max(dream_text_width, bbox[2] - bbox[0])
    dream_padding = 20
    dream_padding_y = 5
    dream_box = [
        SUMMARY_X - dream_text_width - dream_padding,
        SUMMARY_Y_START - dream_padding_y,
        SUMMARY_X + 10,
        SUMMARY_Y_START + len(summary_strings) * SUMMARY_LINE_SPACING + dream_padding_y
    ]
    draw.rounded_rectangle(dream_box, radius=16, fill="#6625ff")

    for i, text in enumerate(summary_strings):
        y_pos = SUMMARY_Y_START + (i * SUMMARY_LINE_SPACING)
        text_bbox = draw.textbbox((0, 0), text, font=summary_font)
        text_width = text_bbox[2] - text_bbox[0]
        x_pos = SUMMARY_X - text_width
        draw.text((x_pos, y_pos), text, font=summary_font, fill="white", stroke_width=1, stroke_fill="black")

    # Draw Player of the Week section
    potw_data = summary_data['player_of_week']
    potw_player_info = potw_data['player_info']
    potw_name = potw_player_info['web_name']
    potw_points = potw_data['points']
    
    # Player of the Week positioning (top left, same Y as goalkeeper)
    potw_x = 140  # Left side of the image
    potw_y = GK_Y - 20  # Same Y axis as goalkeeper, slightly above
    
    # Try to load player headshot for POTW
    potw_headshot_path = os.path.join(HEADSHOTS_DIR, f"{potw_name.replace(' ', '_')}_{potw_data['id']}.png")
    potw_img = None
    try:
        potw_img = Image.open(potw_headshot_path).convert("RGBA")
        potw_img = potw_img.resize((60, 76), Image.LANCZOS)
    except FileNotFoundError:
        # Fallback to jersey if headshot not found
        team_id = potw_player_info['team']
        team_name = all_teams[team_id]['name'].replace(' ', '_')
        kit = 'goalkeeper' if potw_player_info['element_type'] == 1 else 'home'
        jersey_path = os.path.join(JERSEYS_DIR, f"{team_name}_{kit}.webp")
        try:
            potw_img = Image.open(jersey_path).convert("RGBA")
            potw_img = potw_img.resize((60, 60), Image.LANCZOS)
        except FileNotFoundError:
            pass
    
    # Draw POTW background box
    potw_box_width = 220  # Made wider to fit "Player of the Week" text
    potw_box_height = 110
    potw_box = [potw_x, potw_y, potw_x + potw_box_width, potw_y + potw_box_height]
    draw.rounded_rectangle(potw_box, radius=14, fill="#ffd700")
    
    # Draw "Player of the Week" title
    title_text = "Player of the Week"
    title_bbox = draw.textbbox((0, 0), title_text, font=potw_font)
    title_x = potw_x + (potw_box_width - title_bbox[2]) // 2
    draw.text((title_x, potw_y + 5), title_text, font=potw_font, fill="black")
    
    # Draw player image if available
    if potw_img:
        img_x = potw_x + 15
        img_y = potw_y + 30
        background.paste(potw_img, (img_x, img_y), potw_img)
        
        # Draw name and points beside the image
        name_x = img_x + potw_img.width + 15
        draw.text((name_x, img_y), potw_name, font=potw_font, fill="black")
        draw.text((name_x, img_y + 25), f"{potw_points} pts", font=potw_font, fill="black")
        draw.text((name_x, img_y + 50), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_font, fill="black")
    else:
        # Draw text only if no image
        name_x = potw_x + 15
        draw.text((name_x, potw_y + 35), potw_name, font=potw_font, fill="black")
        draw.text((name_x, potw_y + 55), f"{potw_points} pts", font=potw_font, fill="black")
        draw.text((name_x, potw_y + 75), f"Goals: {potw_data['goals']}, Assists: {potw_data['assists']}", font=potw_font, fill="black")

    img_byte_arr = io.BytesIO()
    background.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

# --- DISCORD SLASH COMMANDS ---

@bot.tree.command(name="toggle_goals", description="Enable or disable live goal alerts in this channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_goals(interaction: discord.Interaction):
    """Toggles goal alerts for the current channel."""
    await interaction.response.defer(ephemeral=True)

    league_id = get_league_id_for_context(interaction)
    if not league_id:
        await interaction.followup.send("A league must be configured for this channel or server first. Use `/setleague`.")
        return

    channel_id = interaction.channel_id
    if await asyncio.to_thread(is_goal_subscribed, channel_id):
        await asyncio.to_thread(remove_goal_subscription, channel_id)
        await interaction.followup.send("🔴 Live goal alerts disabled for this channel.")
    else:
        await asyncio.to_thread(add_goal_subscription, channel_id, league_id)
        await interaction.followup.send("🟢 Live goal alerts enabled. I will post goals as they happen.")

@toggle_goals.error
async def toggle_goals_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the `Manage Channels` permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
        raise error


@bot.tree.command(name="toggle_transfer_alerts", description="Enable or disable transfer flop alerts in this channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def toggle_transfer_alerts(interaction: discord.Interaction):
    """Toggles transfer flop alerts for the current channel."""
    await interaction.response.defer(ephemeral=True)

    # This alert depends on the goal subscription, so check that first
    if not await asyncio.to_thread(is_goal_subscribed, interaction.channel_id):
        await interaction.followup.send("Goal alerts must be enabled first with `/toggle_goals` before you can enable this.", ephemeral=True)
        return

    is_subscribed = await asyncio.to_thread(is_transfer_alert_subscribed, interaction.channel_id)
    
    if is_subscribed:
        await asyncio.to_thread(set_transfer_alert_subscription, interaction.channel_id, False)
        await interaction.followup.send("🔴 Transfer flop alerts disabled for this channel.")
    else:
        await asyncio.to_thread(set_transfer_alert_subscription, interaction.channel_id, True)
        await interaction.followup.send("🟢 Transfer flop alerts enabled for this channel.")

@toggle_transfer_alerts.error
async def toggle_transfer_alerts_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the `Manage Channels` permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
        raise error


@bot.tree.command(name="setleague", description="Configure which FPL league this server or channel uses.")
@app_commands.describe(league_id="The FPL league ID (numbers only).",
                       scope="Apply this league to the whole server or just this channel.")
@app_commands.choices(scope=[
    app_commands.Choice(name="Server-wide (default)", value="server"),
    app_commands.Choice(name="This channel only", value="channel")
])
async def setleague(interaction: discord.Interaction, league_id: int, scope: str = "server"):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild:
        await interaction.followup.send("This command can only be used inside a server.")
        return

    scope_value = scope or "server"
    permissions = interaction.user.guild_permissions
    # Check permissions: Allow Manage Guild/Channels, Administrator, or Server Owner
    is_server_owner = interaction.user.id == interaction.guild.owner_id
    base_perm = permissions.manage_guild if scope_value == "server" else permissions.manage_channels
    has_permission = base_perm or permissions.administrator or is_server_owner

    if not has_permission and not await interaction.client.is_owner(interaction.user):
        required = "Manage Server" if scope_value == "server" else "Manage Channels"
        await interaction.followup.send(f"You need the **{required}** permission to set the league in this scope.")
        return

    league_data = await fetch_fpl_api(
        bot.session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings"
    )

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


        super().__init__(timeout=86400) # 24 hours


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


        self.clear_items() # Disable buttons


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


        self.clear_items() # Disable buttons


        await interaction.message.edit(embed=embed, view=self)





        # Notify user


        new_user = await interaction.client.fetch_user(self.new_user_id)


        team_data = await asyncio.to_thread(get_team_by_fpl_id, self.fpl_team_id)


        await new_user.send(f"Your claim for **{team_data['team_name']}** in the server **{interaction.guild.name}** was denied.")








@bot.tree.command(name="setadminchannel", description="Sets the channel for admin notifications.")


@app_commands.describe(channel="The channel to be used for admin notifications.")


@app_commands.checks.has_permissions(manage_guild=True)


async def setadminchannel(interaction: discord.Interaction, channel: discord.TextChannel):


    """Sets the admin channel for this server."""


    await interaction.response.defer(ephemeral=True)


    league_config.setdefault("admin_channels", {})


    league_config["admin_channels"][str(interaction.guild_id)] = channel.id


    save_league_config()


    await interaction.followup.send(f"Admin channel has been set to {channel.mention}.")





@setadminchannel.error


async def setadminchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):


    if isinstance(error, app_commands.MissingPermissions):


        await interaction.response.send_message("You need the `Manage Server` permission to use this command.", ephemeral=True)


    else:


        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)


        raise error








@bot.tree.command(name="claim", description="Claim your FPL team to link it to your Discord account for this server.")


@app_commands.describe(team="The FPL team you want to claim.")


async def claim(interaction: discord.Interaction, team: str):


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





    # Check if team is already claimed IN THIS GUILD


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

@assign.error
async def assign_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the `Manage Server` permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
        raise error


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
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if not bootstrap_data:
        await interaction.followup.send("Could not fetch FPL bootstrap data.")
        return

    gw_event = next((event for event in bootstrap_data.get('events', []) if event['is_current']), None)
    if not gw_event:
        gw_event = next((event for event in sorted(bootstrap_data.get('events', []), key=lambda x: x['id'], reverse=True) if event['finished']), None)
    
    if not gw_event:
        await interaction.followup.send("Could not determine the current or last gameweek.")
        return
        
    current_gw = gw_event['id']
    is_finished = gw_event['finished']

    # Try to use the live cache if it's for the correct gameweek
    live_data = bot.live_fpl_data
    if not live_data or live_data.get('gw') != current_gw:
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")

    if not live_data:
        await interaction.followup.send(f"Could not fetch data for Gameweek {current_gw}.")
        return

    # --- Fetch league data ---
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw
    )
    if not league_data:
        await interaction.followup.send("Failed to fetch FPL league data.")
        return

    live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}
    all_players_map = {p['id']: p for p in bootstrap_data.get('elements', [])}
    
    tasks = [get_live_manager_details(session, mgr, current_gw, live_points_map, all_players_map, live_data, is_finished=is_finished) for mgr in league_data.get('standings', {}).get('results', [])]
    all_manager_data = await asyncio.gather(*tasks)
    
    manager_live_scores = [d for d in all_manager_data if d is not None]
    manager_live_scores.sort(key=lambda x: x['live_total_points'], reverse=True)
    
    live_rank = "N/A"
    selected_manager_details = None
    for i, mgr_data in enumerate(manager_live_scores):
        if mgr_data['id'] == manager_id:
            live_rank = i + 1
            selected_manager_details = mgr_data
            break
    
    if not selected_manager_details:
        await interaction.followup.send("Could not calculate live data for the selected manager.")
        return

    summary_data = {
        "rank": live_rank,
        "gw_points": selected_manager_details['final_gw_points'],
        "total_points": selected_manager_details['live_total_points']
    }

    fpl_data_for_image = {
        "bootstrap": bootstrap_data,
        "live": live_data,
        "picks": selected_manager_details['picks_data']
    }
    
    image_bytes = await asyncio.to_thread(generate_team_image, fpl_data_for_image, summary_data, is_finished=is_finished)
    if image_bytes:
        file = discord.File(fp=image_bytes, filename="fpl_team.png")
        manager_name = selected_manager_details.get('name', 'Manager')
        await interaction.followup.send(f"**{manager_name}'s Team for GW {current_gw}**", file=file)
    else:
        await interaction.followup.send("Sorry, there was an error creating the team image.")

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
    return choices[:25]

@bot.tree.command(name="table", description="Displays the live FPL league table.")
async def table(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    # --- Gameweek and Data determination ---
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
    if not bootstrap_data:
        await interaction.followup.send("Could not fetch FPL bootstrap data.")
        return

    # Find the current or last finished gameweek
    gw_event = next((event for event in bootstrap_data.get('events', []) if event['is_current']), None)
    if not gw_event:
        gw_event = next((event for event in sorted(bootstrap_data.get('events', []), key=lambda x: x['id'], reverse=True) if event['finished']), None)
    
    if not gw_event:
        await interaction.followup.send("Could not determine the current or last gameweek.")
        return
        
    current_gw = gw_event['id']
    is_finished = gw_event['finished']

    # Try to use the live cache if it's for the correct gameweek
    live_data = bot.live_fpl_data
    if not live_data or live_data.get('gw') != current_gw:
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")

    if not live_data:
        await interaction.followup.send(f"Could not fetch data for Gameweek {current_gw}.")
        return

    # --- Fetch league data ---
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw,
        force_refresh=not is_finished # Only force refresh standings for live gameweeks
    )
    if not league_data:
        await interaction.followup.send("Failed to fetch FPL league data.")
        return

    # --- Process and Display ---
    live_points_map = {p['id']: p['stats'] for p in live_data.get('elements', [])}
    all_players_map = {p['id']: p for p in bootstrap_data['elements']}
    
    tasks = [get_live_manager_details(session, manager, current_gw, live_points_map, all_players_map, live_data, is_finished=is_finished) for manager in league_data.get('standings', {}).get('results', [])]
    manager_details = [res for res in await asyncio.gather(*tasks) if res]

    manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

    # --- Build Table String ---
    def format_name(name):
        parts = name.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}. {parts[-1]}"
        return name

    TABLE_LIMIT = 25
    
    # Process manager names and find max length for padding
    processed_managers = []
    for i, manager in enumerate(manager_details[:TABLE_LIMIT]):
        processed_managers.append({
            'rank': i + 1,
            'name': format_name(manager['name']),
            'total': manager['live_total_points'],
            'gw': manager['final_gw_points']
        })
    
    # Calculate padding length
    max_len = 0
    if processed_managers:
        max_len = max(len(m['name']) for m in processed_managers)

    # Build the table line by line
    table_lines = []
    header = f"**🏆 {league_data['league']['name']} - Live GW {current_gw} Table 🏆**"
    table_lines.append("```")
    # Header for the code block
    table_lines.append(f"{'#':<3} {'Manager'.ljust(max_len)}  {'GW':>4}  {'Total':>6}")
    table_lines.append("-" * (max_len + 18))

    for m in processed_managers:
        padded_name = m['name'].ljust(max_len)
        table_lines.append(f"{str(m['rank']):<3} {padded_name}  {m['gw']:>4}  {m['total']:>6}")

    table_lines.append("```")

    has_next_page = league_data.get('standings', {}).get('has_next', False)
    if len(manager_details) > TABLE_LIMIT or has_next_page:
        league_url = f"https://fantasy.premierleague.com/leagues/{league_id}/standings/c"
        table_lines.append(f"Only showing top {TABLE_LIMIT}. View full table at <{league_url}>")

    await interaction.followup.send("\n".join(table_lines))

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

    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap",
        cache_gw=current_gw
    )
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw
    )

    if not bootstrap_data or not league_data:
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    all_players = {p['id']: p for p in bootstrap_data['elements']}
    selected_player = all_players.get(player_id)
    
    if not selected_player:
        await interaction.followup.send("Player not found.")
        return

    player_name = f"{selected_player['first_name']} {selected_player['second_name']}"
    
    tasks = []
    for manager in league_data['standings']['results']:
        manager_id = manager['entry']
        tasks.append(fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/",
            cache_key=f"picks_entry_{manager_id}",
            cache_gw=current_gw
        ))
    
    all_picks_data = await asyncio.gather(*tasks)
    
    owners = []
    benched = []
    for i, picks_data in enumerate(all_picks_data):
        if picks_data and 'picks' in picks_data:
            manager_name = league_data['standings']['results'][i]['player_name']
            for pick in picks_data['picks']:
                if pick['element'] == player_id:
                    if pick['position'] > 11:
                        benched.append(manager_name)
                    else:
                        owners.append(manager_name)
                    break
    
    embed = discord.Embed(
        title=f"Ownership for {player_name}",
        color=discord.Color.blue()
    )

    if not owners and not benched:
        embed.description = f"**{player_name}** is not owned by any managers in the league."
    else:
        if owners:
            embed.add_field(name=f"Owned By ({len(owners)})", value="\n".join(owners), inline=True)
        if benched:
            embed.add_field(name=f"Benched By ({len(benched)})", value="\n".join(benched), inline=True)
    
    await interaction.followup.send(embed=embed)

@player.autocomplete('player')
async def player_autocomplete(interaction: discord.Interaction, current: str):
    session = bot.session
    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap_autocomplete"
    )
    if not bootstrap_data:
        return []
    
    all_players = bootstrap_data['elements']
    choices = []
    
    for player in all_players:
        full_name = f"{player['first_name']} {player['second_name']}"
        web_name = player['web_name']
        if current.lower() in full_name.lower() or current.lower() in web_name.lower():
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
    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap",
        cache_gw=last_completed_gw
    )
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=last_completed_gw
    )
    completed_gw_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}event/{last_completed_gw}/live/",
        cache_key=f"event_live_gw{last_completed_gw}",
        cache_gw=last_completed_gw,
        force_refresh=True
    )

    if not all([bootstrap_data, league_data, completed_gw_data]):
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    all_players = {p['id']: p for p in bootstrap_data['elements']}
    completed_gw_stats = {p['id']: p['stats'] for p in completed_gw_data['elements']}
    
    # Get all unique players from all managers' squads for the completed gameweek
    all_squad_players = {}
    tasks = []
    for manager in league_data['standings']['results']:
        manager_id = manager['entry']
        tasks.append(fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/event/{last_completed_gw}/picks/",
            cache_key=f"picks_entry_{manager_id}",
            cache_gw=last_completed_gw
        ))
    
    all_picks_data = await asyncio.gather(*tasks)
    
    for picks_data in all_picks_data:
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
        "player_of_week": player_of_week
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
        await interaction.followup.send(f"🌟 **Dream Team for GW {last_completed_gw}** 🌟", file=file)
    else:
        await interaction.followup.send("Sorry, there was an error creating the dream team image.")

@bot.tree.command(name="transfers", description="Lists all transfers made by league managers for the current gameweek.")
async def transfers(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap",
        cache_gw=current_gw
    )
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw
    )

    if not bootstrap_data or not league_data:
        await interaction.followup.send("Failed to fetch FPL league data. Please try again later.")
        return

    player_lookup = {p['id']: p for p in bootstrap_data['elements']}
    managers = league_data['standings']['results']

    tasks = [
        get_manager_transfer_activity(session, manager['entry'], current_gw)
        for manager in managers
    ]
    manager_transfer_data = await asyncio.gather(*tasks)

    chip_labels = {
        "wildcard": "Wildcard 🪙",
        "freehit": "Free Hit 🪙"
    }

    # Process all managers and create a list of fields to be added to embeds
    fields = []
    for manager, data in zip(managers, manager_transfer_data):
        if data is None or (not data['transfers'] and not data['chip']):
            continue

        manager_name = manager['player_name']
        team_name = manager['entry_name']
        entry_id = manager['entry']
        
        status_tokens = []
        chip_label = chip_labels.get(data['chip'])
        if chip_label: status_tokens.append(chip_label)
        if data['transfer_cost']: status_tokens.append(f"-{data['transfer_cost']} pts")
        
        suffix = f" ({', '.join(status_tokens)})" if status_tokens else ""
        field_name = f"**{manager_name}**{suffix}"
        
        # Build the field value, with the link on the first line
        team_url = build_manager_url(entry_id, current_gw)
        transfer_lines = [f"[{team_name}]({team_url})"]

        if not data['transfers'] and chip_label:
             transfer_lines.append("No transfers made")
        else:
            for transfer in data['transfers']:
                out_player = player_lookup.get(transfer.get('element_out'))
                in_player = player_lookup.get(transfer.get('element_in'))
                out_name = out_player['web_name'] if out_player else "Unknown"
                in_name = in_player['web_name'] if in_player else "Unknown"
                out_price = f"£{(transfer.get('element_out_cost', 0) or 0) / 10:.1f}m"
                in_price = f"£{(transfer.get('element_in_cost', 0) or 0) / 10:.1f}m"
                transfer_lines.append(f"❌ {out_name} ({out_price}) ➜ ✅ {in_name} ({in_price})")

        field_value = "\n".join(transfer_lines)
        # Add a blank line for spacing if there are transfers to show
        if transfer_lines:
             field_value += "\n\u200b"
        fields.append({'name': field_name, 'value': field_value, 'inline': False})

    if not fields:
        await interaction.followup.send(f"No transfers or chips played in GW {current_gw}.")
        return

    # Send embeds in chunks of 25 fields
    for i in range(0, len(fields), 25):
        chunk = fields[i:i+25]
        
        embed = discord.Embed(
            title=f"Gameweek {current_gw} Transfers",
            color=discord.Color.blue()
        )
        if i > 0: # Add a page number for subsequent embeds
            embed.title += f" (Page {i//25 + 1})"

        for field in chunk:
            embed.add_field(name=field['name'], value=field['value'], inline=False)
        
        if i == 0:
            await interaction.followup.send(embed=embed)
        else:
            await interaction.channel.send(embed=embed)


@bot.tree.command(name="captains", description="Shows which player each manager captained for the current gameweek.")
async def captains(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap",
        cache_gw=current_gw
    )
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw
    )

    if not bootstrap_data or not league_data:
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    all_players = {p['id']: p for p in bootstrap_data['elements']}
    
    tasks = []
    for manager in league_data['standings']['results']:
        manager_id = manager['entry']
        tasks.append(fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/",
            cache_key=f"picks_entry_{manager_id}",
            cache_gw=current_gw
        ))
    
    all_picks_data = await asyncio.gather(*tasks)
    
    captain_info = []
    for i, picks_data in enumerate(all_picks_data):
        if picks_data and 'picks' in picks_data:
            manager_entry = league_data['standings']['results'][i]
            manager_name = manager_entry['player_name']
            manager_id = manager_entry['entry']
            
            captain_pick = next((p for p in picks_data['picks'] if p['is_captain']), None)
            
            if captain_pick:
                captain_id = captain_pick['element']
                captain_player_info = all_players.get(captain_id)
                if captain_player_info:
                    captain_name = f"{captain_player_info['first_name']} {captain_player_info['second_name']}"
                    manager_link = format_manager_link(manager_name, manager_id, current_gw)
                    captain_info.append(f"{manager_link} - **{captain_name}**")
    
    embed = discord.Embed(
        title=f"Captain Choices for GW {current_gw}",
        color=discord.Color.blue()
    )

    if captain_info:
        embed.description = "\n".join(captain_info)
    else:
        embed.description = "No captain information found for the current gameweek."
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="fixtures", description="Shows the upcoming fixtures for a team or all teams.")
@app_commands.describe(team="The team to show fixtures for. Leave blank for all teams.")
async def fixtures(interaction: discord.Interaction, team: str = None):
    await interaction.response.defer()

    session = bot.session
    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap"
    )
    fixtures_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}fixtures/",
        cache_key="fixtures"
    )

    if not bootstrap_data or not fixtures_data:
        await interaction.followup.send("Failed to fetch FPL data. Please try again later.")
        return

    teams_map = {t['id']: t for t in bootstrap_data['teams']}
    
    embed = discord.Embed(title="Upcoming Fixtures", color=discord.Color.blue())

    team_id_to_show = int(team) if team else None

    def get_fdr_emoji(difficulty):
        if difficulty <= 2:
            return "🟩"
        elif difficulty == 3:
            return "⬜"
        elif difficulty == 4:
            return "🟧"
        else:
            return "🟥"

    upcoming_fixtures = [
        f for f in fixtures_data 
        if not f.get('finished', True) and f.get('event') and current_gw <= f['event'] < current_gw + 5
    ]
    
    if team_id_to_show:
        team_name = teams_map[team_id_to_show]['name']
        team_fixtures = [f for f in upcoming_fixtures if f['team_h'] == team_id_to_show or f['team_a'] == team_id_to_show]
        
        fixture_lines = []
        for f in sorted(team_fixtures, key=lambda x: x['event']):
            is_home = f['team_h'] == team_id_to_show
            opponent = teams_map[f['team_a'] if is_home else f['team_h']]['short_name']
            difficulty = f['team_h_difficulty'] if is_home else f['team_a_difficulty']
            fdr_emoji = get_fdr_emoji(difficulty)
            venue = "(H)" if is_home else "(A)"
            fixture_lines.append(f"GW{f['event']}: vs {opponent} {venue} {fdr_emoji}")
        
        if fixture_lines:
            embed.add_field(name=team_name, value="\n".join(fixture_lines), inline=False)
        else:
            embed.description = f"No upcoming fixtures found for {team_name} in the next 4 gameweeks."

    else: # All teams
        fixtures_by_team = {team_id: [] for team_id in teams_map}
        for f in upcoming_fixtures:
            gw_str = f"GW{f['event']}"
            # Home team
            h_opponent = teams_map[f['team_a']]['short_name']
            h_fdr = get_fdr_emoji(f['team_h_difficulty'])
            fixtures_by_team[f['team_h']].append(f"{gw_str:<5} {h_opponent}(H) {h_fdr}")
            # Away team
            a_opponent = teams_map[f['team_h']]['short_name']
            a_fdr = get_fdr_emoji(f['team_a_difficulty'])
            fixtures_by_team[f['team_a']].append(f"{gw_str:<5} {a_opponent}(A) {a_fdr}")

        # Create a list of fields to be added
        fields_to_add = []
        for team_id, team_data in sorted(teams_map.items(), key=lambda x: x[1]['name']):
            if fixtures_by_team[team_id]:
                # Sort this team's fixtures by gameweek
                sorted_fixtures = sorted(fixtures_by_team[team_id], key=lambda x: int(x.split()[0][2:]))
                value_string = "```\n" + "\n".join(sorted_fixtures) + "\n```"
                fields_to_add.append({'name': team_data['name'], 'value': value_string, 'inline': True})
        
        # Add fields in chunks of 3 to ensure alignment
        for i in range(0, len(fields_to_add), 3):
            chunk = fields_to_add[i:i+3]
            for field in chunk:
                embed.add_field(name=field['name'], value=field['value'], inline=True)
            # Add blank fields to fill the row if it's not a full row of 3
            if len(chunk) < 3:
                for _ in range(3 - len(chunk)):
                    embed.add_field(name='\u200b', value='\u200b', inline=True)

    await interaction.followup.send(embed=embed)


@fixtures.autocomplete('team')
async def fixtures_autocomplete(interaction: discord.Interaction, current: str):
    session = bot.session
    bootstrap_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}bootstrap-static/",
        cache_key="bootstrap_autocomplete"
    )
    if not bootstrap_data:
        return []
    
    all_teams = bootstrap_data['teams']
    choices = []
    
    for team in all_teams:
        team_name = team['name']
        if current.lower() in team_name.lower():
            choices.append(app_commands.Choice(name=team_name, value=str(team['id'])))
    
    return sorted(choices, key=lambda x: x.name)[:25]


@bot.tree.command(name="h2h", description="Compare two FPL teams for the current gameweek.")
@app_commands.describe(manager_a="The first manager to compare.", manager_b="The second manager to compare.")
@app_commands.autocomplete(manager_a=team_autocomplete)
@app_commands.autocomplete(manager_b=team_autocomplete)
async def h2h(interaction: discord.Interaction, manager_a: str, manager_b: str):
    await interaction.response.defer()

    if manager_a == manager_b:
        await interaction.followup.send("You can't compare a manager against themselves.", ephemeral=True)
        return

    # Get FPL IDs and names
    try:
        p1_fpl_id = int(manager_a)
        p2_fpl_id = int(manager_b)
    except ValueError:
        await interaction.followup.send("Invalid team selection. Please choose a team from the autocomplete list.", ephemeral=True)
        return
    
    manager1_data = await asyncio.to_thread(get_team_by_fpl_id, p1_fpl_id)
    manager2_data = await asyncio.to_thread(get_team_by_fpl_id, p2_fpl_id)

    if not manager1_data or not manager2_data:
        await interaction.followup.send("Could not find one or both of the selected managers.", ephemeral=True)
        return

    player1_name = manager1_data['manager_name']
    player2_name = manager2_data['manager_name']

    session = bot.session
    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return

    # Fetch picks for both managers
    async def get_picks(fpl_id):
        return await fetch_fpl_api(
            session,
            f"{BASE_API_URL}entry/{fpl_id}/event/{current_gw}/picks/",
            cache_key=f"picks_entry_{fpl_id}",
            cache_gw=current_gw
        )

    picks1_data, picks2_data = await asyncio.gather(get_picks(p1_fpl_id), get_picks(p2_fpl_id))

    if not picks1_data or not picks2_data:
        await interaction.followup.send("Could not fetch player picks. Please try again later.")
        return

    team1_players = {p['element'] for p in picks1_data['picks']}
    team2_players = {p['element'] for p in picks2_data['picks']}

    common_players = team1_players.intersection(team2_players)
    diffs1 = team1_players.difference(team2_players)
    diffs2 = team2_players.difference(team1_players)

    # Get player names
    bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/", cache_key="bootstrap")
    if not bootstrap_data:
        await interaction.followup.send("Could not fetch player data.")
        return
    player_map = {p['id']: p['web_name'] for p in bootstrap_data['elements']}

    def get_player_names(ids):
        return [player_map.get(p_id, "Unknown") for p_id in ids]

    diffs1_names = sorted(get_player_names(diffs1))
    diffs2_names = sorted(get_player_names(diffs2))

    embed = discord.Embed(
        title=f"Head to Head: {player1_name} vs {player2_name}",
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"{len(common_players)} players in common.")

    embed.add_field(name=f"{player1_name}'s Differentials", value="\n".join(diffs1_names) or "None", inline=True)
    embed.add_field(name=f"{player2_name}'s Differentials", value="\n".join(diffs2_names) or "None", inline=True)
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="shame", description="Highlights the biggest manager mistakes from the last completed gameweek.")
async def shame(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    completed_gw = await get_last_completed_gameweek(session)
    if not completed_gw:
        await interaction.followup.send("Could not determine the last completed gameweek.")
        return

    # --- Data Fetching ---
    bootstrap_data, gw_data = await asyncio.gather(
        fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/", cache_key="bootstrap"),
        fetch_fpl_api(session, f"{BASE_API_URL}event/{completed_gw}/live/", cache_key=f"event_live_gw{completed_gw}", cache_gw=completed_gw)
    )
    if not bootstrap_data or not gw_data:
        await interaction.followup.send("Failed to fetch FPL data.")
        return
        
    player_map = {p['id']: p for p in bootstrap_data['elements']}
    points_map = {p['id']: p['stats'] for p in gw_data['elements']}
    
    all_league_teams = await asyncio.to_thread(get_all_league_teams, interaction.guild_id, league_id)
    if not all_league_teams:
        await interaction.followup.send("No teams found for this league.")
        return

    all_league_teams = all_league_teams[:25]
        
    # --- Metric Calculation ---
    manager_metrics = []
    
    async def get_manager_metrics(user):
        fpl_id = user['fpl_team_id']
        
        picks_data, transfers_data = await asyncio.gather(
            fetch_fpl_api(session, f"{BASE_API_URL}entry/{fpl_id}/event/{completed_gw}/picks/", cache_key=f"picks_entry_{fpl_id}", cache_gw=completed_gw),
            fetch_fpl_api(session, f"{BASE_API_URL}entry/{fpl_id}/transfers/", cache_key=f"transfers_entry_{fpl_id}", cache_gw=completed_gw)
        )

        if not picks_data or 'picks' not in picks_data:
            return None

        # 1. Bench Points
        bench_points = sum(
            points_map.get(p['element'], {}).get('total_points', 0)
            for p in picks_data['picks'] if p['position'] > 11
        )

        # 2. Captain Points
        captain_pick = next((p for p in picks_data['picks'] if p['is_captain']), None)
        vice_pick = next((p for p in picks_data['picks'] if p['is_vice_captain']), None)
        
        effective_captain_id = None
        effective_captain_points = -1

        if captain_pick:
            captain_minutes = points_map.get(captain_pick['element'], {}).get('minutes', 0)
            if captain_minutes == 0 and vice_pick:
                effective_captain_id = vice_pick['element']
            else:
                effective_captain_id = captain_pick['element']
        
        if effective_captain_id:
            effective_captain_points = points_map.get(effective_captain_id, {}).get('total_points', 0)

        # 3. Transfer Flop
        highest_transfer_out_points = -1
        highest_scoring_transfer_out_name = ""
        if transfers_data:
            transfers_this_week = [t for t in transfers_data if t.get("event") == completed_gw]
            for transfer in transfers_this_week:
                out_player_id = transfer['element_out']
                out_player_points = points_map.get(out_player_id, {}).get('total_points', 0)
                if out_player_points > highest_transfer_out_points:
                    highest_transfer_out_points = out_player_points
                    highest_scoring_transfer_out_name = player_map.get(out_player_id, {}).get('web_name', 'Unknown')
        
        return {
            "discord_id": user['discord_user_id'],
            "manager_name": user['manager_name'],
            "bench_points": bench_points,
            "captain_id": effective_captain_id,
            "captain_points": effective_captain_points,
            "highest_transfer_out_points": highest_transfer_out_points,
            "highest_scoring_transfer_out_name": highest_scoring_transfer_out_name
        }

    tasks = [get_manager_metrics(user) for user in all_league_teams]
    results = [res for res in await asyncio.gather(*tasks) if res is not None]

    if not results:
        await interaction.followup.send(f"Could not calculate shame metrics for GW {completed_gw}.")
        return

    # --- Find the "Winners" ---
    most_benched = max(results, key=lambda x: x['bench_points'])
    worst_captain = min(results, key=lambda x: x['captain_points'])
    biggest_flop = max(results, key=lambda x: x['highest_transfer_out_points'])

    # --- Formatting Output ---
    embed = discord.Embed(
        title=f"🤡 Gameweek {completed_gw} Wall of Shame 🤡",
        color=discord.Color.dark_orange()
    )
    
    shame_found = False
    # Most Points Benched
    if most_benched and most_benched['bench_points'] > 0:
        user_mention = f"<@{most_benched['discord_id']}>" if most_benched['discord_id'] else f"**{most_benched['manager_name']}**"
        embed.add_field(name="Most Points Benched", value=f"{user_mention} ({most_benched['bench_points']} points)", inline=False)
        shame_found = True

    # Worst Captain
    if worst_captain and worst_captain['captain_id']:
        user_mention = f"<@{worst_captain['discord_id']}>" if worst_captain['discord_id'] else f"**{worst_captain['manager_name']}**"
        captain_name = player_map.get(worst_captain['captain_id'], {}).get('web_name', 'Unknown')
        embed.add_field(name="Worst Captain Choice", value=f"{user_mention} (Captain {captain_name}: {worst_captain['captain_points']} pts)", inline=False)
        shame_found = True

    # Biggest Transfer Flop
    if biggest_flop and biggest_flop['highest_transfer_out_points'] > 0:
        user_mention = f"<@{biggest_flop['discord_id']}>" if biggest_flop['discord_id'] else f"**{biggest_flop['manager_name']}**"
        embed.add_field(name="Biggest Transfer Flop", value=f"{user_mention} (Transferred out {biggest_flop['highest_scoring_transfer_out_name']}: {biggest_flop['highest_transfer_out_points']} pts)", inline=False)
        shame_found = True
    
    if not shame_found:
        embed.description = f"🎉 No manager mistakes found for GW {completed_gw}! Everyone is a winner."

    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("!!! ERROR: DISCORD_BOT_TOKEN not found in .env file. Please create a .env file with your bot token.")
    else:
        bot.run(DISCORD_BOT_TOKEN)

