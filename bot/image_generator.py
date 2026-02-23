"""Image generation functions for the FPL Discord bot."""

import os
import io
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from bot.logging_config import get_logger

logger = get_logger('image')

# --- File Paths ---
BACKGROUND_IMAGE_PATH = "mobile-pitch-graphic.png"
DREAMTEAM_BACKGROUND_PATH = "dream-pitch-graphic.png"
FONT_PATH = "Geist-Medium.otf"
JERSEYS_DIR = "team_jerseys"
JERSEY_SIZE = (85, 113)  # ~18% smaller than original 104x146, maintains aspect ratio

# --- Layout & Styling ---
NAME_FONT_SIZE = 18
POINTS_FONT_SIZE = 18
CAPTAIN_FONT_SIZE = 22
SUMMARY_FONT_SIZE = 24
POINTS_BOX_EXTRA_PADDING = 4
PLAYER_BOX_WIDTH = 110  # Fixed width to fit "XXXXXXXX..." at font size 20

# --- Shared Layout Settings ---
NAME_BOX_Y_OFFSET = 66      # Offset from player Y to name box
JERSEY_Y_OFFSET = 16        # Offset for jersey paste position
NAME_TEXT_Y_OFFSET = -1     # Name text Y offset within box
POINTS_BOX_COLOR = "#2E0F68"  # Purple
NAME_BOX_COLOR = (0, 0, 0, 200)  # Black, mostly opaque

# --- Glassmorphism Settings ---
GLASS_PADDING_TOP = 8       # Padding above jersey inside glass card
GLASS_BLUR_RADIUS = 8       # Gaussian blur radius (matches website backdrop-filter: blur(8px))
GLASS_TINT = (255, 255, 255, 65)  # White tint (~25% opacity, matches website light glass look)
GLASS_CORNER_RADIUS = 10    # Rounded top corners


def format_player_price(player):
    """Return player's price as £X.Xm string."""
    return f"£{player.get('now_cost', 0) / 10:.1f}m"


def build_manager_url(entry_id, gameweek=None, base_url=None):
    """Return the URL for a manager's team on our site, or FPL as fallback."""
    if base_url:
        return f"{base_url}/manager/{entry_id}/stats"
    if gameweek:
        return f"https://fantasy.premierleague.com/entry/{entry_id}/event/{gameweek}"
    return f"https://fantasy.premierleague.com/entry/{entry_id}/history/"


def format_manager_link(label, entry_id, gameweek=None, base_url=None):
    """Wrap a label in Markdown linking to the manager's team."""
    url = build_manager_url(entry_id, gameweek, base_url)
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


def load_jersey_image(team_name: str, is_goalkeeper: bool = False, target_height: int = None):
    """
    Load and resize a jersey image for a team.

    Args:
        team_name: The FPL API team name
        is_goalkeeper: Whether to try loading GK jersey first
        target_height: Target height to resize to (maintains aspect ratio)

    Returns:
        PIL Image object or None if not found
    """
    jersey_filename = get_jersey_filename(team_name, is_goalkeeper)
    jersey_path = os.path.join(JERSEYS_DIR, jersey_filename)

    try:
        jersey = Image.open(jersey_path).convert("RGBA")
        if target_height:
            scale = target_height / jersey.height
            new_width = int(jersey.width * scale)
            jersey = jersey.resize((new_width, target_height), Image.LANCZOS)
        return jersey
    except FileNotFoundError:
        logger.warning(f"Jersey not found: {jersey_path}")
        return None


def draw_captain_indicator(background, draw, font, text: str, paste_x: int, paste_y: int,
                          circle_size: int = 28, x_offset: int = 63, y_offset: int = -5):
    """
    Draw a captain/vice-captain indicator circle with text.

    Args:
        background: PIL Image to paste the circle onto
        draw: ImageDraw object for text drawing
        font: Font to use for the indicator text
        text: Text to display ("C", "TC", or "V")
        paste_x: X position of the jersey
        paste_y: Y position of the jersey
        circle_size: Size of the indicator circle (default 28)
        x_offset: X offset from paste_x (default 75)
        y_offset: Y offset from paste_y (default -5)
    """
    # Create antialiased circle using supersampling
    scale = 4
    circle_img = Image.new("RGBA", (circle_size * scale, circle_size * scale), (0, 0, 0, 0))
    circle_draw = ImageDraw.Draw(circle_img)
    circle_draw.ellipse([0, 0, circle_size * scale - 1, circle_size * scale - 1], fill="black")
    circle_img = circle_img.resize((circle_size, circle_size), Image.LANCZOS)

    # Position and paste circle
    circle_x = paste_x + x_offset
    circle_y = paste_y + y_offset
    background.paste(circle_img, (circle_x, circle_y), circle_img)

    # Use smaller font for "TC" so it fits in the circle
    text_font = font
    if text == "TC":
        text_font = ImageFont.truetype(FONT_PATH, CAPTAIN_FONT_SIZE - 6)

    # Draw centered text (with stroke for bold effect)
    text_x = circle_x + circle_size // 2
    text_y = circle_y + circle_size // 2
    # Adjust Y position slightly for "V" to center better
    if text == "V":
        text_y += 1
    draw.text((text_x, text_y), text, font=text_font, fill="white", anchor="mm", stroke_width=1, stroke_fill="white")


