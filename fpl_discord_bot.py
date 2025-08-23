import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import os
from PIL import Image, ImageDraw, ImageFont
import io
import asyncio

# --- CONFIGURATION ---
FPL_LEAGUE_ID = "70625"
DISCORD_BOT_TOKEN = "YOUR_DISCORD_BOT_TOKEN_HERE"  # 🚨 IMPORTANT: Replace with your actual bot token

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


class FPLBot(commands.Bot):
    """A Discord bot for displaying FPL league and team information."""
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("Bot is ready and online.")

bot = FPLBot()

# --- FPL API HELPER FUNCTIONS (Async) ---
async def fetch_fpl_api(session, url):
    """Fetches data from the FPL API asynchronously."""
    try:
        async with session.get(url, headers=REQUEST_HEADERS) as response:
            if response.status == 200:
                return await response.json()
            else:
                print(f"Error fetching {url}: Status {response.status}")
                return None
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

async def get_league_managers(session):
    """Fetches all manager names and IDs for the configured league."""
    league_url = f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/"
    league_data = await fetch_fpl_api(session, league_url)
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
    picks_task = fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/event/{current_gw}/picks/")
    history_task = fetch_fpl_api(session, f"{BASE_API_URL}entry/{manager_id}/history/")
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

def generate_team_image(fpl_data, summary_data):
    try:
        background = Image.open(BACKGROUND_IMAGE_PATH).convert("RGBA")
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        captain_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE)
        summary_font = ImageFont.truetype(FONT_PATH, SUMMARY_FONT_SIZE)
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
        player_points = live_points.get(player_id, 0) * player_pick.get('multiplier', 1)
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

        name_text, points_text = player_name, f"{player_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = max(name_bbox[2], points_bbox[2]) + 10
        name_box_height = (name_bbox[3] - name_bbox[1]) + 4
        name_box_x = x - box_width // 2
        name_box_y = y + 55
        points_box_height = (points_bbox[3] - points_bbox[1]) + 4
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill=(0, 135, 81, 150))
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            draw.text((paste_x + 80, paste_y - 5), "C", font=captain_font, fill="black", stroke_width=2, stroke_fill="yellow")
        elif player_pick['is_vice_captain']:
            draw.text((paste_x + 80, paste_y - 5), "V", font=captain_font, fill="black", stroke_width=2, stroke_fill="white")

    summary_strings = [f"League Rank: {summary_data['rank']}", f"GW Points: {summary_data['gw_points']}", f"Total Points: {summary_data['total_points']}"]
    for i, text in enumerate(summary_strings):
        y_pos = SUMMARY_Y_START + (i * SUMMARY_LINE_SPACING)
        text_bbox = draw.textbbox((0, 0), text, font=summary_font)
        text_width = text_bbox[2] - text_bbox[0]
        x_pos = SUMMARY_X - text_width
        draw.text((x_pos, y_pos), text, font=summary_font, fill="white", stroke_width=1, stroke_fill="black")

    img_byte_arr = io.BytesIO()
    background.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

# --- DISCORD SLASH COMMANDS ---

@bot.tree.command(name="team", description="Generates an image of a manager's current FPL team.")
@app_commands.describe(manager="Select the manager's team to view.")
async def team(interaction: discord.Interaction, manager: str):
    await interaction.response.defer()
    manager_id = int(manager)

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return

        bootstrap_data = await fetch_fpl_api(session, f"{BASE_API_URL}bootstrap-static/")
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")

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
        
        image_bytes = generate_team_image(fpl_data_for_image, summary_data)
        if image_bytes:
            file = discord.File(fp=image_bytes, filename="fpl_team.png")
            await interaction.followup.send(file=file)
        else:
            await interaction.followup.send("Sorry, there was an error creating the team image.")

@team.autocomplete('manager')
async def team_autocomplete(interaction: discord.Interaction, current: str):
    async with aiohttp.ClientSession() as session:
        managers = await get_league_managers(session)
        choices = [app_commands.Choice(name=name, value=str(id)) for name, id in managers.items() if current.lower() in name.lower()]
        return choices[:25]

@bot.tree.command(name="table", description="Displays the live FPL league table.")
async def table(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiohttp.ClientSession() as session:
        current_gw = await get_current_gameweek(session)
        if not current_gw:
            await interaction.followup.send("Could not determine the current gameweek.")
            return
            
        league_data = await fetch_fpl_api(session, f"{BASE_API_URL}leagues-classic/{FPL_LEAGUE_ID}/standings/")
        live_data = await fetch_fpl_api(session, f"{BASE_API_URL}event/{current_gw}/live/")

        if not league_data or not live_data:
            await interaction.followup.send("Failed to fetch FPL league data. Please try again later.")
            return

        live_points_map = {p['id']: p['stats'] for p in live_data['elements']}
        
        tasks = [get_live_manager_details(session, manager, current_gw, live_points_map) for manager in league_data['standings']['results']]
        manager_details = [res for res in await asyncio.gather(*tasks) if res]

        manager_details.sort(key=lambda x: x['live_total_points'], reverse=True)

        header = f"🏆 {league_data['league']['name']} - Live GW {current_gw} Table 🏆"
        table_content = "```"
        table_content += f"{'Rank':<5} {'Manager':<20} {'GW Pts':<8} {'Total':<8} {'Played':<8}\n"
        table_content += "-" * 52 + "\n"

        for i, manager in enumerate(manager_details):
            rank = i + 1
            table_content += f"{str(rank):<5} {manager['name']:<20.19} {manager['final_gw_points']:<8} {manager['live_total_points']:<8} {manager['players_played']}/11\n"
        
        table_content += "```"
        await interaction.followup.send(f"{header}\n{table_content}")


if __name__ == "__main__":
    if DISCORD_BOT_TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        print("!!! ERROR: Please replace 'YOUR_DISCORD_BOT_TOKEN_HERE' with your actual bot token in the script.")
    else:
        bot.run(DISCORD_BOT_TOKEN)