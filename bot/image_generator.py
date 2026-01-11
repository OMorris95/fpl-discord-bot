"""Image generation functions for the FPL Discord bot."""

import os
import io
from PIL import Image, ImageDraw, ImageFont

# --- File Paths ---
BACKGROUND_IMAGE_PATH = "mobile-pitch-graphic.png"
FONT_PATH = "font.ttf"
JERSEYS_DIR = "team_jerseys_test"
JERSEY_SIZE = (94, 125)  # ~10% smaller than original 104x146, maintains aspect ratio

# --- Layout & Styling ---
NAME_FONT_SIZE = 18
POINTS_FONT_SIZE = 18
CAPTAIN_FONT_SIZE = 22
SUMMARY_FONT_SIZE = 24
POINTS_BOX_EXTRA_PADDING = 4
PLAYER_BOX_WIDTH = 110  # Fixed width to fit "XXXXXXXX..." at font size 20


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


def get_jersey_filename(team_name: str, is_goalkeeper: bool = False) -> str:
    """Convert FPL API team name to jersey filename."""
    # Handle special cases where API name differs from file name
    name_mapping = {
        "Spurs": "Tottenham",
        "Nott'm Forest": "Nott-Forest",
    }
    mapped_name = name_mapping.get(team_name, team_name)
    # Replace spaces with hyphens
    base_name = mapped_name.replace(" ", "-")

    # Support goalkeeper jerseys (fall back to outfield if not found)
    if is_goalkeeper:
        gk_path = f"{base_name}-GK.png"
        if os.path.exists(os.path.join(JERSEYS_DIR, gk_path)):
            return gk_path

    return f"{base_name}.png"


def calculate_player_coordinates(picks, all_players, width, height):
    """Calculate x,y coordinates for each player based on their position."""
    starters = [p for p in picks if p['position'] <= 11]
    bench = [p for p in picks if p['position'] > 11]
    bench.sort(key=lambda x: x['position'])  # Ensure bench is ordered

    positions = {1: [], 2: [], 3: [], 4: []}
    for p in starters:
        player_type = all_players[p['element']]['element_type']
        positions[player_type].append(p)

    coords = {}

    # Vertical Layout Y-coordinates (approximate ratios for mobile pitch)
    y_ratios = {1: 0.13, 2: 0.31, 3: 0.50, 4: 0.69}

    # Calculate coordinates for starters
    for pos_type, y_ratio in y_ratios.items():
        players = positions[pos_type]
        if not players:
            continue
        y = int(height * y_ratio)
        for i, p in enumerate(players):
            x = int(width * (i + 0.5) / len(players))
            coords[p['element']] = (x, y)

    # Calculate coordinates for bench (Horizontal row at bottom)
    bench_y = int(height * 0.89)
    for i, p in enumerate(bench):
        x = int(width * (i + 0.5) / len(bench))
        coords[p['element']] = (x, bench_y)
    return coords


