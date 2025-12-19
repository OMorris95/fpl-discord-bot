Implementation Guide: Advanced FPL Bot Features

Context

This guide details the implementation logic for five specific FPL bot commands. It assumes the existence of the league_teams database table defined in the "Linking Guide" to resolve Discord Users to FPL Team IDs.

1. Feature: Live Goal Alerts (/goals & /toggle_goals)

Type: Background Task (Loop) + Configuration Command.

A. Database Schema

We need to know which channels want goal alerts.

Table: goal_subscriptions
| Column | Type | Description |
| :--- | :--- | :--- |
| channel_id | String/BigInt | The Discord Channel ID to post alerts in. |
| league_id | Integer | The FPL League ID to track owners for (optional, defaults to main league). |

B. Command: /toggle_goals

Logic:

Check if channel_id exists in goal_subscriptions.

If Exists: Delete row (Unsubscribe). Reply: "🔴 Live goal alerts disabled for this channel."

If New: Insert row (Subscribe). Reply: "🟢 Live goal alerts enabled. I will post goals as they happen."

C. The Polling Logic (Background Task)

Frequency: Every 60 seconds during live match windows.
Endpoint: https://fantasy.premierleague.com/api/event/{current_gw}/live/

Algorithm:

Cache State: Maintain a dictionary in memory last_known_goals = { player_id: count }.

Fetch: Get the live data.

Iterate: Loop through all elements (players) in the live data.

Compare:

If player.stats.goals_scored > last_known_goals[player.id]:

EVENT DETECTED: GOAL!

Identify Player Name (from bootstrap-static data).

Calculate goals_new - goals_old (usually 1).

Identify Owners:

Query league_teams to find which managers own this player.

Optimization: You might need to cache managers' picks for the current GW to avoid spamming the API for every goal.

Broadcast:

Loop through all channel_ids in goal_subscriptions.

Send Embed:

Title: ⚽ GOAL: {Player_Name} ({Team})

Description: "Scored against {Opponent}!"

Fields: "Owned By: @User1, @User2", "Benched By: @User3 (🤡)"

2. Feature: Fixture Ticker (/fixtures)

Type: Slash Command.
Endpoint: https://fantasy.premierleague.com/api/fixtures/

Logic:

Filter: Get fixtures where finished = false and event (gameweek) is within the next 3-5 weeks.

Input: User provides a team (Autocomplete) or all.

FDR (Fixture Difficulty Rating):

The API provides team_h_difficulty and team_a_difficulty (1-5 scale).

Mapping:

1-2: 🟩 (Green Square) - Easy

3: ⬜ (White/Grey Square) - Medium

4: 🟧 (Orange Square) - Hard

5: 🟥 (Red Square) - Very Hard

Output: Construct an Embed.

Row format: GW{x}: vs {Opponent} (H) 🟩

3. Feature: Head-to-Head (/h2h)

Type: Slash Command.
Usage: /h2h [rival_user] (Compare me vs rival) OR /h2h [user_a] [user_b].
Endpoint: https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/

Logic:

Resolve IDs: Convert Discord User mentions to FPL IDs using league_teams table.

Fetch Picks: Call the endpoint for both managers.

Set Operations:

Team_A_Players = Set of IDs in Team A.

Team_B_Players = Set of IDs in Team B.

Common = Team_A Intersection Team_B.

Differentials_A = Team_A - Common.

Differentials_B = Team_B - Common.

Output: Two-column Embed.

Col 1 ({User A}): List names of Differentials_A (e.g., "Haaland, Saka").

Col 2 ({User B}): List names of Differentials_B (e.g., "Kane, Son").

Footer: "7 players in common."

4. Feature: Wall of Shame (/shame)

Type: Slash Command.
Goal: Highlight manager mistakes for the current week.

Logic:

Scope: Fetch current GW picks for all linked users in league_teams.

Metrics Calculation:

Bench Points: Sum points of players in positions 12, 13, 14, 15 (API uses 1-indexed positions 1-11 for starters, 12-15 for bench).

Captain Fail: Check if is_captain == true and points < 2.

Transfer Flop: Requires fetching transfers endpoint. Compare points_of_player_out vs points_of_player_in.

Sorting: Sort list by "Bench Points" descending.

Output:

Title: 🤡 Gameweek {GW} Wall of Shame

Entry 1: 🥇 @Dave - Benched 24 points! (Archer 12, Senesi 8)

Entry 2: 🥈 @Sarah - Captained Haaland (2pts) instead of Salah (15pts).

5. Feature: Chip Usage Matrix (/chips)

Type: Slash Command.
Endpoint: https://fantasy.premierleague.com/api/entry/{team_id}/history/

Logic:

Iterate: Loop through all linked managers.

Fetch History: For each manager, look at the chips array in the response.

Check Status:

Track status of: wildcard_1, wildcard_2, freehit, 3xc (Triple Captain), bboost (Bench Boost).

Note: Wildcards reset halfway through the season.

Formatting: Use a Monospaced Code Block for a grid table.

Output Example:

/chips
-----------------------------------
Manager    | WC | FH | TC | BB
-----------------------------------
John       | ✅ | ❌ | ✅ | ❌
Sarah      | ✅ | ✅ | ❌ | ❌
Mike       | ❌ | ❌ | ❌ | ✅
-----------------------------------
✅ = Used   ❌ = Available
