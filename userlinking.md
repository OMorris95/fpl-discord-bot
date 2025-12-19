# **Implementation Guide: Discord User to FPL Team Linking System**

## **Context**

We are building a Discord bot for Fantasy Premier League (FPL). We need to implement a system that links a Discord User ID to a specific FPL Team ID within a specific league. This allows the bot to know which team belongs to which user for commands like /live, /shame, and /rivals.

## **1\. Database Schema**

We need a database table to store the league inventory. We are moving away from a simple "User-\>Team" mapping to a "League Inventory" model.

**Table Name:** league\_teams

| Column Name | Data Type | Constraints | Description |
| :---- | :---- | :---- | :---- |
| fpl\_team\_id | Integer | PRIMARY KEY | The unique ID of the team from FPL. |
| league\_id | Integer | NOT NULL | The classic league ID this team belongs to. |
| team\_name | String | NOT NULL | The team name (e.g., "Klopp Kops"). |
| manager\_name | String | NOT NULL | The manager's real name (e.g., "John Smith"). |
| discord\_user\_id | String | NULLABLE | The Discord User ID (Snowflake) claiming this team. |

## **2\. Workflow: League Initialization (/setleague)**

**Trigger:** Admin runs /setleague \[league\_id\].

**Logic:**

1. **Fetch Data:** GET https://fantasy.premierleague.com/api/leagues-classic/{league\_id}/standings/  
2. **Iterate:** Loop through the standings.results array.  
3. **Upsert (Insert or Update):**  
   * Check if fpl\_team\_id exists in the database.  
   * **If New:** Insert the row. discord\_user\_id should be NULL.  
   * **If Exists:** Update the team\_name and manager\_name (in case they changed on FPL). **DO NOT** overwrite an existing discord\_user\_id.  
4. **Feedback:** Reply to the admin: "League initialized. X teams found. Users can now use /claim."

## **3\. Workflow: User Claiming (/claim)**

Trigger: User runs /claim \[team\_name\].  
Tech Requirement: This command MUST use Discord Autocomplete for the team\_name parameter.

### **Autocomplete Logic**

1. User starts typing.  
2. Bot queries the league\_teams database table.  
3. Filter: WHERE team\_name LIKE %input%.  
4. Return: A list of Choice(name="Team Name (Manager)", value=fpl\_team\_id).

### **Command Execution Logic**

When the user submits the command with a specific fpl\_team\_id:

1. **Fetch Row:** Get the team details from league\_teams using the fpl\_team\_id.  
2. **Check Ownership:**  
   * **Scenario A: Unclaimed (discord\_user\_id IS NULL)**  
     * **Action:** Update the row: Set discord\_user\_id \= Context User ID.  
     * **Response:** Ephemeral message: "✅ Success\! You have been linked to **{team\_name}**."  
   * **Scenario B: Claimed (discord\_user\_id IS NOT NULL)**  
     * **Action:** Trigger the **Admin Approval Workflow** (See Section 4).  
     * **Response:** Ephemeral message: "⚠️ That team is already linked to another user. An admin approval request has been sent."

## **4\. Workflow: Admin Conflict Resolution (Buttons)**

If Scenario B occurs (Hostile Takeover), do not update the database immediately. Instead:

1. **Construct Embed:**  
   * Title: 🚨 Claim Conflict  
   * Description: \<@NewUser\> wants to claim **{team\_name}**. Currently owned by \<@OldUser\>.  
   * Fields: FPL ID, Team Name.  
2. **Create View (Buttons):**  
   * \[Green Button\]: Approve Transfer (Custom ID: approve\_claim\_{fpl\_id}\_{new\_user\_id})  
   * \[Red Button\]: Deny Request (Custom ID: deny\_claim\_{new\_user\_id})  
3. **Send:** Post this message to the configured Admin Log Channel.

### **Button Callbacks**

* **On "Approve":**  
  1. **Update DB:** Set discord\_user\_id \= new\_user\_id for that fpl\_team\_id.  
  2. **Edit Message:** Change Embed color to Green. Update status to "✅ Approved by {AdminUser}". Disable buttons.  
  3. **Notify:** DM the New User: "Your claim for **{team\_name}** was approved."  
* **On "Deny":**  
  1. **Update DB:** Do nothing.  
  2. **Edit Message:** Change Embed color to Red. Update status to "⛔ Denied by {AdminUser}". Disable buttons.  
  3. **Notify:** DM the New User: "Your claim for **{team\_name}** was denied."

## **5\. Workflow: Manual Assignment (/assign)**

Trigger: Admin runs /assign \[user\_mention\] \[team\_name\].  
Tech Requirement: Use Autocomplete for team\_name (same as /claim).  
**Logic:**

1. **Force Update:** Update league\_teams set discord\_user\_id \= user\_mention.id WHERE fpl\_team\_id \= selected\_value.  
2. **Response:** "✅ Manually linked {User} to {Team}."

## **6\. Helper Functions to Implement**

The bot will need these core helpers for other commands (like /live):

* get\_fpl\_id(discord\_user\_id): Returns the fpl\_team\_id or None.  
* get\_discord\_user(fpl\_team\_id): Returns the discord\_user\_id or None.