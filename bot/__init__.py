# Bot module exports
from .logging_config import get_logger, logger

from .database import (
    init_database,
    upsert_league_teams,
    get_fpl_id_for_user,
    get_linked_user_for_team,
    link_user_to_team,
    get_unclaimed_teams,
    get_all_teams_for_autocomplete,
    get_team_by_fpl_id,
    get_linked_users,
    get_all_league_teams,
    is_live_alert_subscribed,
    add_live_alert_subscription,
    remove_live_alert_subscription,
    get_all_live_alert_subscriptions,
    is_transfer_alert_subscribed,
    set_transfer_alert_subscription,
    DB_PATH,
)

from .api import get_live_manager_details

from .backend_api import (
    get_bootstrap,
    get_live_data,
    get_fixtures,
    get_league_standings,
    get_league_picks,
    get_league_history,
    get_league_transfers,
    get_manager_picks,
    get_manager_history,
    get_manager_transfers,
    get_current_gameweek,
    get_last_completed_gameweek,
    get_gameweek_info,
)

from .image_generator import (
    format_player_price,
    build_manager_url,
    format_manager_link,
    get_jersey_filename,
    load_jersey_image,
    calculate_player_coordinates,
    generate_team_image,
    generate_dreamteam_image,
    BACKGROUND_IMAGE_PATH,
    FONT_PATH,
    JERSEYS_DIR,
    JERSEY_SIZE,
)