def draw_glass_card(background, card_x, card_y, card_w, card_h,
                    blur_radius=GLASS_BLUR_RADIUS, tint=GLASS_TINT,
                    corner_radius=GLASS_CORNER_RADIUS):
    """Draw a glassmorphism card by blurring the background region and applying a dark tint.

    Only top corners are rounded (matching website PlayerPill style).
    """
    # Clamp to image bounds
    x1 = max(0, card_x)
    y1 = max(0, card_y)
    x2 = min(background.width, card_x + card_w)
    y2 = min(background.height, card_y + card_h)

    if x2 <= x1 or y2 <= y1:
        return

    w, h = x2 - x1, y2 - y1

    # Crop the region from the background and blur it
    region = background.crop((x1, y1, x2, y2)).convert("RGBA")
    blurred = region.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Apply dark tint overlay
    tint_overlay = Image.new("RGBA", (w, h), tint)
    blurred = Image.alpha_composite(blurred, tint_overlay)

    # Create mask with rounded top corners only
    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=corner_radius, fill=255)
    # Square off bottom corners
    if corner_radius > 0:
        mask_draw.rectangle([0, h - corner_radius, corner_radius, h], fill=255)
        mask_draw.rectangle([w - corner_radius, h - corner_radius, w, h], fill=255)

    # Paste blurred+tinted region back using the mask
    background.paste(blurred, (x1, y1), mask)


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
    y_ratios = {1: 0.07, 2: 0.26, 3: 0.47, 4: 0.67}

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
    bench_y = int(height * 0.88)
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
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        logger.error(f"Error loading image resources: {e}")
        return None

    all_players = {p['id']: p for p in fpl_data['bootstrap']['elements']}
    all_teams = {t['id']: t for t in fpl_data['bootstrap']['teams']}
    live_points = {p['id']: p['stats'] for p in fpl_data['live']['elements']}
    width, height = background.size
    coordinates = calculate_player_coordinates(fpl_data['picks']['picks'], all_players, width, height)

    # Build fixture lookup per team for unstarted games
    live_fixtures = fpl_data['live'].get('fixtures', [])
    team_fixture_map = {}  # team_id -> fixture
    for fix in live_fixtures:
        team_fixture_map[fix['team_h']] = fix
        team_fixture_map[fix['team_a']] = fix

    def get_fixture_text(team_id):
        """Return fixture text like 'ARS (H)' if the player's game hasn't started, else None."""
        fix = team_fixture_map.get(team_id)
        if not fix or fix.get('started', False):
            return None
        is_home = fix['team_h'] == team_id
        opp_id = fix['team_a'] if is_home else fix['team_h']
        opp_team = all_teams.get(opp_id, {})
        opp_name = opp_team.get('short_name', '???')
        return f"{opp_name} ({'H' if is_home else 'A'})"

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

        # Check if this player's game hasn't started yet
        fixture_text = get_fixture_text(player_info['team'])

        # Determine points to display
        display_points = base_points
        scoring_pick_details = next((p for p in scoring_picks_data if p['element'] == player_id), None)

        if scoring_pick_details:
            final_multiplier = scoring_pick_details.get('final_multiplier', 1)
            display_points = base_points * final_multiplier

        if fixture_text:
            points_text = fixture_text
        else:
            points_text = f"{display_points} pts"

        # For subbed out starters, show their base points but they won't be summed
        if was_subbed_out:
            points_text = f"({base_points}) pts"

        # --- Drawing logic ---
        # Get team jersey using utility function
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        asset_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])
        if asset_img is None:
            continue

        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2 + JERSEY_Y_OFFSET

        # Pre-calculate box positions for glass card
        box_width = PLAYER_BOX_WIDTH
        name_box_x = x - box_width // 2
        name_box_y = y + NAME_BOX_Y_OFFSET
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING

        # Draw glass card behind jersey + name area
        glass_x = name_box_x
        glass_y = paste_y - GLASS_PADDING_TOP
        glass_h = (name_box_y + name_box_height) - glass_y
        draw_glass_card(background, glass_x, glass_y, box_width, glass_h)

        # Add visual indicators for subs
        if was_subbed_out:
            red_overlay = Image.new("RGBA", asset_img.size, (255, 0, 0, 80))
            asset_img = Image.alpha_composite(asset_img, red_overlay)
        if was_subbed_in:
            green_overlay = Image.new("RGBA", asset_img.size, (0, 255, 0, 80))
            asset_img = Image.alpha_composite(asset_img, green_overlay)

        # Paste jersey on top of glass card
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 9:
            name_text = name_text[:8] + "..."
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height

        # Name box (black, rounded top corners, flat bottom)
        r = 5
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=r, fill=NAME_BOX_COLOR)
        draw.rectangle([name_box_x, name_box_y + name_box_height - r, name_box_x + box_width, name_box_y + name_box_height], fill=NAME_BOX_COLOR)
        # Points box (purple for points, grey for upcoming fixture)
        pts_color = "#374151" if fixture_text else POINTS_BOX_COLOR
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=r, fill=pts_color)
        draw.rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + r], fill=pts_color)
        draw.text((x - name_bbox[2] / 2, name_box_y + NAME_TEXT_Y_OFFSET), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

        if player_pick['is_captain']:
            active_chip = fpl_data['picks'].get('active_chip')
            captain_text = "TC" if active_chip == '3xc' else "C"
            draw_captain_indicator(background, draw, captain_font, captain_text, paste_x, paste_y)
        elif player_pick['is_vice_captain']:
            draw_captain_indicator(background, draw, captain_font, "V", paste_x, paste_y)

    # Wrap pitch in editorial header + footer (scaled 1.25x for 600px width)
    hdr_height = 48
    ftr_height = 45
    pitch_w, pitch_h = background.size
    canvas = Image.new("RGBA", (pitch_w, hdr_height + pitch_h + ftr_height), TABLE_BG)
    canvas_draw = ImageDraw.Draw(canvas)

    # Header bar
    hdr_font = ImageFont.truetype(FONT_BOLD, 22)
    hdr_detail_font = ImageFont.truetype(FONT_PATH, 20)
    chip_font = ImageFont.truetype(FONT_BOLD, 12)
    canvas_draw.rectangle([0, 0, pitch_w, hdr_height], fill=TABLE_HEADER_BG)
    team_name_text = summary_data.get('team_name', 'My Team')
    canvas_draw.text((10, (hdr_height - 22) // 2), team_name_text, font=hdr_font, fill=TABLE_TEXT_BLACK)

    # Chip badge between team name and points
    active_chip = fpl_data['picks'].get('active_chip')
    chip_right_edge = pitch_w - 10  # where points text will anchor from
    detail_text = f"{summary_data['gw_points']} pts  \u2022  Total: {summary_data['total_points']}"
    canvas_draw.text((chip_right_edge, (hdr_height - 20) // 2), detail_text, font=hdr_detail_font, fill="#525252", anchor="ra")

    if active_chip and active_chip in CHIP_COLORS:
        chip_bg, chip_label = CHIP_COLORS[active_chip]
        # Circular badge right after team name
        name_bbox = canvas_draw.textbbox((10, 0), team_name_text, font=hdr_font)
        name_right = name_bbox[2]
        chip_diameter = 30
        chip_x = name_right + 8
        chip_y = (hdr_height - chip_diameter) // 2
        # Anti-aliased circle via supersampling
        scale = 4
        circle_img = Image.new("RGBA", (chip_diameter * scale, chip_diameter * scale), (0, 0, 0, 0))
        circle_draw = ImageDraw.Draw(circle_img)
        circle_draw.ellipse([0, 0, chip_diameter * scale - 1, chip_diameter * scale - 1], fill=chip_bg)
        circle_img = circle_img.resize((chip_diameter, chip_diameter), Image.LANCZOS)
        canvas.paste(circle_img, (chip_x, chip_y), circle_img)
        chip_label_font = ImageFont.truetype(FONT_BOLD, 13)
        canvas_draw.text((chip_x + chip_diameter // 2, chip_y + chip_diameter // 2), chip_label,
                         font=chip_label_font, fill="white", anchor="mm")
    canvas_draw.line([(0, hdr_height), (pitch_w, hdr_height)], fill=TABLE_BORDER, width=1)

    # Paste pitch
    canvas.paste(background, (0, hdr_height))

    # Footer
    footer_y = hdr_height + pitch_h
    ftr_font = ImageFont.truetype(FONT_PATH, 16)
    gw_num = fpl_data['live'].get('gw', '')
    canvas_draw.line([(0, footer_y), (pitch_w, footer_y)], fill=TABLE_BORDER, width=1)
    footer_text = f"GW{gw_num} \u2022 livefplstats.com"
    canvas_draw.text((pitch_w // 2, footer_y + ftr_height // 2), footer_text,
                     font=ftr_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    # Gradient bar
    total_h = hdr_height + pitch_h + ftr_height
    gradient_height = 4
    gradient_y = total_h - gradient_height
    for x in range(pitch_w):
        t = x / max(pitch_w - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            canvas.putpixel((x, gradient_y + dy), (r, g, b, 255))

    img_byte_arr = io.BytesIO()
    canvas.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr


def generate_dreamteam_image(fpl_data, summary_data):
    """Generate dream team image with Player of the Week graphic."""
    try:
        pitch = Image.open(DREAMTEAM_BACKGROUND_PATH).convert("RGBA")
        base_layer = Image.new("RGBA", pitch.size, "#1A1A1E")
        background = Image.alpha_composite(base_layer, pitch)
        draw = ImageDraw.Draw(background)
        name_font = ImageFont.truetype(FONT_PATH, NAME_FONT_SIZE)
        points_font = ImageFont.truetype(FONT_PATH, POINTS_FONT_SIZE)
        potw_font = ImageFont.truetype(FONT_PATH, 20)  # Player of the Week font
        name_sample_bbox = draw.textbbox((0, 0), "Agjpqy", font=name_font)
        points_sample_bbox = draw.textbbox((0, 0), "Agjpqy 0123456789", font=points_font)
        fixed_name_box_height = (name_sample_bbox[3] - name_sample_bbox[1]) + 4
        fixed_points_box_height = (points_sample_bbox[3] - points_sample_bbox[1]) + 4
    except Exception as e:
        logger.error(f"Error loading image resources: {e}")
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

        # Get team jersey using utility function
        team_id = player_info['team']
        team_name = all_teams[team_id]['name']
        is_goalkeeper = player_info['element_type'] == 1
        asset_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])
        if asset_img is None:
            continue

        x, y = coordinates[player_id]
        paste_x, paste_y = x - asset_img.width // 2, y - asset_img.height // 2 + JERSEY_Y_OFFSET

        # Pre-calculate box positions for glass card
        box_width = PLAYER_BOX_WIDTH
        name_box_x = x - box_width // 2
        name_box_y = y + NAME_BOX_Y_OFFSET
        name_box_height = fixed_name_box_height + POINTS_BOX_EXTRA_PADDING

        # Draw glass card behind jersey + name area
        glass_x = name_box_x
        glass_y = paste_y - GLASS_PADDING_TOP
        glass_h = (name_box_y + name_box_height) - glass_y
        draw_glass_card(background, glass_x, glass_y, box_width, glass_h)

        # Paste jersey on top of glass card
        background.paste(asset_img, (paste_x, paste_y), asset_img)

        name_text = player_name
        if len(name_text) > 9:
            name_text = name_text[:8] + "..."
        points_text = f"{display_points} pts"
        name_bbox = draw.textbbox((0, 0), name_text, font=name_font)
        points_bbox = draw.textbbox((0, 0), points_text, font=points_font)
        points_box_height = fixed_points_box_height + POINTS_BOX_EXTRA_PADDING
        points_box_x = name_box_x
        points_box_y = name_box_y + name_box_height

        # Name box (black, rounded top corners, flat bottom)
        r = 5
        draw.rounded_rectangle([name_box_x, name_box_y, name_box_x + box_width, name_box_y + name_box_height], radius=r, fill=NAME_BOX_COLOR)
        draw.rectangle([name_box_x, name_box_y + name_box_height - r, name_box_x + box_width, name_box_y + name_box_height], fill=NAME_BOX_COLOR)
        # Points box (purple, flat top, rounded bottom corners)
        draw.rounded_rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + points_box_height], radius=r, fill=POINTS_BOX_COLOR)
        draw.rectangle([points_box_x, points_box_y, points_box_x + box_width, points_box_y + r], fill=POINTS_BOX_COLOR)
        draw.text((x - name_bbox[2] / 2, name_box_y + NAME_TEXT_Y_OFFSET), name_text, font=name_font, fill="white")
        draw.text((x - points_bbox[2] / 2, points_box_y), points_text, font=points_font, fill="white")

    # Draw Player of the Week section (centered at bottom of pitch)
    potw_data = summary_data['player_of_week']
    potw_player_info = potw_data['player_info']
    potw_name = potw_player_info['web_name']
    potw_points = potw_data['points']

    # POTW fonts
    potw_title_font = ImageFont.truetype(FONT_PATH, 36)
    potw_details_font = ImageFont.truetype(FONT_PATH, 28)

    # Load jersey for POTW using utility function
    team_id = potw_player_info['team']
    team_name = all_teams[team_id]['name']
    is_goalkeeper = potw_player_info['element_type'] == 1
    potw_img = load_jersey_image(team_name, is_goalkeeper, target_height=JERSEY_SIZE[1])

    # Draw "Player of the Week" title - centered horizontally
    title_text = "Player of the Week"
    title_bbox = draw.textbbox((0, 0), title_text, font=potw_title_font)
    title_width = title_bbox[2]
    title_x = (width - title_width) // 2
    title_y = int(height * 0.85) - 15
    draw.text((title_x, title_y), title_text, font=potw_title_font, fill="black")

    # Calculate positioning for details section (name, pts, stats + jersey)
    details_y = title_y + 70
    jersey_width = potw_img.width if potw_img else 0

    name_bbox = draw.textbbox((0, 0), potw_name, font=potw_details_font)
    pts_bbox = draw.textbbox((0, 0), f"{potw_points} pts", font=potw_details_font)
    stats_bbox = draw.textbbox((0, 0), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_details_font)
    max_text_width = max(name_bbox[2], pts_bbox[2], stats_bbox[2])

    total_width = max_text_width + 20 + jersey_width
    start_x = (width - total_width) // 2

    draw.text((start_x, details_y), potw_name, font=potw_details_font, fill="black")
    draw.text((start_x, details_y + 28), f"{potw_points} pts", font=potw_details_font, fill="black")
    draw.text((start_x, details_y + 56), f"G: {potw_data['goals']} A: {potw_data['assists']}", font=potw_details_font, fill="black")

    if potw_img:
        jersey_x = start_x + max_text_width + 20
        jersey_y = details_y - 10
        background.paste(potw_img, (jersey_x, jersey_y), potw_img)

    # Wrap pitch in editorial header + footer (scaled 1.25x for 600px width)
    hdr_height = 48
    ftr_height = 45
    pitch_w, pitch_h = background.size
    canvas = Image.new("RGBA", (pitch_w, hdr_height + pitch_h + ftr_height), TABLE_BG)
    canvas_draw = ImageDraw.Draw(canvas)

    # Header bar
    hdr_font = ImageFont.truetype(FONT_BOLD, 22)
    hdr_detail_font = ImageFont.truetype(FONT_PATH, 20)
    canvas_draw.rectangle([0, 0, pitch_w, hdr_height], fill=TABLE_HEADER_BG)
    league_name = summary_data.get('league_name', 'Dream Team')
    if len(league_name) >= 20:
        league_name = league_name[:18] + "..."
    canvas_draw.text((10, (hdr_height - 22) // 2), league_name, font=hdr_font, fill=TABLE_TEXT_BLACK)
    gw = summary_data.get('gameweek', '')
    detail_text = f"Dream Team GW{gw}  \u2022  {summary_data['total_points']} pts"
    canvas_draw.text((pitch_w - 10, (hdr_height - 20) // 2), detail_text, font=hdr_detail_font, fill="#525252", anchor="ra")
    canvas_draw.line([(0, hdr_height), (pitch_w, hdr_height)], fill=TABLE_BORDER, width=1)

    # Paste pitch
    canvas.paste(background, (0, hdr_height))

    # Footer
    footer_y = hdr_height + pitch_h
    ftr_font = ImageFont.truetype(FONT_PATH, 16)
    canvas_draw.line([(0, footer_y), (pitch_w, footer_y)], fill=TABLE_BORDER, width=1)
    footer_text = f"GW{gw} \u2022 livefplstats.com"
    canvas_draw.text((pitch_w // 2, footer_y + ftr_height // 2), footer_text,
                     font=ftr_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    # Gradient bar
    total_h = hdr_height + pitch_h + ftr_height
    gradient_height = 4
    gradient_y = total_h - gradient_height
    for x in range(pitch_w):
        t = x / max(pitch_w - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            canvas.putpixel((x, gradient_y + dy), (r, g, b, 255))

    img_byte_arr = io.BytesIO()
    canvas.convert("RGB").save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr


# =====================================================
# LEAGUE TABLE IMAGE
# =====================================================

# Colors matching the website's editorial theme
TABLE_BG = "#FFFFFF"
TABLE_HEADER_BG = "#F5F5F4"       # editorial-bg
TABLE_BORDER = "#E5E5E4"          # border-light
TABLE_TEXT_BLACK = "#171717"       # ink-black
TABLE_TEXT_MUTED = "#737373"       # ink-muted
TABLE_GW_BLUE = "#2563EB"         # blue-600
TABLE_RANK_UP = "#059669"         # green
TABLE_RANK_DOWN = "#DC2626"       # red
TABLE_CARD_RADIUS = 16

# Chip badge colors (matching ChipBadge.tsx)
CHIP_COLORS = {
    'wildcard': ('#8B5CF6', 'WC'),
    'freehit':  ('#3B82F6', 'FH'),
    'bboost':   ('#10B981', 'BB'),
    '3xc':      ('#F59E0B', 'TC'),
}

# Font paths
FONT_REGULAR = "Geist-Regular.otf"
FONT_SEMIBOLD = "Geist-SemiBold.otf"
FONT_BOLD = "Geist-Bold.otf"

# FDR colors — exact match to website CSS (theme-variables.css)
FDR_COLORS = {
    1: ("#2c6f00", "#FFFFFF"),  # dark green bg, white text
    2: ("#01fc7a", "#000000"),  # bright green bg, black text
    3: ("#cdcdcd", "#000000"),  # light grey bg, black text
    4: ("#ff3030", "#FFFFFF"),  # bright red bg, white text
    5: ("#c40000", "#FFFFFF"),  # dark red bg, white text
}
FDR_BGW = ("#6B7280", "#FFFFFF")  # gray-500 bg, white text
POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _draw_rounded_rect(draw, bbox, radius, fill=None, outline=None, width=1):
    """Draw a rounded rectangle with optional outline."""
    if fill:
        draw.rounded_rectangle(bbox, radius=radius, fill=fill)
    if outline:
        draw.rounded_rectangle(bbox, radius=radius, outline=outline, width=width)


def _draw_chip_badge(img, draw, cx, cy, chip_key, size=22):
    """Draw a colored chip badge circle centered at (cx, cy)."""
    config = CHIP_COLORS.get(chip_key)
    if not config:
        return
    color, label = config

    # Draw filled circle using supersampling for antialiasing
    scale = 4
    circle_img = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    circle_draw = ImageDraw.Draw(circle_img)
    circle_draw.ellipse([0, 0, size * scale - 1, size * scale - 1], fill=color)
    circle_img = circle_img.resize((size, size), Image.LANCZOS)

    x = cx - size // 2
    y = cy - size // 2
    img.paste(circle_img, (x, y), circle_img)

    # Draw label text
    chip_font = ImageFont.truetype(FONT_BOLD, 11)
    draw.text((cx, cy), label, font=chip_font, fill="white", anchor="mm")


def _draw_rank_arrow(img, draw, cx, cy, direction, size=16):
    """Draw a small colored circle with an up/down chevron arrow, antialiased."""
    color = TABLE_RANK_UP if direction == 'up' else TABLE_RANK_DOWN

    # Draw everything at 4x scale then downscale for clean antialiasing
    scale = 4
    s = size * scale  # 64px working size
    arrow_img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    arrow_draw = ImageDraw.Draw(arrow_img)

    # Circle
    arrow_draw.ellipse([0, 0, s - 1, s - 1], fill=color)

    # Chevron matching Lucide's ChevronUp/ChevronDown (strokeWidth=3, size=12 in 16px circle)
    # Lucide chevron-up path in 24x24 viewbox: polyline points="18 15 12 9 6 15"
    # Scale to our working size: center at s/2, icon spans ~60% of circle
    mid = s // 2
    span_x = int(s * 0.28)   # horizontal reach from center
    span_y = int(s * 0.14)   # vertical reach from center
    stroke = int(s * 0.14)   # thick stroke for visibility

    # Offset chevron slightly within the circle for better visual centering
    nudge = int(s * 0.06)  # ~1px at final size
    if direction == 'up':
        points = [(mid - span_x, mid + span_y - nudge), (mid, mid - span_y - nudge), (mid + span_x, mid + span_y - nudge)]
    else:
        points = [(mid - span_x, mid - span_y + nudge), (mid, mid + span_y + nudge), (mid + span_x, mid - span_y + nudge)]

    arrow_draw.line(points, fill="white", width=stroke, joint="curve")
    # Round the line caps by drawing circles at the endpoints
    cap_r = stroke // 2
    for pt in [points[0], points[2]]:
        arrow_draw.ellipse([pt[0] - cap_r, pt[1] - cap_r, pt[0] + cap_r, pt[1] + cap_r], fill="white")

    # Downscale with LANCZOS for smooth antialiasing
    arrow_img = arrow_img.resize((size, size), Image.LANCZOS)

    x = cx - size // 2
    y = cy - size // 2
    img.paste(arrow_img, (x, y), arrow_img)


def generate_league_table_image(league_name, current_gw, managers, website_url=None):
    """Generate a league table image matching the website's MiniLeagueTable style.

    Args:
        league_name: Name of the league
        current_gw: Current gameweek number
        managers: List of dicts sorted by live_total_points desc, each with:
            - name: Player name
            - team_name: FPL team name
            - live_total_points: Total points including live GW
            - final_gw_points: GW points
            - picks_data: Picks dict with 'active_chip' key
            - prev_rank: Previous rank (1-indexed), or None
        website_url: Optional website URL for footer

    Returns:
        BytesIO PNG image or None
    """
    try:
        # Fonts
        header_font = ImageFont.truetype(FONT_BOLD, 18)
        col_header_font = ImageFont.truetype(FONT_BOLD, 11)
        name_font = ImageFont.truetype(FONT_SEMIBOLD, 14)
        points_font = ImageFont.truetype(FONT_BOLD, 14)
        gw_font = ImageFont.truetype(FONT_PATH, 14)
        rank_font = ImageFont.truetype(FONT_PATH, 13)
        footer_font = ImageFont.truetype(FONT_PATH, 13)
    except Exception as e:
        logger.error(f"Error loading fonts for league table: {e}")
        return None

    # Layout constants
    padding_x = 20
    row_height = 38
    header_height = 48
    col_header_height = 32
    footer_height = 36
    num_managers = len(managers)

    # Column positions (x offsets from left)
    col_rank_x = padding_x + 4
    col_name_x = padding_x + 48
    col_total_x = 340       # right-aligned
    col_chip_x = 395        # center-aligned
    col_gw_x = 450          # right-aligned
    img_width = 480
    img_height = header_height + col_header_height + (row_height * num_managers) + footer_height + 2

    # Create image
    img = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Card background (sharp corners)
    draw.rectangle([0, 0, img_width - 1, img_height - 1], fill=TABLE_BG, outline=TABLE_BORDER)

    # Header section
    draw.text((padding_x, (header_height - 18) // 2), league_name, font=header_font, fill=TABLE_TEXT_BLACK)

    # Header border
    header_bottom = header_height
    draw.line([(0, header_bottom), (img_width, header_bottom)], fill=TABLE_BORDER, width=1)

    # Column headers
    col_y = header_bottom + (col_header_height - 11) // 2
    draw.rectangle([0, header_bottom + 1, img_width, header_bottom + col_header_height], fill=TABLE_HEADER_BG)
    draw.text((col_rank_x, col_y), "#", font=col_header_font, fill=TABLE_TEXT_MUTED)
    draw.text((col_name_x, col_y), "MANAGER", font=col_header_font, fill=TABLE_TEXT_MUTED)
    draw.text((col_total_x, col_y), "TOTAL", font=col_header_font, fill=TABLE_TEXT_MUTED, anchor="ra")
    draw.text((col_chip_x, col_y), "CHIP", font=col_header_font, fill=TABLE_TEXT_MUTED, anchor="ma")
    draw.text((col_gw_x, col_y), "GW", font=col_header_font, fill=TABLE_GW_BLUE, anchor="ra")

    col_header_bottom = header_bottom + col_header_height
    draw.line([(0, col_header_bottom), (img_width, col_header_bottom)], fill=TABLE_BORDER, width=1)

    # Data rows
    for i, manager in enumerate(managers):
        row_y = col_header_bottom + (i * row_height)
        row_center_y = row_y + row_height // 2

        # Row divider (except first row)
        if i > 0:
            draw.line([(padding_x - 4, row_y), (img_width - padding_x + 4, row_y)], fill=TABLE_BORDER, width=1)

        # Rank
        rank = i + 1
        draw.text((col_rank_x, row_center_y), str(rank), font=rank_font, fill=TABLE_TEXT_MUTED, anchor="lm")

        # Rank movement arrow
        prev_rank = manager.get('prev_rank')
        if prev_rank and prev_rank != rank:
            arrow_x = col_rank_x + 28
            if rank < prev_rank:
                _draw_rank_arrow(img, draw, arrow_x, row_center_y, 'up')
            else:
                _draw_rank_arrow(img, draw, arrow_x, row_center_y, 'down')

        # Manager name (truncate if too long)
        mgr_name = manager['name']
        max_name_width = col_total_x - col_name_x - 40
        name_bbox = draw.textbbox((0, 0), mgr_name, font=name_font)
        if name_bbox[2] > max_name_width:
            while len(mgr_name) > 3:
                mgr_name = mgr_name[:-1]
                bbox = draw.textbbox((0, 0), mgr_name + "...", font=name_font)
                if bbox[2] <= max_name_width:
                    mgr_name += "..."
                    break
        draw.text((col_name_x, row_center_y), mgr_name, font=name_font, fill=TABLE_TEXT_BLACK, anchor="lm")

        # Total points
        total_str = str(manager['live_total_points'])
        draw.text((col_total_x, row_center_y), total_str, font=points_font, fill=TABLE_TEXT_BLACK, anchor="rm")

        # Chip badge
        active_chip = manager.get('picks_data', {}).get('active_chip')
        if active_chip:
            _draw_chip_badge(img, draw, col_chip_x, row_center_y, active_chip)
        else:
            draw.text((col_chip_x, row_center_y), "-", font=rank_font, fill=TABLE_TEXT_MUTED, anchor="mm")

        # GW points
        gw_str = str(manager['final_gw_points'])
        draw.text((col_gw_x, row_center_y), gw_str, font=gw_font, fill=TABLE_GW_BLUE, anchor="rm")

    # Footer
    footer_y = col_header_bottom + (num_managers * row_height)
    draw.line([(0, footer_y), (img_width, footer_y)], fill=TABLE_BORDER, width=1)
    footer_text = f"GW{current_gw} • livefplstats.com"
    draw.text((img_width // 2, footer_y + footer_height // 2), footer_text,
              font=footer_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    # Gradient line at bottom (warm-yellow #FEBF04 -> deep-red #C21600, matching website)
    gradient_height = 4
    gradient_y = img_height - gradient_height
    for x in range(img_width):
        t = x / max(img_width - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            img.putpixel((x, gradient_y + dy), (r, g, b, 255))

    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
    img_byte_arr.seek(0)
    return img_byte_arr


# =====================================================
# GW SUMMARY IMAGE (captains + transfers)
# =====================================================

def _draw_section_header(draw, y, text, color, img_width, padding_x=16):
    """Draw a section header bar with colored text."""
    font = ImageFont.truetype(FONT_BOLD, 12)
    draw.rectangle([0, y, img_width, y + 24], fill=TABLE_HEADER_BG)
    draw.line([(0, y), (img_width, y)], fill=TABLE_BORDER, width=1)
    draw.text((padding_x, y + 6), text, font=font, fill=color)
    draw.line([(0, y + 24), (img_width, y + 24)], fill=TABLE_BORDER, width=1)
    return y + 24


def _draw_count_badge(img, draw, x, y, count):
    """Draw a small blue count badge at (x, y)."""
    size = 16
    scale = 4
    badge = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    badge_draw = ImageDraw.Draw(badge)
    badge_draw.ellipse([0, 0, size * scale - 1, size * scale - 1], fill=TABLE_GW_BLUE)
    badge = badge.resize((size, size), Image.LANCZOS)
    img.paste(badge, (x - size // 2, y - size // 2), badge)
    badge_font = ImageFont.truetype(FONT_BOLD, 10)
    draw.text((x, y), str(count), font=badge_font, fill="white", anchor="mm")


def _draw_player_column(img, draw, cx, top_y, player_name, team_name, managers, fonts):
    """Draw a single player column: jersey + name + manager list. Returns height used."""
    jersey_h = 50
    name_font, mgr_font = fonts

    # Load and draw jersey
    jersey = load_jersey_image(team_name, target_height=jersey_h)
    if jersey:
        paste_x = cx - jersey.width // 2
        img.paste(jersey, (paste_x, top_y), jersey)
        # Count badge in top-right corner of jersey
        if len(managers) > 1:
            _draw_count_badge(img, draw, paste_x + jersey.width - 2, top_y + 2, len(managers))

    y = top_y + jersey_h + 2

    # Player name (truncated)
    display_name = player_name[:9] + "..." if len(player_name) > 9 else player_name
    draw.text((cx, y), display_name, font=name_font, fill=TABLE_TEXT_BLACK, anchor="ma")
    y += 17

    # Manager names
    for mgr in managers:
        draw.text((cx, y), mgr, font=mgr_font, fill=TABLE_TEXT_MUTED, anchor="ma")
        y += 13

    return y - top_y


def _draw_player_columns_section(img, draw, top_y, players_data, fonts, img_width=480, padding_x=16):
    """Draw a row of player columns. Returns height used."""
    if not players_data:
        empty_font = ImageFont.truetype(FONT_PATH, 12)
        draw.text((img_width // 2, top_y + 20), "None", font=empty_font, fill=TABLE_TEXT_MUTED, anchor="mm")
        return 40

    num_cols = min(len(players_data), 5)
    usable_width = img_width - 2 * padding_x
    col_width = usable_width // num_cols

    max_height = 0
    for i, player in enumerate(players_data[:5]):
        cx = padding_x + col_width // 2 + i * col_width
        h = _draw_player_column(
            img, draw, cx, top_y + 8,
            player['player_name'], player['team_name'], player['managers'], fonts
        )
        max_height = max(max_height, h)

    # If more than 5 players, draw second row
    if len(players_data) > 5:
        row2_y = top_y + 8 + max_height + 8
        num_cols2 = min(len(players_data) - 5, 5)
        col_width2 = usable_width // num_cols2
        max_height2 = 0
        for i, player in enumerate(players_data[5:10]):
            cx = padding_x + col_width2 // 2 + i * col_width2
            h = _draw_player_column(
                img, draw, cx, row2_y,
                player['player_name'], player['team_name'], player['managers'], fonts
            )
            max_height2 = max(max_height2, h)
        return 8 + max_height + 8 + max_height2 + 12
    else:
        return 8 + max_height + 12


def _format_short_name(name):
    """Format name as 'F. Surname' to save space."""
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {parts[-1]}"
    return name


def generate_gw_summary_image(gw_number, league_name, captains_data, transfers_in_data, transfers_out_data):
    """Generate a GW summary image showing captain choices and transfers.

    Args:
        gw_number: Gameweek number
        league_name: League name string
        captains_data: list of {player_name, team_name, managers: [str]} sorted by popularity
        transfers_in_data: same format, top transfers in
        transfers_out_data: same format, top transfers out

    Returns:
        BytesIO PNG image or None
    """
    try:
        header_font = ImageFont.truetype(FONT_BOLD, 16)
        league_font = ImageFont.truetype(FONT_BOLD, 15)
        name_font = ImageFont.truetype(FONT_SEMIBOLD, 13)
        mgr_font = ImageFont.truetype(FONT_REGULAR, 12)
        footer_font = ImageFont.truetype(FONT_PATH, 13)
    except Exception as e:
        logger.error(f"Error loading fonts for GW summary: {e}")
        return None

    img_width = 480
    padding_x = 16
    header_height = 48
    footer_height = 36
    fonts = (name_font, mgr_font)

    # Pre-calculate section heights by drawing to a scratch image
    scratch = Image.new("RGBA", (img_width, 2000), (0, 0, 0, 0))
    scratch_draw = ImageDraw.Draw(scratch)

    cap_height = _draw_player_columns_section(scratch, scratch_draw, 0, captains_data, fonts, img_width, padding_x)
    tin_height = _draw_player_columns_section(scratch, scratch_draw, 0, transfers_in_data, fonts, img_width, padding_x)
    tout_height = _draw_player_columns_section(scratch, scratch_draw, 0, transfers_out_data, fonts, img_width, padding_x)

    # Total image height
    img_height = header_height + 24 + cap_height + 24 + tin_height + 24 + tout_height + footer_height + 2

    # Create real image
    img = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background
    draw.rectangle([0, 0, img_width - 1, img_height - 1], fill=TABLE_BG, outline=TABLE_BORDER)

    # Header
    draw.rectangle([0, 0, img_width, header_height], fill=TABLE_HEADER_BG)
    draw.text((padding_x, (header_height - 16) // 2), f"Gameweek {gw_number} Summary", font=header_font, fill=TABLE_TEXT_BLACK)
    # League name right-aligned
    draw.text((img_width - padding_x, (header_height - 15) // 2), league_name, font=league_font, fill=TABLE_TEXT_MUTED, anchor="ra")
    draw.line([(0, header_height), (img_width, header_height)], fill=TABLE_BORDER, width=1)

    # Captain choices section
    y = header_height
    y = _draw_section_header(draw, y, "CAPTAIN CHOICES", TABLE_TEXT_MUTED, img_width, padding_x)
    _draw_player_columns_section(img, draw, y, captains_data, fonts, img_width, padding_x)
    y += cap_height

    # Transfers in section
    y = _draw_section_header(draw, y, "TRANSFERS IN", TABLE_RANK_UP, img_width, padding_x)
    _draw_player_columns_section(img, draw, y, transfers_in_data, fonts, img_width, padding_x)
    y += tin_height

    # Transfers out section
    y = _draw_section_header(draw, y, "TRANSFERS OUT", TABLE_RANK_DOWN, img_width, padding_x)
    _draw_player_columns_section(img, draw, y, transfers_out_data, fonts, img_width, padding_x)
    y += tout_height

    # Footer
    draw.line([(0, y), (img_width, y)], fill=TABLE_BORDER, width=1)
    footer_text = f"GW{gw_number} • livefplstats.com"
    draw.text((img_width // 2, y + footer_height // 2), footer_text, font=footer_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    # Gradient bar
    gradient_height = 4
    gradient_y = img_height - gradient_height
    for x in range(img_width):
        t = x / max(img_width - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            img.putpixel((x, gradient_y + dy), (r, g, b, 255))

    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
    img_byte_arr.seek(0)
    return img_byte_arr


# =====================================================
# GW RECAP IMAGE (shame + praise)
# =====================================================

SHAME_BG = "#FEF2F2"    # subtle red
PRAISE_BG = "#F0FDF4"   # subtle green


def _draw_metric_card(draw, x, y, width, category, manager_names, detail, value_str, value_color, fonts):
    """Draw a single metric card (rounded rect with category, managers, detail, value).

    Args:
        manager_names: list of manager name strings (will be shown two per row)
    """
    import math
    card_font_cat, card_font_name, card_font_detail, card_font_value = fonts

    if isinstance(manager_names, str):
        manager_names = [manager_names]

    name_rows = math.ceil(len(manager_names) / 2)
    row_line_h = 18
    base_height = 56
    extra = max(0, name_rows - 1) * row_line_h
    height = base_height + extra

    # Card background
    _draw_rounded_rect(draw, [x, y, x + width, y + height], 8, fill="#FFFFFF", outline=TABLE_BORDER)

    # Category label (top-left)
    draw.text((x + 16, y + 8), category, font=card_font_cat, fill=TABLE_TEXT_MUTED)

    # Manager names (two columns per row)
    col1_x = x + 16
    col2_x = x + 16 + 130  # fixed gap between the two name columns
    name_y = y + 28
    for i in range(0, len(manager_names), 2):
        draw.text((col1_x, name_y), manager_names[i], font=card_font_name, fill=TABLE_TEXT_BLACK)
        if i + 1 < len(manager_names):
            draw.text((col2_x, name_y), manager_names[i + 1], font=card_font_name, fill=TABLE_TEXT_BLACK)
        name_y += row_line_h

    # Detail text (center-right area, if present)
    if detail:
        draw.text((x + width - 80, y + 28), detail, font=card_font_detail, fill=TABLE_TEXT_MUTED, anchor="ra")

    # Value (far right)
    draw.text((x + width - 16, y + 28), value_str, font=card_font_value, fill=value_color, anchor="ra")

    return height


def generate_recap_image(gw_number, league_name, shame_data, praise_data):
    """Generate a GW recap image with shame and praise sections.

    Args:
        gw_number: Gameweek number
        league_name: League name string
        shame_data: dict with keys 'most_benched', 'worst_captain', 'transfer_flop'
            each value: {manager_name, value, player_name (optional)}
        praise_data: dict with keys 'highest_score', 'best_captain', 'best_transfer'
            each value: {manager_name, value, player_name (optional)}

    Returns:
        BytesIO PNG image or None
    """
    try:
        header_font = ImageFont.truetype(FONT_BOLD, 16)
        league_font = ImageFont.truetype(FONT_BOLD, 15)
        section_font = ImageFont.truetype(FONT_BOLD, 13)
        card_cat_font = ImageFont.truetype(FONT_SEMIBOLD, 11)
        card_name_font = ImageFont.truetype(FONT_SEMIBOLD, 14)
        card_detail_font = ImageFont.truetype(FONT_PATH, 13)
        card_value_font = ImageFont.truetype(FONT_BOLD, 14)
        footer_font = ImageFont.truetype(FONT_PATH, 13)
    except Exception as e:
        logger.error(f"Error loading fonts for recap: {e}")
        return None

    import math

    img_width = 480
    padding_x = 20
    header_height = 48
    section_header_h = 28
    base_card_height = 56
    row_line_h = 18
    card_gap = 8
    section_padding = 16
    footer_height = 36
    card_width = img_width - 2 * padding_x
    card_fonts = (card_cat_font, card_name_font, card_detail_font, card_value_font)

    def _prep_metric(category, data):
        """Prepare display data for a metric, return (manager_names_list, detail, value_str, card_h)."""
        if data:
            names = [d['manager_name'] for d in data]
            unique_players = list(dict.fromkeys(d.get('player_name', '') for d in data if d.get('player_name')))
            player_str = ", ".join(unique_players)
            detail = player_str
            value_str = f"{data[0]['value']} pts"
            name_rows = math.ceil(len(names) / 2)
            card_h = base_card_height + max(0, name_rows - 1) * row_line_h
        else:
            names = ["-"]
            detail = ""
            value_str = "No data"
            card_h = base_card_height
        return names, detail, value_str, card_h

    # Pre-compute all metrics to get dynamic heights
    shame_metrics_raw = [
        ('MOST POINTS BENCHED', shame_data.get('most_benched', [])),
        ('WORST CAPTAIN', shame_data.get('worst_captain', [])),
        ('BIGGEST TRANSFER FLOP', shame_data.get('transfer_flop', [])),
    ]
    praise_metrics_raw = [
        ('HIGHEST GW SCORE', praise_data.get('highest_score', [])),
        ('BEST CAPTAIN', praise_data.get('best_captain', [])),
        ('BEST TRANSFER IN', praise_data.get('best_transfer', [])),
    ]

    shame_prepared = [_prep_metric(cat, data) for cat, data in shame_metrics_raw]
    praise_prepared = [_prep_metric(cat, data) for cat, data in praise_metrics_raw]

    shame_cards_h = sum(p[3] for p in shame_prepared) + 2 * card_gap
    praise_cards_h = sum(p[3] for p in praise_prepared) + 2 * card_gap
    shame_section_h = section_header_h + section_padding // 2 + shame_cards_h + section_padding
    praise_section_h = section_header_h + section_padding // 2 + praise_cards_h + section_padding

    img_height = header_height + shame_section_h + praise_section_h + footer_height + 2

    img = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background
    draw.rectangle([0, 0, img_width - 1, img_height - 1], fill=TABLE_BG, outline=TABLE_BORDER)

    # Header
    draw.rectangle([0, 0, img_width, header_height], fill=TABLE_HEADER_BG)
    draw.text((padding_x, (header_height - 16) // 2), f"Gameweek {gw_number} Recap", font=header_font, fill=TABLE_TEXT_BLACK)
    draw.text((img_width - padding_x, (header_height - 15) // 2), league_name, font=league_font, fill=TABLE_TEXT_MUTED, anchor="ra")
    draw.line([(0, header_height), (img_width, header_height)], fill=TABLE_BORDER, width=1)

    y = header_height

    # --- SHAME SECTION ---
    draw.rectangle([0, y, img_width, y + shame_section_h], fill=SHAME_BG)
    draw.text((img_width // 2, y + section_header_h // 2), "WALL OF SHAME", font=section_font, fill=TABLE_RANK_DOWN, anchor="mm")
    y += section_header_h + section_padding // 2

    for (cat, _), (names, detail, value_str, card_h) in zip(shame_metrics_raw, shame_prepared):
        _draw_metric_card(draw, padding_x, y, card_width, cat, names, detail, value_str, TABLE_RANK_DOWN, card_fonts)
        y += card_h + card_gap

    y = header_height + shame_section_h

    # --- PRAISE SECTION ---
    draw.rectangle([0, y, img_width, y + praise_section_h], fill=PRAISE_BG)
    draw.text((img_width // 2, y + section_header_h // 2), "HALL OF FAME", font=section_font, fill=TABLE_RANK_UP, anchor="mm")
    y += section_header_h + section_padding // 2

    for (cat, _), (names, detail, value_str, card_h) in zip(praise_metrics_raw, praise_prepared):
        _draw_metric_card(draw, padding_x, y, card_width, cat, names, detail, value_str, TABLE_RANK_UP, card_fonts)
        y += card_h + card_gap

    # Footer
    footer_y = header_height + shame_section_h + praise_section_h
    draw.line([(0, footer_y), (img_width, footer_y)], fill=TABLE_BORDER, width=1)
    draw.text((img_width // 2, footer_y + footer_height // 2), f"GW{gw_number} • livefplstats.com",
              font=footer_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    # Gradient bar
    gradient_height = 4
    gradient_y = img_height - gradient_height
    for x in range(img_width):
        t = x / max(img_width - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            img.putpixel((x, gradient_y + dy), (r, g, b, 255))

    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
    img_byte_arr.seek(0)
    return img_byte_arr


# =====================================================
# Helper: draw standard footer + gradient bar
# =====================================================

def _draw_footer(draw, img, img_width, footer_y, footer_height, current_gw, font_size=13):
    """Draw the standard footer bar with gradient at the bottom."""
    draw.line([(0, footer_y), (img_width, footer_y)], fill=TABLE_BORDER, width=1)
    ftr_font = ImageFont.truetype(FONT_PATH, font_size)
    footer_text = f"GW{current_gw} • livefplstats.com"
    draw.text((img_width // 2, footer_y + footer_height // 2), footer_text,
              font=ftr_font, fill=TABLE_TEXT_MUTED, anchor="mm")

    gradient_height = 4
    gradient_y = footer_y + footer_height - gradient_height
    for x in range(img_width):
        t = x / max(img_width - 1, 1)
        r = int(0xFE + (0xC2 - 0xFE) * t)
        g = int(0xBF + (0x16 - 0xBF) * t)
        b = int(0x04 + (0x00 - 0x04) * t)
        for dy in range(gradient_height):
            img.putpixel((x, footer_y + footer_height - gradient_height + dy), (r, g, b, 255))


def _draw_fdr_legend(draw, y, img_width, center=True):
    """Draw the FDR color legend: Easy [1][2][3][4][5] Hard  BGW Blank"""
    label_font = ImageFont.truetype(FONT_PATH, 13)
    num_font = ImageFont.truetype(FONT_SEMIBOLD, 13)

    box_w, box_h = 32, 24
    gap = 5
    total_boxes_w = 5 * box_w + 4 * gap

    easy_text_w = draw.textlength("Easy", font=label_font)
    hard_text_w = draw.textlength("Hard", font=label_font)
    bgw_text_w = draw.textlength("Blank", font=label_font)
    bgw_box_w = 44
    total_w = easy_text_w + 8 + total_boxes_w + 8 + hard_text_w + 20 + bgw_box_w + 6 + bgw_text_w
    start_x = (img_width - total_w) // 2 if center else 16

    text_y = y + (box_h // 2)

    # "Easy" label
    draw.text((start_x, text_y), "Easy", font=label_font, fill=TABLE_TEXT_MUTED, anchor="lm")
    x = start_x + int(easy_text_w) + 8

    # 5 FDR boxes
    for fdr in range(1, 6):
        bg_color, text_color = FDR_COLORS[fdr]
        draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=3, fill=bg_color)
        draw.text((x + box_w // 2, y + box_h // 2), str(fdr),
                  font=num_font, fill=text_color, anchor="mm")
        x += box_w + gap

    # "Hard" label
    x -= gap
    x += 8
    draw.text((x, text_y), "Hard", font=label_font, fill=TABLE_TEXT_MUTED, anchor="lm")
    x += int(hard_text_w) + 20

    # BGW indicator (wider box to fit "BGW" text)
    bgw_box_w = 44
    bgw_bg, bgw_text = FDR_BGW
    draw.rounded_rectangle([x, y, x + bgw_box_w, y + box_h], radius=3, fill=bgw_bg)
    draw.text((x + bgw_box_w // 2, y + box_h // 2), "BGW",
              font=num_font, fill=bgw_text, anchor="mm")
    x += bgw_box_w + 6
    draw.text((x, text_y), "Blank", font=label_font, fill=TABLE_TEXT_MUTED, anchor="lm")


# =====================================================
# Player Ownership Image
# =====================================================

def generate_player_ownership_image(player_info, team_info, current_gw,
                                     gw_history, owners, benched):
    """Generate a 480px-wide player ownership image."""
    try:
        img_width = 480
        padding_x = 16
        header_height = 48
        section_header_h = 28
        row_h = 28
        footer_height = 36

        # --- Calculate dynamic height ---
        # Must exactly match y increments in drawing code below
        jersey_h = 80
        player_section_h = 16 + jersey_h + 6 + 20 + 4 + 16 + 4 + 16  # top_pad through total_pts
        gw_row_h = 2 + 14 + 32 + 12  # gap + labels + boxes + bottom_pad
        import math
        owned_rows = math.ceil(len(owners) / 2) if owners else 0
        benched_rows = math.ceil(len(benched) / 2) if benched else 0
        owned_section_h = (section_header_h + owned_rows * row_h) if owners else 0
        benched_section_h = (section_header_h + benched_rows * row_h) if benched else 0
        no_owners_h = 40 if (not owners and not benched) else 0

        total_height = (header_height + player_section_h + gw_row_h +
                        owned_section_h + benched_section_h + no_owners_h +
                        footer_height)

        img = Image.new("RGBA", (img_width, total_height), TABLE_BG)
        draw = ImageDraw.Draw(img)

        # --- Header ---
        draw.rectangle([0, 0, img_width, header_height], fill=TABLE_HEADER_BG)
        hdr_font = ImageFont.truetype(FONT_BOLD, 16)
        hdr_detail_font = ImageFont.truetype(FONT_PATH, 13)
        draw.text((padding_x, header_height // 2), "Player Ownership",
                  font=hdr_font, fill=TABLE_TEXT_BLACK, anchor="lm")
        draw.text((img_width - padding_x, header_height // 2), f"GW{current_gw}",
                  font=hdr_detail_font, fill=TABLE_TEXT_MUTED, anchor="rm")
        draw.line([(0, header_height), (img_width, header_height)], fill=TABLE_BORDER, width=1)

        y = header_height + 16

        # --- Jersey ---
        team_name = team_info.get('name', '')
        is_gk = player_info.get('element_type') == 1
        jersey = load_jersey_image(team_name, is_gk, target_height=jersey_h)
        if jersey:
            paste_x = (img_width - jersey.width) // 2
            img.paste(jersey, (paste_x, y), jersey)
        y += jersey_h + 6

        # --- Player name ---
        name_font = ImageFont.truetype(FONT_BOLD, 17)
        player_name = f"{player_info.get('first_name', '')} {player_info.get('second_name', '')}"
        draw.text((img_width // 2, y), player_name,
                  font=name_font, fill=TABLE_TEXT_BLACK, anchor="ma")
        y += 20 + 4

        # --- Club + Position ---
        detail_font = ImageFont.truetype(FONT_PATH, 13)
        position = POSITION_MAP.get(player_info.get('element_type', 0), "")
        club_pos_text = f"{team_name}  •  {position}"
        draw.text((img_width // 2, y), club_pos_text,
                  font=detail_font, fill=TABLE_TEXT_MUTED, anchor="ma")
        y += 16 + 4

        # --- Total points ---
        pts_font = ImageFont.truetype(FONT_SEMIBOLD, 14)
        total_pts = player_info.get('total_points', 0)
        draw.text((img_width // 2, y), f"Total Points: {total_pts}",
                  font=pts_font, fill=TABLE_TEXT_BLACK, anchor="ma")
        y += 16

        # --- Last 5 GW history boxes ---
        y += 2
        gw_label_font = ImageFont.truetype(FONT_REGULAR, 10)
        gw_pts_font = ImageFont.truetype(FONT_SEMIBOLD, 13)
        box_w, box_h = 52, 32
        box_gap = 6
        total_row_w = 5 * box_w + 4 * box_gap
        start_x = (img_width - total_row_w) // 2

        history_entries = list(gw_history[-5:]) if gw_history else []
        while len(history_entries) < 5:
            history_entries.insert(0, None)

        for i, entry in enumerate(history_entries):
            bx = start_x + i * (box_w + box_gap)
            if entry:
                gw_label = f"GW{entry.get('round', '?')}"
                pts_text = str(entry.get('total_points', 0))
            else:
                gw_label = "-"
                pts_text = "-"

            draw.text((bx + box_w // 2, y), gw_label,
                      font=gw_label_font, fill=TABLE_TEXT_MUTED, anchor="ma")
            by = y + 14
            draw.rounded_rectangle([bx, by, bx + box_w, by + box_h],
                                   radius=4, fill=TABLE_HEADER_BG)
            pts_color = TABLE_TEXT_BLACK if entry else TABLE_TEXT_MUTED
            draw.text((bx + box_w // 2, by + box_h // 2), pts_text,
                      font=gw_pts_font, fill=pts_color, anchor="mm")

        y += 14 + box_h + 12

        # --- Ownership sections ---
        def draw_section(label, names, color, y_pos):
            draw.rectangle([0, y_pos, img_width, y_pos + section_header_h],
                           fill=TABLE_HEADER_BG)
            draw.line([(0, y_pos), (img_width, y_pos)], fill=TABLE_BORDER, width=1)
            sec_font = ImageFont.truetype(FONT_BOLD, 11)
            draw.text((padding_x, y_pos + section_header_h // 2),
                      f"{label} ({len(names)})",
                      font=sec_font, fill=color, anchor="lm")
            draw.line([(0, y_pos + section_header_h), (img_width, y_pos + section_header_h)],
                      fill=TABLE_BORDER, width=1)
            y_pos += section_header_h

            row_font = ImageFont.truetype(FONT_SEMIBOLD, 13)
            rank_font = ImageFont.truetype(FONT_PATH, 12)
            col_w = img_width // 2
            max_name_w = col_w - padding_x - 40

            # Two-column layout
            num_rows = math.ceil(len(names) / 2)
            for row_idx in range(num_rows):
                row_center = y_pos + row_h // 2
                for col in range(2):
                    idx = row_idx * 2 + col
                    if idx >= len(names):
                        break
                    name = names[idx]
                    col_x = col * col_w + padding_x
                    draw.text((col_x + 8, row_center), f"{idx + 1}.",
                              font=rank_font, fill=TABLE_TEXT_MUTED, anchor="lm")
                    display_name = name
                    while draw.textlength(display_name, font=row_font) > max_name_w and len(display_name) > 3:
                        display_name = display_name[:-1]
                    if display_name != name:
                        display_name += "..."
                    draw.text((col_x + 32, row_center), display_name,
                              font=row_font, fill=TABLE_TEXT_BLACK, anchor="lm")
                y_pos += row_h
                if row_idx < num_rows - 1:
                    draw.line([(padding_x, y_pos), (img_width - padding_x, y_pos)],
                              fill=TABLE_BORDER, width=1)
            return y_pos

        if owners:
            y = draw_section("OWNED BY", owners, TABLE_GW_BLUE, y)
        if benched:
            y = draw_section("BENCHED BY", benched, TABLE_TEXT_MUTED, y)
        if not owners and not benched:
            no_own_font = ImageFont.truetype(FONT_PATH, 14)
            draw.text((img_width // 2, y + 20),
                      "Not owned by any managers in the league",
                      font=no_own_font, fill=TABLE_TEXT_MUTED, anchor="mm")
            y += 40

        # --- Footer ---
        _draw_footer(draw, img, img_width, y, footer_height, current_gw, font_size=13)

        img_byte_arr = io.BytesIO()
        img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
        img_byte_arr.seek(0)
        return img_byte_arr

    except Exception as e:
        logger.error(f"Error generating player ownership image: {e}", exc_info=True)
        return None


# =====================================================
# Fixtures — Single Team Image
# =====================================================

def generate_fixtures_single_image(team_info, fixtures, current_gw):
    """Generate a 520px-wide single-team fixtures image."""
    try:
        from collections import OrderedDict

        img_width = 440
        padding_x = 16
        header_height = 52
        legend_height = 36
        col_header_h = 34
        row_h = 36
        footer_height = 36
        fdr_bar_w = 80
        fdr_bar_h = 26

        # Group fixtures by GW for DGW handling
        gw_groups = OrderedDict()
        for f in fixtures:
            gw = f['gw']
            if gw not in gw_groups:
                gw_groups[gw] = []
            gw_groups[gw].append(f)

        num_rows = len(gw_groups)
        total_height = header_height + legend_height + col_header_h + (num_rows * row_h) + footer_height

        img = Image.new("RGBA", (img_width, total_height), TABLE_BG)
        draw = ImageDraw.Draw(img)

        # --- Header ---
        draw.rectangle([0, 0, img_width, header_height], fill=TABLE_HEADER_BG)
        hdr_font = ImageFont.truetype(FONT_BOLD, 18)
        hdr_detail_font = ImageFont.truetype(FONT_PATH, 15)
        team_name = team_info.get('name', 'Team')
        draw.text((padding_x, header_height // 2), f"{team_name} Fixtures",
                  font=hdr_font, fill=TABLE_TEXT_BLACK, anchor="lm")
        gws = list(gw_groups.keys())
        gw_range_text = f"GW{gws[0]}–GW{gws[-1]}" if gws else ""
        draw.text((img_width - padding_x, header_height // 2), gw_range_text,
                  font=hdr_detail_font, fill=TABLE_TEXT_MUTED, anchor="rm")
        draw.line([(0, header_height), (img_width, header_height)], fill=TABLE_BORDER, width=1)

        # --- FDR Legend ---
        legend_y = header_height + 6
        _draw_fdr_legend(draw, legend_y, img_width)

        # --- Column headers ---
        col_y = header_height + legend_height
        draw.rectangle([0, col_y, img_width, col_y + col_header_h], fill=TABLE_HEADER_BG)
        draw.line([(0, col_y), (img_width, col_y)], fill=TABLE_BORDER, width=1)
        col_font = ImageFont.truetype(FONT_BOLD, 13)
        col_gw_x = padding_x + 20
        col_fixture_x = padding_x + 80
        col_fdr_x = img_width - padding_x - fdr_bar_w
        draw.text((col_gw_x, col_y + col_header_h // 2), "GW",
                  font=col_font, fill=TABLE_TEXT_MUTED, anchor="mm")
        draw.text((col_fixture_x, col_y + col_header_h // 2), "FIXTURE",
                  font=col_font, fill=TABLE_TEXT_MUTED, anchor="lm")
        draw.text((col_fdr_x + fdr_bar_w // 2, col_y + col_header_h // 2), "FDR",
                  font=col_font, fill=TABLE_TEXT_MUTED, anchor="mm")
        draw.line([(0, col_y + col_header_h), (img_width, col_y + col_header_h)],
                  fill=TABLE_BORDER, width=1)

        # --- Fixture rows ---
        y = col_y + col_header_h
        gw_font = ImageFont.truetype(FONT_PATH, 15)
        fixture_font = ImageFont.truetype(FONT_SEMIBOLD, 15)
        fdr_font = ImageFont.truetype(FONT_SEMIBOLD, 14)

        for gw, group in gw_groups.items():
            row_center_y = y + row_h // 2

            draw.text((col_gw_x, row_center_y), str(gw),
                      font=gw_font, fill=TABLE_TEXT_MUTED, anchor="mm")

            if group[0].get('is_blank'):
                draw.text((col_fixture_x, row_center_y), "BLANK GAMEWEEK",
                          font=fixture_font, fill=TABLE_TEXT_MUTED, anchor="lm")
                bgw_bg, bgw_text = FDR_BGW
                bar_y = row_center_y - fdr_bar_h // 2
                draw.rounded_rectangle([col_fdr_x, bar_y, col_fdr_x + fdr_bar_w, bar_y + fdr_bar_h],
                                       radius=4, fill=bgw_bg)
                draw.text((col_fdr_x + fdr_bar_w // 2, row_center_y), "BGW",
                          font=fdr_font, fill=bgw_text, anchor="mm")
            elif len(group) == 1:
                f = group[0]
                venue = "(H)" if f['is_home'] else "(A)"
                fixture_text = f"{f['opponent']} {venue}"
                draw.text((col_fixture_x, row_center_y), fixture_text,
                          font=fixture_font, fill=TABLE_TEXT_BLACK, anchor="lm")
                fdr = f.get('fdr', 3)
                fdr_bg, fdr_text = FDR_COLORS.get(fdr, FDR_COLORS[3])
                bar_y = row_center_y - fdr_bar_h // 2
                draw.rounded_rectangle([col_fdr_x, bar_y, col_fdr_x + fdr_bar_w, bar_y + fdr_bar_h],
                                       radius=4, fill=fdr_bg)
                draw.text((col_fdr_x + fdr_bar_w // 2, row_center_y), str(fdr),
                          font=fdr_font, fill=fdr_text, anchor="mm")
            else:
                # DGW
                dgw_font = ImageFont.truetype(FONT_SEMIBOLD, 14)
                parts = []
                for f in group:
                    venue = "(H)" if f['is_home'] else "(A)"
                    parts.append(f"{f['opponent']}{venue}")
                draw.text((col_fixture_x, row_center_y), ", ".join(parts),
                          font=dgw_font, fill=TABLE_TEXT_BLACK, anchor="lm")
                half_w = (fdr_bar_w - 4) // 2
                for fi, f in enumerate(group[:2]):
                    fdr = f.get('fdr', 3)
                    fdr_bg, fdr_text = FDR_COLORS.get(fdr, FDR_COLORS[3])
                    bx = col_fdr_x + fi * (half_w + 4)
                    bar_y = row_center_y - fdr_bar_h // 2
                    draw.rounded_rectangle([bx, bar_y, bx + half_w, bar_y + fdr_bar_h],
                                           radius=4, fill=fdr_bg)
                    draw.text((bx + half_w // 2, row_center_y), str(fdr),
                              font=fdr_font, fill=fdr_text, anchor="mm")

            y += row_h
            draw.line([(padding_x, y), (img_width - padding_x, y)],
                      fill=TABLE_BORDER, width=1)

        # --- Footer ---
        _draw_footer(draw, img, img_width, y, footer_height, current_gw, font_size=13)

        img_byte_arr = io.BytesIO()
        img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
        img_byte_arr.seek(0)
        return img_byte_arr

    except Exception as e:
        logger.error(f"Error generating single fixtures image: {e}", exc_info=True)
        return None


# =====================================================
# Fixtures — All Teams Image
# =====================================================

def generate_fixtures_all_image(teams_fixtures, gw_range, current_gw):
    """Generate an all-teams fixture difficulty grid image."""
    try:
        img_width = 660
        padding_x = 12
        header_height = 52
        legend_height = 36
        col_header_h = 34
        row_h = 36
        footer_height = 42
        num_teams = len(teams_fixtures)
        num_gws = len(gw_range)

        total_height = header_height + legend_height + col_header_h + (num_teams * row_h) + footer_height

        team_col_w = 130
        gw_col_start = team_col_w + padding_x
        cell_gap = 6
        available_w = img_width - gw_col_start - padding_x
        cell_w = (available_w - (num_gws - 1) * cell_gap) // num_gws

        img = Image.new("RGBA", (img_width, total_height), TABLE_BG)
        draw = ImageDraw.Draw(img)

        # --- Header ---
        draw.rectangle([0, 0, img_width, header_height], fill=TABLE_HEADER_BG)
        hdr_font = ImageFont.truetype(FONT_BOLD, 18)
        hdr_detail_font = ImageFont.truetype(FONT_PATH, 15)
        draw.text((padding_x, header_height // 2), "Fixture Difficulty",
                  font=hdr_font, fill=TABLE_TEXT_BLACK, anchor="lm")
        gw_range_text = f"GW{gw_range[0]}–GW{gw_range[-1]}" if gw_range else ""
        draw.text((img_width - padding_x, header_height // 2), gw_range_text,
                  font=hdr_detail_font, fill=TABLE_TEXT_MUTED, anchor="rm")
        draw.line([(0, header_height), (img_width, header_height)], fill=TABLE_BORDER, width=1)

        # --- FDR Legend ---
        legend_y = header_height + 6
        _draw_fdr_legend(draw, legend_y, img_width)

        # --- Column headers ---
        col_y = header_height + legend_height
        draw.rectangle([0, col_y, img_width, col_y + col_header_h], fill=TABLE_HEADER_BG)
        draw.line([(0, col_y), (img_width, col_y)], fill=TABLE_BORDER, width=1)
        col_font = ImageFont.truetype(FONT_BOLD, 13)
        draw.text((padding_x + 8, col_y + col_header_h // 2), "TEAM",
                  font=col_font, fill=TABLE_TEXT_MUTED, anchor="lm")
        for gi, gw in enumerate(gw_range):
            cx = gw_col_start + gi * (cell_w + cell_gap) + cell_w // 2
            draw.text((cx, col_y + col_header_h // 2), f"GW{gw}",
                      font=col_font, fill=TABLE_TEXT_MUTED, anchor="mm")
        draw.line([(0, col_y + col_header_h), (img_width, col_y + col_header_h)],
                  fill=TABLE_BORDER, width=1)

        # --- Team rows ---
        y = col_y + col_header_h
        team_font = ImageFont.truetype(FONT_SEMIBOLD, 14)
        cell_font = ImageFont.truetype(FONT_SEMIBOLD, 13)
        cell_font_sm = ImageFont.truetype(FONT_SEMIBOLD, 10)
        cell_h = row_h - 6

        for team_data in teams_fixtures:
            row_center_y = y + row_h // 2

            draw.text((padding_x + 8, row_center_y), team_data['team_short'],
                      font=team_font, fill=TABLE_TEXT_BLACK, anchor="lm")

            gw_lookup = {}
            for f in team_data['fixtures']:
                gw = f['gw']
                if gw not in gw_lookup:
                    gw_lookup[gw] = []
                gw_lookup[gw].append(f)

            for gi, gw in enumerate(gw_range):
                cx = gw_col_start + gi * (cell_w + cell_gap)
                cy = row_center_y - cell_h // 2

                fxs = gw_lookup.get(gw, [])
                if not fxs or (len(fxs) == 1 and fxs[0].get('is_blank')):
                    bgw_bg, bgw_text = FDR_BGW
                    draw.rounded_rectangle([cx, cy, cx + cell_w, cy + cell_h],
                                           radius=3, fill=bgw_bg)
                    draw.text((cx + cell_w // 2, row_center_y), "BGW",
                              font=cell_font, fill=bgw_text, anchor="mm")
                elif len(fxs) == 1:
                    f = fxs[0]
                    fdr = f.get('fdr', 3)
                    fdr_bg, fdr_text = FDR_COLORS.get(fdr, FDR_COLORS[3])
                    draw.rounded_rectangle([cx, cy, cx + cell_w, cy + cell_h],
                                           radius=3, fill=fdr_bg)
                    venue = "(H)" if f['is_home'] else "(A)"
                    cell_text = f"{f['opponent']}{venue}"
                    draw.text((cx + cell_w // 2, row_center_y), cell_text,
                              font=cell_font, fill=fdr_text, anchor="mm")
                else:
                    # DGW — split cell
                    half_w = (cell_w - 2) // 2
                    for fi, f in enumerate(fxs[:2]):
                        fdr = f.get('fdr', 3)
                        fdr_bg, fdr_text = FDR_COLORS.get(fdr, FDR_COLORS[3])
                        hx = cx + fi * (half_w + 2)
                        draw.rounded_rectangle([hx, cy, hx + half_w, cy + cell_h],
                                               radius=3, fill=fdr_bg)
                        cell_text = f"{f['opponent']}"
                        draw.text((hx + half_w // 2, row_center_y), cell_text,
                                  font=cell_font_sm, fill=fdr_text, anchor="mm")

            y += row_h
            draw.line([(padding_x, y), (img_width - padding_x, y)],
                      fill=TABLE_BORDER, width=1)

        # --- Footer ---
        _draw_footer(draw, img, img_width, y, footer_height, current_gw, font_size=14)

        img_byte_arr = io.BytesIO()
        img.convert("RGB").save(img_byte_arr, format='PNG', quality=95)
        img_byte_arr.seek(0)
        return img_byte_arr

    except Exception as e:
        logger.error(f"Error generating all fixtures image: {e}", exc_info=True)
        return None
