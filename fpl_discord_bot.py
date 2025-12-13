import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import io
import asyncio
from dotenv import load_dotenv
from typing import Literal

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CONFIG_PATH = Path("config/league_config.json")
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
        super().__init__(command_prefix="!", intents=intents)
        self.session = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.api_semaphore = asyncio.Semaphore(10)
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}.")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
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
async def get_live_manager_details(session, manager_entry, current_gw, live_points_map):
    """Fetches picks/history for a manager and calculates their live score, accounting for chips."""
    manager_id = manager_entry['entry']
    picks_task = fetch_fpl_api(
        session,
        f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/",
        cache_key=f"picks_entry_{manager_id}",
        cache_gw=current_gw
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

    # Determine which players' points to count based on active chip
    active_chip = picks_data.get('active_chip')
    players_to_score = []
    if active_chip == 'bboost':
        players_to_score = picks_data['picks']  # Count all 15 players for Bench Boost
    else:
        players_to_score = [p for p in picks_data['picks'] if p['position'] <= 11]  # Count starting 11

    # Calculate points
    live_gw_points = sum(live_points_map.get(p['element'], {}).get('total_points', 0) * p['multiplier'] for p in players_to_score)
    transfer_cost = picks_data['entry_history']['event_transfers_cost']
    final_gw_points = live_gw_points - transfer_cost

    # Calculate total points
    pre_gw_total = 0
    if current_gw > 1:
        prev_gw_history = next((gw for gw in history_data['current'] if gw['event'] == current_gw - 1), None)
        if prev_gw_history:
            pre_gw_total = prev_gw_history['total_points']
    
    live_total_points = pre_gw_total + final_gw_points

    # Calculate players played for the table view (always just the starting XI)
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
    return f"[{label}](<{url}>)"


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

def generate_team_image(fpl_data, summary_data):
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
    live_points = {p['id']: p['stats']['total_points'] for p in fpl_data['live']['elements']}
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players)

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
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill="#015030")
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            draw.text((paste_x + 80, paste_y - 5), "C", font=captain_font, fill="black", stroke_width=2, stroke_fill="yellow")
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

    location = "this server" if scope_value == "server" else f"{interaction.channel.mention}"
    league_name = league_data['league']['name']
    await interaction.followup.send(f"League set to **{league_name}** ({league_id}) for {location}.")


@bot.tree.command(name="team", description="Generates an image of a manager's current FPL team.")
@app_commands.describe(manager="Select the manager's team to view.")
async def team(interaction: discord.Interaction, manager: str):
    await interaction.response.defer()
    manager_id = int(manager)

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
    live_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}event/{current_gw}/live/",
        cache_key="event_live",
        cache_gw=current_gw
    )
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw
    )

    if not all([bootstrap_data, live_data, league_data]):
        await interaction.followup.send("Failed to fetch essential FPL data. Please try again later.")
        return

    live_points_map = {p['id']: p['stats'] for p in live_data['elements']}
    
    tasks = [get_live_manager_details(session, mgr, current_gw, live_points_map) for mgr in league_data['standings']['results']]
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
    
    image_bytes = await asyncio.to_thread(generate_team_image, fpl_data_for_image, summary_data)
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

    managers = await get_league_managers(bot.session, league_id)
    choices = [app_commands.Choice(name=name, value=str(id)) for name, id in managers.items() if current.lower() in name.lower()]
    return choices[:25]

@bot.tree.command(name="table", description="Displays the live FPL league table.")
async def table(interaction: discord.Interaction):
    await interaction.response.defer()

    session = bot.session
    league_id = await ensure_league_id(interaction)
    if not league_id:
        return

    current_gw = await get_current_gameweek(session)
    if not current_gw:
        await interaction.followup.send("Could not determine the current gameweek.")
        return
        
    league_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}leagues-classic/{league_id}/standings/",
        cache_key=f"league_{league_id}_standings",
        cache_gw=current_gw,
        force_refresh=True
    )
    live_data = await fetch_fpl_api(
        session,
        f"{BASE_API_URL}event/{current_gw}/live/",
        cache_key="event_live",
        cache_gw=current_gw,
        force_refresh=True
    )

    if not league_data or not live_data:
        await interaction.followup.send("Failed to fetch FPL league data. Please try again later.")
        return

    live_points_map = {p['id']: p['stats'] for p in live_data['elements']}
    
    tasks = [get_live_manager_details(session, manager, current_gw, live_points_map) for manager in league_data['standings']['results']]
    manager_details = [res for res in await asyncio.gather(*tasks) if res]

    manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

    header = f"**🏆 {league_data['league']['name']} - Live GW {current_gw} Table 🏆**"
    TABLE_LIMIT = 25
    table_content = "```"
    table_content += f"{'Rank':<5} {'Manager':<20} {'GW Pts':<8} {'Total':<8} {'Played':<8}\n"
    table_content += "-" * 52 + "\n"

    for i, manager in enumerate(manager_details[:TABLE_LIMIT]):
        rank = i + 1
        table_content += (
            f"{str(rank):<5} {manager['name']:<20.19} "
            f"{manager['final_gw_points']:<8} {manager['live_total_points']:<8} "
            f"{manager['players_played']}/11\n"
        )

    table_content += "```"
    message = f"{header}\n{table_content}"

    has_next_page = league_data['standings'].get('has_next', False)
    if len(manager_details) > TABLE_LIMIT or has_next_page:
        league_url = f"https://fantasy.premierleague.com/leagues/{league_id}/standings/c"
        message += f"\nView the full table at <{league_url}>"

    await interaction.followup.send(message)