def generate_team_image(fpl_data, summary_data, is_finished=False):
    """Generate a team image showing all players with their points."""
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
    width, height = background.size
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players, width, height)

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
        # Get team jersey
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        jersey_filename = get_jersey_filename(team_name, is_goalkeeper)
        jersey_path = os.path.join(JERSEYS_DIR, jersey_filename)

        try:
            asset_img = Image.open(jersey_path).convert("RGBA")
            # Resize to target height, maintain aspect ratio (GK jerseys are wider)
            target_height = JERSEY_SIZE[1]
            scale = target_height / asset_img.height
            new_width = int(asset_img.width * scale)
            asset_img = asset_img.resize((new_width, target_height), Image.LANCZOS)
        except FileNotFoundError:
            print(f"Jersey not found: {jersey_path}")
            continue
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2 + 10

        # Add visual indicators for subs
        if was_subbed_out:
            red_overlay = Image.new("RGBA", asset_img.size, (255, 0, 0, 80))
            asset_img = Image.alpha_composite(asset_img, red_overlay)
        if was_subbed_in:
            green_overlay = Image.new("RGBA", asset_img.size, (0, 255, 0, 80))
            asset_img = Image.alpha_composite(asset_img, green_overlay)

        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 10:
            name_text = name_text[:8] + "..."
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = PLAYER_BOX_WIDTH
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING
        name_box_x = x - box_width // 2
        name_box_y = y + 66  # Adjusted for 131px tall jerseys
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill="#015030")
        draw.text((x - name_bbox[2] / 2, name_box_y - 2), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            active_chip = fpl_data['picks'].get('active_chip')
            captain_text = "TC" if active_chip == '3xc' else "C"
            draw.text((paste_x + 80, paste_y - 5), captain_text, font=captain_font, fill="black", stroke_width=2, stroke_fill="yellow")
        elif player_pick['is_vice_captain']:
            draw.text((paste_x + 80, paste_y - 5), "V", font=captain_font, fill="black", stroke_width=2, stroke_fill="white")

    # Draw Header Info (Team Name, GW Points, Total Points)
    header_y = 20
    left_margin = 20

    # Team Name
    team_name_text = summary_data.get('team_name', 'My Team')
    team_font = ImageFont.truetype(FONT_PATH, 28)
    draw.text((left_margin - 10, header_y - 6), team_name_text, font=team_font, fill="white")

    # Calculate offset for points info (beside team name)
    team_bbox = draw.textbbox((0, 0), team_name_text, font=team_font)
    team_width = team_bbox[2] - team_bbox[0]
    points_x = left_margin + team_width + 30

    # GW Points
    gw_text = f"GW{fpl_data['live'].get('gw', '')} PTS: {summary_data['gw_points']}"
    draw.text((points_x, header_y - 14), gw_text, font=summary_font, fill="white")

    # Total Points (below GW points)
    total_text = f"Total PTS: {summary_data['total_points']}"
    draw.text((points_x, header_y + 12), total_text, font=summary_font, fill="white")

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
    width, height = background.size
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players, width, height)

    # Draw players (same as original team image but without captain/vice captain)
    for player_pick in fpl_data['picks']['picks']:
        player_id = player_pick['element']
        player_info = all_players[player_id]
        player_name = player_info['web_name']
        base_points = live_points.get(player_id, 0)
        multiplier = player_pick.get('multiplier', 1)
        is_bench_player = player_pick['position'] > 11 and multiplier == 0
        display_points = base_points if is_bench_player else base_points * multiplier

        # Get team jersey
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        jersey_filename = get_jersey_filename(team_name, is_goalkeeper)
        jersey_path = os.path.join(JERSEYS_DIR, jersey_filename)

        try:
            asset_img = Image.open(jersey_path).convert("RGBA")
            # Resize to target height, maintain aspect ratio (GK jerseys are wider)
            target_height = JERSEY_SIZE[1]
            scale = target_height / asset_img.height
            new_width = int(asset_img.width * scale)
            asset_img = asset_img.resize((new_width, target_height), Image.LANCZOS)
        except FileNotFoundError:
            print(f"Jersey not found: {jersey_path}")
            continue
        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 10:
            name_text = name_text[:8] + "..."
        points_text = f"{display_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        box_width = PLAYER_BOX_WIDTH
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING
        name_box_x = x - box_width // 2
        name_box_y = y + 66  # Adjusted for 131px tall jerseys
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=5, fill=(0, 0, 0, 100))
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=5, fill=(0, 135, 81, 150))
        draw.text((x - name_bbox[2] / 2, name_box_y - 4), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

    # Draw Header Info for Dream Team
    header_y = 20
    left_margin = 20

    team_font = ImageFont.truetype(FONT_PATH, 32)
    draw.text((left_margin, header_y), "Dream Team", font=team_font, fill="black")

    dream_bbox = draw.textbbox((0, 0), "Dream Team", font=team_font)
    dream_width = dream_bbox[2] - dream_bbox[0]
    points_x = left_margin + dream_width + 30

    gw_text = f"Gameweek {summary_data['gameweek']}"
    draw.text((points_x, header_y + 4), gw_text, font=summary_font, fill="black")

    total_text = f"Total PTS: {summary_data['total_points']}"
    draw.text((points_x, header_y + 30), total_text, font=summary_font, fill="black")

    # Draw Player of the Week section
    potw_data = summary_data['player_of_week']
    potw_player_info = potw_data['player_info']
    potw_name = potw_player_info['web_name']
    potw_points = potw_data['points']

    # Player of the Week positioning (Top Left)
    potw_x = 20
    potw_y = 20

    # Load jersey for POTW
    team_id = potw_player_info['team']
    team_name = all_teams[team_id]['name']
    is_goalkeeper = potw_player_info['element_type'] == 1
    jersey_filename = get_jersey_filename(team_name, is_goalkeeper)
    jersey_path = os.path.join(JERSEYS_DIR, jersey_filename)

    potw_img = None
    try:
        potw_img = Image.open(jersey_path).convert("RGBA")
        # Scale down for POTW box (maintain aspect ratio: 104x146 -> 52x73)
        potw_img = potw_img.resize((52, 73), Image.LANCZOS)
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
