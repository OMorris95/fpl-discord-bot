import os
import requests

# --- CONFIGURATION ---
BASE_API_URL = "https://fantasy.premierleague.com/api/"

# **NEW: Correct URL for the 2025/26 season headshots, based on your examples**
# Note the path now includes "premierleague25" and the "p" is removed from the filename.
NEW_SEASON_PLAYER_IMAGE_URL = "https://resources.premierleague.com/premierleague25/photos/players/110x140/{}.png"
# **Legacy URL kept as a fallback**
LEGACY_PLAYER_IMAGE_URL = "https://resources.premierleague.com/premierleague/photos/players/110x140/p{}.png"

JERSEY_IMAGE_BASE_URL = "https://fantasy.premierleague.com/dist/img/shirts/standard/"
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
}
HEADSHOTS_DIR = "player_headshots"
JERSEYS_DIR = "team_jerseys"

# --- MAIN FUNCTION ---
def download_images():
    """
    Downloads all player headshots and team jerseys from the FPL API.
    """
    os.makedirs(HEADSHOTS_DIR, exist_ok=True)
    os.makedirs(JERSEYS_DIR, exist_ok=True)

    # 1. Fetch general game data
    try:
        print("Fetching latest FPL data...")
        response = requests.get(f"{BASE_API_URL}bootstrap-static/", headers=REQUEST_HEADERS)
        response.raise_for_status()
        data = response.json()
        print("Data fetched successfully.")
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching data from FPL API: {e}")
        return

    # 2. Download player headshots
    print("\nDownloading player headshots...")
    players = data.get('elements', [])
    downloaded_headshots = 0
    for player in players:
        player_code = player.get('code')
        player_name = player.get('web_name')
        player_id = player.get('id')
        
        if not all([player_code, player_name, player_id]):
            continue
        
        safe_player_name = player_name.replace(' ', '_')
        filename = f"{safe_player_name}_{player_id}.png"
        filepath = os.path.join(HEADSHOTS_DIR, filename)
        
        if os.path.exists(filepath):
            downloaded_headshots += 1
            continue

        # **UPDATED LOGIC: A list of URLs to try, starting with the new season's URL**
        urls_to_try = [
            NEW_SEASON_PLAYER_IMAGE_URL.format(player_code),
            LEGACY_PLAYER_IMAGE_URL.format(player_code)
        ]
        
        for image_url in urls_to_try:
            try:
                img_response = requests.get(image_url, headers=REQUEST_HEADERS)
                img_response.raise_for_status() # Will fail if the image isn't found (404)
                
                with open(filepath, 'wb') as f:
                    f.write(img_response.content)
                downloaded_headshots += 1
                break # On success, stop trying other URLs
            except requests.exceptions.RequestException:
                continue # If it fails, try the next URL in the list
            
    print(f"Finished downloading player headshots. Total headshots in folder: {downloaded_headshots}")

    # 3. Download team jerseys
    print("\nDownloading team jerseys...")
    teams = data.get('teams', [])
    downloaded_jerseys = 0
    
    for team in teams:
        team_code = team.get('code')
        team_name = team.get('name', f'team_{team_code}').replace(' ', '_')
        if not team_code:
            continue

        kits_to_download = {
            'home': f"shirt_{team_code}-110.webp",
            'away': f"shirt_{team_code}_2-110.webp",
            'third': f"shirt_{team_code}_3-110.webp",
            'goalkeeper': f"shirt_{team_code}_1-110.webp"
        }
        
        for kit_name, remote_filename in kits_to_download.items():
            filepath = os.path.join(JERSEYS_DIR, f"{team_name}_{kit_name}.webp")
            
            if os.path.exists(filepath):
                downloaded_jerseys +=1
                continue
            
            jersey_url = f"{JERSEY_IMAGE_BASE_URL}{remote_filename}"
            try:
                jersey_response = requests.get(jersey_url, headers=REQUEST_HEADERS)
                jersey_response.raise_for_status()
                with open(filepath, 'wb') as f:
                    f.write(jersey_response.content)
                downloaded_jerseys += 1
            except requests.exceptions.RequestException:
                pass

    print(f"Finished downloading team jerseys. Total jerseys in folder: {downloaded_jerseys}")
    print("\n✅ Finished downloading all assets.")

if __name__ == "__main__":
    download_images()