@bot.tree.command(name="player", description="Shows which managers in the league own a specific player.")
@app_commands.describe(player="Select the player to check ownership for.")
async def player(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    player_id = int(player)

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
    for i, picks_data in enumerate(all_picks_data):
        if picks_data and 'picks' in picks_data:
            manager_name = league_data['standings']['results'][i]['player_name']
            for pick in picks_data['picks']:
                if pick['element'] == player_id:
                    # Check if player is on bench (positions 12-15) or starting XI (positions 1-11)
                    if pick['position'] > 11:
                        owners.append(f"{manager_name} **(B)**")  # On bench
                    else:
                        owners.append(manager_name)  # Starting XI
                    break
    
    if owners:
        owner_count = len(owners)
        response = f"**{player_name}** features in **{owner_count}** team{'s' if owner_count != 1 else ''}:\n"
        for owner in owners:
            response += f"• {owner}\n"
    else:
        response = f"**{player_name}** is not owned by any managers in the league."
    
    await interaction.followup.send(response)

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
        cache_key="event_live",
        cache_gw=last_completed_gw
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

    # Group output into blocks (one block per manager) to prevent splitting a manager across messages
    blocks = []
    blocks.append([f"**Gameweek {current_gw} Transfers**", ""])

    for manager, data in zip(managers, manager_transfer_data):
        manager_block = []
        manager_name = manager['player_name']
        team_name = manager['entry_name']
        entry_id = manager['entry']
        manager_link = format_manager_link(manager_name, entry_id, current_gw)
        team_link = format_manager_link(team_name, entry_id, current_gw)

        if data is None:
            manager_block.append(f"{manager_link} - {team_link}:")
            manager_block.append("    Unable to retrieve transfer data.")
            blocks.append(manager_block)
            continue

        status_tokens = []
        chip_label = chip_labels.get(data['chip'])
        if chip_label:
            status_tokens.append(chip_label)
        if data['transfer_cost']:
            status_tokens.append(f"-{data['transfer_cost']} pts")

        suffix = f" ({', '.join(status_tokens)})" if status_tokens else ""
        transfers_this_week = data['transfers']
        if not transfers_this_week and not status_tokens:
            continue

        manager_block.append(f"**{manager_link} - {team_link}{suffix}**")

        for transfer in transfers_this_week:
            out_player = player_lookup.get(transfer.get('element_out'))
            in_player = player_lookup.get(transfer.get('element_in'))

            out_name = out_player['web_name'] if out_player else "Unknown"
            in_name = in_player['web_name'] if in_player else "Unknown"

            out_cost = transfer.get('element_out_cost')
            in_cost = transfer.get('element_in_cost')

            if out_cost is None and out_player:
                out_cost = out_player.get('now_cost')
            if in_cost is None and in_player:
                in_cost = in_player.get('now_cost')

            out_price = f"£{(out_cost or 0) / 10:.1f}m"
            in_price = f"£{(in_cost or 0) / 10:.1f}m"

            manager_block.append(f"    ❌ {out_name} ({out_price}) ➜ ✅ {in_name} ({in_price})")
        manager_block.append("")
        blocks.append(manager_block)

    # Send in chunks, keeping blocks intact
    current_chunk = []
    current_length = 0
    for block in blocks:
        block_len = sum(len(line) + 1 for line in block)
        
        if current_length + block_len > 1900 and current_chunk:
            await interaction.followup.send("\n".join(current_chunk))
            current_chunk = []
            current_length = 0
        
        current_chunk.extend(block)
        current_length += block_len

    if current_chunk:
        await interaction.followup.send("\n".join(current_chunk))


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
    
    # Get all managers' picks for current gameweek
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
            
            # Find the captain (is_captain = True)
            captain_pick = None
            for pick in picks_data['picks']:
                if pick['is_captain']:
                    captain_pick = pick
                    break
            
            if captain_pick:
                captain_id = captain_pick['element']
                captain_player_info = all_players[captain_id]
                captain_name = f"{captain_player_info['first_name']} {captain_player_info['second_name']}"
                captain_info.append((manager_name, manager_id, captain_name))
    
    if captain_info:
        response = f"**Captain choices for GW {current_gw}:**\n\n"
        for manager_name, manager_id, captain_name in captain_info:
            manager_link = format_manager_link(manager_name, manager_id, current_gw)
            response += f"{manager_link} - **{captain_name}**\n"
    else:
        response = "No captain information found for the current gameweek."
    
    await interaction.followup.send(response)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("!!! ERROR: DISCORD_BOT_TOKEN not found in .env file. Please create a .env file with your bot token.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
