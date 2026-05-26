# Uma Enjoyers HQ

A Discord moderation bot built with Python (disnake >= 2.11) using Discord Components V2.

A fully featured bot for moderation, management, and automation of Discord servers.
All messages use Components V2 -- no embeds and no emojis. Data storage via local JSON files.

## Getting Started

```bash
pip install -r requirements.txt
HAVEN_TOKEN=<your-bot-token> python main.py
```

**Environment Variables:**
| Variable | Description | Default |
|----------|-------------|---------|
| `HAVEN_TOKEN` | Bot token (required) | -- |
| `DATA_DIR` | Directory for JSON data files | `data` |

## Features (99 commands)

### Moderation
| Command | Description |
|---------|-------------|
| `/mute` | Timeout with interactive duration selection |
| `/unmute` | Remove timeout |
| `/warn` | Warning (+ auto-actions by thresholds) |
| `/warnings` | List warnings |
| `/delwarn` | Delete a specific warning |
| `/clearwarns` | Clear all warnings |
| `/kick` | Kick a member |
| `/softban` | Ban + unban (message deletion) |
| `/ban` | Ban (with temporary ban support) |
| `/unban` | Unban by ID |
| `/massban` | Ban multiple by ID |
| `/purge` | Delete messages with filters |
| `/purgeuser` | Delete messages from a specific user |
| `/slowmode` | Slow mode |
| `/slowmodepresets` | Slowmode presets (calm/moderate/strict/lockdown) |
| `/lock` / `/unlock` | Lock / unlock a channel |
| `/lockdown` / `/unlockdown` | Lock the entire server |
| `/nuke` | Recreate a channel |
| `/quarantine` / `/unquarantine` | Quarantine with role preservation |
| `/massnick` | Mass nickname change |

### Rules System
| Command | Description |
|---------|-------------|
| `/rules add/remove/edit/list/clear` | Manage rules |
| `/rules display` | Beautiful rules panel in a channel |
| `/rules setrole` | "Accept rules" button with role assignment |

### Anti-Nuke Protection
| Command | Description |
|---------|-------------|
| `/antinuke enable/disable/status` | Nuke protection |
| `/antinuke trusted` | Trusted users |

### Auto-Moderation
| Command | Description |
|---------|-------------|
| `/automod toggle` | Enable/disable module |
| `/automod status` | Module status |
| `/warnthresholds set/remove/list` | Auto-actions by warning count |
| `/whitelist add/remove/list` | Domain whitelist |

Modules: anti-spam, anti-raid, anti-caps, anti-links, anti-invites, anti-mentions

### Voice Channels
`/voice kick/move/moveall/limit/mute/unmute/deafen/undeafen`

### Channel and Thread Management
| Command | Description |
|---------|-------------|
| `/channel clone/rename/topic/create/delete` | Channels |
| `/thread create/archive/lock/unlock/rename` | Threads |

### Ticket System
- Panel with 6 categories
- 4 priority levels
- Claim, transcript, archive, deletion

### Modmail
| Command | Description |
|---------|-------------|
| `/modmail` | Anonymous message to moderators |
| `/modmailreply` | Reply via DM |

### Self-Assignable Roles
`/selfroles create/addrole/send` -- role panels with dropdown

### Information
| Command | Description |
|---------|-------------|
| `/userinfo`, `/avatar`, `/banner` | User |
| `/serverinfo`, `/serversettings` | Server |
| `/roleinfo`, `/rolehierarchy`, `/rolecount` | Roles |
| `/channelinfo`, `/channelcount` | Channels |
| `/history`, `/stats`, `/modleaderboard` | Moderation |
| `/invites`, `/inviteinfo`, `/createinvite` | Invites |
| `/boosts`, `/botlist`, `/membercount` | Statistics |
| `/oldestmembers`, `/newestmembers`, `/joinposition` | Members |
| `/slowmodeinfo`, `/messagestats` | Analytics |
| `/permissions`, `/color`, `/firstmessage`, `/topic` | Utilities |
| `/whois`, `/emojiinfo` | Lookup |

### Tools
| Command | Description |
|---------|-------------|
| `/note add/list/delete/clear` | Moderator notes |
| `/role add/remove/members` | Role management |
| `/roleall` | Mass role assignment |
| `/rolecolor` | Change role color |
| `/reactionrole create/addrole/send` | Reaction roles |
| `/afk` | AFK status |
| `/remind` | Reminders |
| `/giveaway start/end/reroll` | Giveaways |
| `/poll create` | Polls |
| `/snipe`, `/editsnipe` | Deleted/edited messages |
| `/suggest` | Suggestion system |
| `/report`, `/reports`, `/resolve` | Reports |
| `/verify` | Verification |
| `/counting` | Counting game |
| `/say`, `/announce` | Bot messages |
| `/embed` | Custom messages |
| `/dm` | DMs from the bot |
| `/emojisteal` | Copy emojis |
| `/sticky set/remove` | Sticky messages |
| `/schedule message/list` | Scheduled messages |
| `/customcmd add/remove/list` | Custom commands |
| `/autoresponder add/remove/list` | Auto-responses |
| `/cleanup bots/links/images/contains/embeds` | Advanced cleanup |
| `/backup`, `/restore` | Export/import settings |
| `/exportwarns`, `/exportcases` | Export moderation |
| `/nick set/reset` | Nickname management |
| `/haven`, `/ping` | About the bot |

### Event Logging
- Member join / leave (+ milestones)
- Role changes
- Message deletion / editing
- Channel / role creation / deletion
- Ban / unban (+ anti-nuke)
- Voice channels

## Storage (JSON Files)

The bot uses local JSON files for all data storage (in the `data/` directory). Files:

| File | Contents |
|------|----------|
| `config` | Server settings |
| `warnings` | Warnings |
| `cases` | Moderation cases |
| `tickets` | Tickets |
| `tempbans` | Temporary bans |
| `rules` | Server rules |
| `warnthresholds` | Warning thresholds |
| `notes` | Moderator notes |
| `modmail` | Modmail messages |
| `selfroles` | Role panels |
| `customcmds` | Custom commands |
| `autoresponders` | Auto-responses |
| `scheduled` | Scheduled messages |
| `quarantined` | Quarantine |
| `antinuke_trusted` | Trusted users (anti-nuke) |
| `whitelist` | Domain whitelist |
| `reminders` | Reminders |
| `giveaways` | Giveaways |
| `reports` | Reports |
| `suggestions` | Suggestions |

## Technical Details

- Python 3.11+
- disnake >= 2.11
- No external database required (JSON file storage)
- Discord Components V2 (no embeds)
- All in one file `main.py` (~7800 lines)
- 99 slash commands
- 4 background tasks: tempbans (20s), reminders (15s), giveaways (30s), schedule (30s)
- Monochrome color scheme with no emojis
- Interface fully in English
- In-memory cache + synchronous writes to JSON files
