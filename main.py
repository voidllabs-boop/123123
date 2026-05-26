"""Uma Enjoyers HQ -- Discord moderation bot (disnake >= 2.11, Components V2).

A comprehensive, production-grade moderation bot with:
- Full moderation suite (mute, warn, kick, ban, softban, massban, purge, lock, slowmode)
- Ticket system with priorities, transcripts, claiming, archiving
- Auto-moderation (anti-spam, anti-raid, caps filter, link filter, invite filter, mention spam)
- Reaction roles
- AFK system
- Moderator notes on users
- Reminders
- Giveaways
- Detailed server/user/role/channel info commands
- Comprehensive event logging
- Role management (add/remove/info/members)
- Server statistics
- Custom embed-free UI built entirely with Discord Components V2
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import random
import re
import string
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import disnake
from disnake import (
    ButtonStyle,
    Color,
    InteractionContextTypes,
    MessageFlags,
    SelectOption,
    SeparatorSpacing,
)
from disnake.ext import commands, tasks
from pathlib import Path
from keep_alive import keep_alive  # Import the function from file

keep_alive()  # Now this works perfectly!

# ═══════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

GUILD_ONLY = InteractionContextTypes(guild=True, bot_dm=False, private_channel=False)

COLORS: dict[str, int] = {
    "plain": 0x2C2F33,
    "ok": 0x57F287,
    "warn": 0xFEE75C,
    "error": 0xED4245,
    "info": 0x5865F2,
}

VERSION = "1.0.0"
TICKET_CATEGORY_NAME = "Tickets"
ARCHIVE_CATEGORY_NAME = "Archived Tickets"
MAX_TRANSCRIPT_MESSAGES = 500
ANTI_SPAM_WINDOW = 5
ANTI_SPAM_THRESHOLD = 5
ANTI_RAID_WINDOW = 10
ANTI_RAID_THRESHOLD = 8
CAPS_THRESHOLD = 0.7
CAPS_MIN_LENGTH = 10
MAX_MENTIONS_PER_MSG = 5
INVITE_PATTERN = re.compile(
    r"(discord\.gg|discordapp\.com/invite|discord\.com/invite)/[a-zA-Z0-9]+",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# ═══════════════════════════════════════════════════════════════════════════
#  BOT SETUP
# ═══════════════════════════════════════════════════════════════════════════

intents = disnake.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.InteractionBot(intents=intents)

# ═══════════════════════════════════════════════════════════════════════════
#  JSON FILE STORAGE
# ═══════════════════════════════════════════════════════════════════════════

_DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_COLLECTIONS = [
    "config", "warnings", "tickets", "tempbans", "cases", "notes",
    "reactionroles", "customcmds", "autoresponders", "stickies",
    "rules", "warnthresholds", "quarantined", "whitelist", "modmail",
    "scheduled", "selfroles", "antinuke_trusted", "reports",
    "suggestions", "reminders", "giveaways", "polls",
]

# In-memory write-through cache: "collection.json" -> dict
_store: dict[str, dict] = {}

# In-memory caches for anti-spam / anti-raid
_spam_cache: dict[int, list[float]] = defaultdict(list)
_join_cache: dict[int, list[float]] = defaultdict(list)
_afk_users: dict[str, dict[str, str]] = {}  # guild:user -> {message, ts}
_snipe_cache: dict[int, dict[str, Any]] = {}
_editsnipe_cache: dict[int, dict[str, Any]] = {}


def _json_load_all() -> None:
    """Load all collections from JSON files into in-memory cache."""
    for name in _COLLECTIONS:
        fp = _DATA_DIR / f"{name}.json"
        if fp.exists():
            try:
                _store[f"{name}.json"] = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _store[f"{name}.json"] = {}
        else:
            _store[f"{name}.json"] = {}
    print("Storage: JSON files loaded")


def _json_write(col: str, data: dict) -> None:
    """Write data to a JSON file."""
    try:
        fp = _DATA_DIR / f"{col}.json"
        safe = copy.deepcopy(data)
        fp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Storage] write error in '{col}': {e}")


def _load(path: str, default: Any = None) -> Any:
    key = path if path.endswith(".json") else f"{path}.json"
    return _store.get(key, default if default is not None else {})


def _save(path: str, data: Any) -> None:
    key = path if path.endswith(".json") else f"{path}.json"
    _store[key] = data
    col = key.replace(".json", "")
    _json_write(col, data)


def save(key: str) -> tuple[dict, Any]:
    """Return (data, flush) for the given storage key."""
    path = f"{key}.json"
    data = _load(path, {})
    _store[path] = data

    def flush() -> None:
        _save(path, data)

    return data, flush


# ═══════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

_DUR_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_DUR_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_dur(s: str) -> timedelta | None:
    m = _DUR_RE.match(s.strip())
    if not m:
        return None
    return timedelta(seconds=int(m.group(1)) * _DUR_UNITS[m.group(2).lower()])


def fmt_dur(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "0s"
    parts: list[str] = []
    for label, div in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        val, total = divmod(total, div)
        if val:
            parts.append(f"{val}{label}")
    return " ".join(parts)


def fmt_ts(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> int:
    return int(time.time())


def _check_hierarchy(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> bool:
    return inter.author.top_role > member.top_role  # type: ignore[union-attr]


def _guild_key(guild: disnake.Guild | None) -> str:
    return str(guild.id) if guild else "0"


def _user_key(user: disnake.User | disnake.Member) -> str:
    return str(user.id)


def _gen_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def truncate(text: str, limit: int = 1000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} {many}"
    mod = n % 10
    if mod == 1:
        return f"{n} {one}"
    if 2 <= mod <= 4:
        return f"{n} {few}"
    return f"{n} {many}"


def _status_text(status: disnake.Status) -> str:
    mapping = {
        disnake.Status.online: "Online",
        disnake.Status.idle: "Idle",
        disnake.Status.dnd: "Do Not Disturb",
        disnake.Status.offline: "Offline",
        disnake.Status.invisible: "Invisible",
    }
    return mapping.get(status, "Unknown")


# ═══════════════════════════════════════════════════════════════════════════
#  CASE SYSTEM
# ═══════════════════════════════════════════════════════════════════════════


def add_case(
    guild: disnake.Guild,
    user: disnake.User | disnake.Member,
    action: str,
    mod: disnake.User | disnake.Member,
    reason: str,
    duration: str | None = None,
) -> int:
    cases, flush = save("cases")
    gk, uk = _guild_key(guild), _user_key(user)
    cases.setdefault(gk, {}).setdefault(uk, [])
    cid = len(cases[gk][uk]) + 1
    cases[gk][uk].append(
        {
            "id": cid,
            "action": action,
            "mod": str(mod.id),
            "mod_name": str(mod),
            "reason": reason,
            "duration": duration,
            "ts": now_iso(),
        }
    )
    flush()
    return cid


def get_case_count(guild: disnake.Guild, user: disnake.User | disnake.Member) -> int:
    cases = _load("cases.json", {})
    gk, uk = _guild_key(guild), _user_key(user)
    return len(cases.get(gk, {}).get(uk, []))


def get_total_cases(guild: disnake.Guild) -> int:
    cases = _load("cases.json", {})
    gk = _guild_key(guild)
    guild_cases = cases.get(gk, {})
    return sum(len(v) for v in guild_cases.values())


# ═══════════════════════════════════════════════════════════════════════════
#  COMPONENTS V2 HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def card(
    title: str,
    fields: list[tuple[str, str]],
    kind: str = "plain",
    footer: str | None = None,
) -> list[disnake.ui.Container]:
    children: list[Any] = []
    children.append(disnake.ui.TextDisplay(f"### {title}"))
    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    for key, value in fields:
        children.append(disnake.ui.TextDisplay(f"**{key}:** {value}"))
    if footer:
        children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
        children.append(disnake.ui.TextDisplay(f"-# {footer}"))
    return [
        disnake.ui.Container(
            *children,
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


def simple(
    title: str,
    body: str,
    kind: str = "plain",
    footer: str | None = None,
) -> list[disnake.ui.Container]:
    children: list[Any] = [
        disnake.ui.TextDisplay(f"### {title}"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
        disnake.ui.TextDisplay(body),
    ]
    if footer:
        children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
        children.append(disnake.ui.TextDisplay(f"-# {footer}"))
    return [
        disnake.ui.Container(
            *children,
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


def profile_card(
    title: str,
    text_lines: list[str],
    avatar_url: str,
    kind: str = "info",
    footer: str | None = None,
) -> list[disnake.ui.Container]:
    children: list[Any] = [
        disnake.ui.Section(
            disnake.ui.TextDisplay(f"### {title}\n" + "\n".join(text_lines)),
            accessory=disnake.ui.Thumbnail(avatar_url),
        ),
    ]
    if footer:
        children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
        children.append(disnake.ui.TextDisplay(f"-# {footer}"))
    return [
        disnake.ui.Container(
            *children,
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


def list_card(
    title: str,
    items: list[str],
    kind: str = "info",
    footer: str | None = None,
) -> list[disnake.ui.Container]:
    children: list[Any] = [disnake.ui.TextDisplay(f"### {title}")]
    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    for item in items:
        children.append(disnake.ui.TextDisplay(item))
    if footer:
        children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
        children.append(disnake.ui.TextDisplay(f"-# {footer}"))
    return [
        disnake.ui.Container(
            *children,
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


def multi_container(
    containers: list[list[disnake.ui.Container]],
) -> list[disnake.ui.Container]:
    result: list[disnake.ui.Container] = []
    for c in containers:
        result.extend(c)
    return result


async def send_log(guild: disnake.Guild, components: list[Any]) -> None:
    cfg = _load("config.json", {})
    gk = _guild_key(guild)
    ch_id = cfg.get(gk, {}).get("log_channel")
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id))
    if ch and isinstance(ch, disnake.TextChannel):
        try:
            await ch.send(components=components, flags=MessageFlags(is_components_v2=True))
        except disnake.HTTPException:
            pass


async def _try_dm(user: disnake.User | disnake.Member, components: list[Any]) -> bool:
    try:
        await user.send(components=components, flags=MessageFlags(is_components_v2=True))
        return True
    except disnake.Forbidden:
        return False


async def _respond(
    inter: disnake.ApplicationCommandInteraction | disnake.MessageInteraction | disnake.ModalInteraction,
    components: list[Any],
    ephemeral: bool = False,
    file: disnake.File | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "components": components,
        "flags": MessageFlags(is_components_v2=True),
    }
    if ephemeral:
        kwargs["ephemeral"] = True
    if file:
        kwargs["file"] = file
    try:
        await inter.response.send_message(**kwargs)
    except disnake.InteractionResponded:
        await inter.followup.send(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════


def get_cfg(guild: disnake.Guild | None) -> dict[str, Any]:
    cfg = _load("config.json", {})
    return cfg.get(_guild_key(guild), {})


def set_cfg(guild: disnake.Guild | None, key: str, value: Any) -> None:
    cfg = _load("config.json", {})
    gk = _guild_key(guild)
    cfg.setdefault(gk, {})[key] = value
    _save("config.json", cfg)


def get_automod_cfg(guild: disnake.Guild) -> dict[str, Any]:
    cfg = get_cfg(guild)
    return cfg.get("automod", {
        "anti_spam": False,
        "anti_raid": False,
        "anti_caps": False,
        "anti_links": False,
        "anti_invites": False,
        "anti_mentions": False,
        "max_mentions": MAX_MENTIONS_PER_MSG,
    })


def set_automod_cfg(guild: disnake.Guild, automod: dict[str, Any]) -> None:
    set_cfg(guild, "automod", automod)


# ═══════════════════════════════════════════════════════════════════════════
#  /setup
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="setup",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def setup_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@setup_group.sub_command(name="logs", description="Channel for moderation logs")
async def setup_logs(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "log_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Log channel set: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="welcome", description="Welcome channel")
async def setup_welcome(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "welcome_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Welcome channel: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="leave", description="Leave channel")
async def setup_leave(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "leave_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Leave channel: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="modrole", description="Moderator role")
async def setup_modrole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "mod_role", str(role.id))
    await _respond(
        inter,
        simple("Setup", f"Moderator role: {role.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="muterole", description="Mute role (additional)")
async def setup_muterole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "mute_role", str(role.id))
    await _respond(
        inter,
        simple("Setup", f"Mute role: {role.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="tickets", description="Post ticket panel to a channel")
async def setup_tickets(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "ticket_panel_channel", str(channel.id))

    panel = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("### Ticket System"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                "Select the type of request below.\n"
                "A private channel will be created for communication with the moderation team.\n\n"
                "Please describe your issue in as much detail as possible after opening the ticket."
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(label="Question", custom_id="ticket:question", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Complaint", custom_id="ticket:complaint", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Appeal", custom_id="ticket:appeal", style=ButtonStyle.secondary),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(label="Bug", custom_id="ticket:bug", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Partnership", custom_id="ticket:partnership", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Other", custom_id="ticket:other", style=ButtonStyle.danger),
            ),
            accent_colour=Color(COLORS["plain"]),
        )
    ]

    await channel.send(components=panel, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Setup", f"Ticket panel sent to {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@setup_group.sub_command(name="info", description="Current server settings")
async def setup_info(inter: disnake.ApplicationCommandInteraction) -> None:
    cfg = get_cfg(inter.guild)
    automod = cfg.get("automod", {})

    fields: list[tuple[str, str]] = []
    for key, label in [
        ("log_channel", "Log channel"),
        ("welcome_channel", "Welcome channel"),
        ("leave_channel", "Leave channel"),
        ("ticket_panel_channel", "Ticket panel"),
        ("mod_role", "Moderator role"),
        ("mute_role", "Mute role"),
    ]:
        cid = cfg.get(key)
        if key.endswith("_role"):
            fields.append((label, f"<@&{cid}>" if cid else "Not set"))
        else:
            fields.append((label, f"<#{cid}>" if cid else "Not set"))

    am_status = []
    for k, label in [
        ("anti_spam", "Anti-Spam"),
        ("anti_raid", "Anti-Raid"),
        ("anti_caps", "Anti-Caps"),
        ("anti_links", "Anti-Links"),
        ("anti_invites", "Anti-Invites"),
        ("anti_mentions", "Anti-Mentions"),
    ]:
        val = automod.get(k, False)
        am_status.append(f"{label}: {'on' if val else 'off'}")

    fields.append(("Auto-moderation", " | ".join(am_status)))

    await _respond(
        inter,
        card("Uma Enjoyers HQ Settings", fields, "info", f"Uma Enjoyers HQ v{VERSION}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /automod
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="automod",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def automod_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@automod_group.sub_command(name="toggle", description="Toggle an auto-moderation module")
async def automod_toggle(
    inter: disnake.ApplicationCommandInteraction,
    module: str = commands.Param(
        choices=["anti_spam", "anti_raid", "anti_caps", "anti_links", "anti_invites", "anti_mentions"]
    ),
) -> None:
    assert inter.guild is not None
    automod = get_automod_cfg(inter.guild)
    current = automod.get(module, False)
    automod[module] = not current
    set_automod_cfg(inter.guild, automod)
    state = "enabled" if automod[module] else "disabled"
    labels = {
        "anti_spam": "Anti-Spam",
        "anti_raid": "Anti-Raid",
        "anti_caps": "Anti-Caps",
        "anti_links": "Anti-Links",
        "anti_invites": "Anti-Invites",
        "anti_mentions": "Anti-Mentions",
    }
    await _respond(
        inter,
        simple("Auto-Moderation", f"**{labels.get(module, module)}** -- {state}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@automod_group.sub_command(name="status", description="Auto-moderation status")
async def automod_status(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    automod = get_automod_cfg(inter.guild)
    fields: list[tuple[str, str]] = []
    for k, label in [
        ("anti_spam", "Anti-Spam"),
        ("anti_raid", "Anti-Raid"),
        ("anti_caps", "Anti-Caps"),
        ("anti_links", "Anti-Links"),
        ("anti_invites", "Anti-Invites"),
        ("anti_mentions", "Anti-Mentions"),
    ]:
        val = automod.get(k, False)
        fields.append((label, "Enabled" if val else "Disabled"))
    await _respond(inter, card("Auto-Moderation", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /mute, /unmute
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="mute",
    description="Timeout a member",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def mute_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Error", "You cannot mute this member (role hierarchy)", "error"),
            ephemeral=True,
        )
    if member.bot:
        return await _respond(
            inter,
            simple("Error", "Cannot mute a bot", "error"),
            ephemeral=True,
        )

    select_options = [
        SelectOption(label="60 seconds", value="60s"),
        SelectOption(label="5 minutes", value="5m"),
        SelectOption(label="10 minutes", value="10m"),
        SelectOption(label="30 minutes", value="30m"),
        SelectOption(label="1 hour", value="1h"),
        SelectOption(label="3 hours", value="3h"),
        SelectOption(label="6 hours", value="6h"),
        SelectOption(label="12 hours", value="12h"),
        SelectOption(label="1 day", value="1d"),
        SelectOption(label="3 days", value="3d"),
        SelectOption(label="7 days", value="7d"),
    ]

    warn_count = len(_load("warnings.json", {}).get(_guild_key(inter.guild), {}).get(_user_key(member), []))
    case_count = get_case_count(inter.guild, member)  # type: ignore[arg-type]

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### Timeout: {member.display_name}"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                f"**Member:** {member.mention} ({member.id})\n"
                f"**Reason:** {reason}\n"
                f"**Warnings:** {warn_count} | **Cases:** {case_count}"
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay("Select duration:"),
            disnake.ui.ActionRow(
                disnake.ui.StringSelect(
                    custom_id=f"mute_dur:{member.id}:{reason}",
                    placeholder="Timeout duration",
                    options=select_options,
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="28 days",
                    custom_id=f"mute_28d:{member.id}:{reason}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Custom time",
                    custom_id=f"mute_custom:{member.id}:{reason}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Cancel",
                    custom_id="mute_cancel",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["warn"]),
        )
    ]

    await _respond(inter, comps, ephemeral=True)


async def _apply_mute(
    inter: disnake.MessageInteraction | disnake.ModalInteraction,
    member_id: int,
    reason: str,
    duration: timedelta,
) -> None:
    guild = inter.guild
    if guild is None:
        return
    member = guild.get_member(member_id)
    if member is None:
        await _respond(inter, simple("Error", "Member not found on the server", "error"), ephemeral=True)
        return

    await member.timeout(duration=duration, reason=reason)
    cid = add_case(guild, member, "mute", inter.author, reason, fmt_dur(duration))

    c = card(
        "Timeout Applied",
        [
            ("Member", f"{member.mention} ({member.id})"),
            ("Duration", fmt_dur(duration)),
            ("Reason", reason),
            ("Moderator", inter.author.mention),
            ("Case", f"#{cid}"),
        ],
        "warn",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )

    await _respond(inter, c, ephemeral=True)
    await send_log(guild, c)
    await _try_dm(
        member,
        simple(
            f"Uma Enjoyers HQ -- {guild.name}",
            f"You have been timed out.\n\n"
            f"**Duration:** {fmt_dur(duration)}\n"
            f"**Reason:** {reason}\n"
            f"**Moderator:** {inter.author}",
            "warn",
            f"Case #{cid}",
        ),
    )


@bot.listen("on_string_select")
async def mute_dur_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("mute_dur:"):
        return
    parts = cid.split(":", 2)
    member_id = int(parts[1])
    reason = parts[2]
    dur = parse_dur(inter.values[0])
    if dur is None:
        return
    await _apply_mute(inter, member_id, reason, dur)


@bot.listen("on_button_click")
async def mute_buttons(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""

    if cid == "mute_cancel":
        await _respond(inter, simple("Cancelled", "Timeout cancelled", "plain"), ephemeral=True)
        return

    if cid.startswith("mute_28d:"):
        parts = cid.split(":", 2)
        member_id = int(parts[1])
        reason = parts[2]
        await _apply_mute(inter, member_id, reason, timedelta(days=28))
        return

    if cid.startswith("mute_custom:"):
        parts = cid.split(":", 2)
        member_id = parts[1]
        reason = parts[2]
        modal = disnake.ui.Modal(
            title="Custom Timeout Duration",
            custom_id=f"mute_modal:{member_id}:{reason}",
            components=[
                disnake.ui.TextInput(
                    label="Duration (25m / 2h / 7d / 90s)",
                    custom_id="duration",
                    placeholder="Example: 25m",
                    max_length=10,
                ),
            ],
        )
        await inter.response.send_modal(modal)
        return


@bot.listen("on_modal_submit")
async def mute_modal_submit(inter: disnake.ModalInteraction) -> None:
    cid = inter.custom_id
    if not cid.startswith("mute_modal:"):
        return
    parts = cid.split(":", 2)
    member_id = int(parts[1])
    reason = parts[2]
    raw = inter.text_values.get("duration", "")
    dur = parse_dur(raw)
    if dur is None:
        await _respond(
            inter,
            simple("Error", f"Invalid format: {raw}. Use: 25m / 2h / 7d / 90s", "error"),
            ephemeral=True,
        )
        return
    await _apply_mute(inter, member_id, reason, dur)


@bot.slash_command(
    name="unmute",
    description="Remove timeout",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def unmute_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    await member.timeout(duration=None, reason=f"Removed by moderator {inter.author}")
    c = card(
        "Timeout Removed",
        [
            ("Member", f"{member.mention} ({member.id})"),
            ("Moderator", inter.author.mention),
        ],
        "ok",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c, ephemeral=True)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /warn, /warnings, /clearwarns, /delwarn
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="warn",
    description="Issue a warning",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def warn_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Error", "Role hierarchy does not allow this", "error"),
            ephemeral=True,
        )
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    warnings.setdefault(gk, {}).setdefault(uk, [])
    warnings[gk][uk].append({
        "reason": reason,
        "mod": str(inter.author.id),
        "mod_name": str(inter.author),
        "ts": now_iso(),
    })
    flush()
    cid = add_case(inter.guild, member, "warn", inter.author, reason)  # type: ignore[arg-type]
    warn_count = len(warnings[gk][uk])

    c = card(
        "Warning",
        [
            ("Member", f"{member.mention} ({member.id})"),
            ("Reason", reason),
            ("Moderator", inter.author.mention),
            ("Case", f"#{cid}"),
            ("Total warnings", str(warn_count)),
        ],
        "warn",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Uma Enjoyers HQ -- {inter.guild.name}",  # type: ignore[union-attr]
            f"You have been warned.\n\n"
            f"**Reason:** {reason}\n"
            f"**Total warnings:** {warn_count}\n"
            f"**Moderator:** {inter.author}",
            "warn",
            f"Case #{cid}",
        ),
    )
    await _check_warn_threshold(inter.guild, member)  # type: ignore[arg-type]


@bot.slash_command(
    name="warnings",
    description="List member warnings",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def warnings_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    warnings = _load("warnings.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)
    warns = warnings.get(gk, {}).get(uk, [])
    if not warns:
        return await _respond(
            inter,
            simple("Warnings", f"{member.mention} has no warnings", "info"),
            ephemeral=True,
        )
    items: list[str] = []
    for i, w in enumerate(warns, 1):
        items.append(
            f"**#{i}** | {w['reason']} | <@{w['mod']}> | {w['ts'][:10]}"
        )
    await _respond(
        inter,
        list_card(
            f"Warnings -- {member.display_name}",
            items,
            "warn",
            f"Total: {len(warns)} warning(s)",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="clearwarns",
    description="Clear all warnings for a member",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def clearwarns_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    count = len(warnings.get(gk, {}).get(uk, []))
    if gk in warnings and uk in warnings[gk]:
        warnings[gk][uk] = []
        flush()
    await _respond(
        inter,
        simple(
            "Warnings Cleared",
            f"All warnings for {member.mention} have been removed ({count} total)",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Warnings Cleared",
            f"**Member:** {member.mention}\n**Removed:** {count}\n**Moderator:** {inter.author.mention}",
            "info",
        ),
    )


@bot.slash_command(
    name="delwarn",
    description="Delete a specific warning by number",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def delwarn_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    number: int = commands.Param(ge=1, description="Warning number"),
) -> None:
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    warns = warnings.get(gk, {}).get(uk, [])
    if number > len(warns) or number < 1:
        return await _respond(
            inter,
            simple("Error", f"Warning #{number} not found", "error"),
            ephemeral=True,
        )
    removed = warns.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple(
            "Warning Deleted",
            f"Deleted warning #{number} from {member.mention}\n**Reason was:** {removed['reason']}",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /kick
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="kick",
    description="Kick a member",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(kick_members=True),
)
async def kick_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Error", "Role hierarchy does not allow this", "error"),
            ephemeral=True,
        )
    cid = add_case(inter.guild, member, "kick", inter.author, reason)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Uma Enjoyers HQ -- {inter.guild.name}",  # type: ignore[union-attr]
            f"You have been kicked from the server.\n\n**Reason:** {reason}\n**Moderator:** {inter.author}",
            "error",
            f"Case #{cid}",
        ),
    )
    await member.kick(reason=reason)
    c = card(
        "Kick",
        [
            ("Member", f"{member} ({member.id})"),
            ("Reason", reason),
            ("Moderator", inter.author.mention),
            ("Case", f"#{cid}"),
        ],
        "error",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /softban
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="softban",
    description="Ban + unban (delete messages without permanent ban)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def softban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
    delete_days: int = commands.Param(default=1, ge=0, le=7, description="Days of messages to delete"),
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Error", "Role hierarchy does not allow this", "error"),
            ephemeral=True,
        )
    cid = add_case(inter.guild, member, "softban", inter.author, reason)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Uma Enjoyers HQ -- {inter.guild.name}",  # type: ignore[union-attr]
            f"You have been softbanned (kick with message deletion).\n\n**Reason:** {reason}",
            "error",
            f"Case #{cid}",
        ),
    )
    await inter.guild.ban(member, reason=f"Softban: {reason}", delete_message_seconds=delete_days * 86400)  # type: ignore[union-attr]
    await inter.guild.unban(member, reason="Softban unban")  # type: ignore[union-attr]
    c = card(
        "Softban",
        [
            ("Member", f"{member} ({member.id})"),
            ("Reason", reason),
            ("Messages deleted for", f"{delete_days} day(s)"),
            ("Moderator", inter.author.mention),
            ("Case", f"#{cid}"),
        ],
        "error",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /ban, /unban, /massban + tempban loop
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="ban",
    description="Ban a member",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def ban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
    duration: str | None = None,
    delete_messages: int = commands.Param(default=0, ge=0, le=7, description="Days of messages to delete"),
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Error", "Role hierarchy does not allow this", "error"),
            ephemeral=True,
        )

    dur_td: timedelta | None = None
    dur_str: str | None = None
    if duration:
        dur_td = parse_dur(duration)
        if dur_td is None:
            return await _respond(
                inter,
                simple("Error", f"Invalid duration format: {duration}", "error"),
                ephemeral=True,
            )
        dur_str = fmt_dur(dur_td)

    cid = add_case(inter.guild, member, "ban", inter.author, reason, dur_str)  # type: ignore[arg-type]

    dm_text = f"You have been banned from the server.\n\n**Reason:** {reason}"
    if dur_str:
        dm_text += f"\n**Duration:** {dur_str}"
    dm_text += f"\n**Moderator:** {inter.author}"

    await _try_dm(
        member,
        simple(f"Uma Enjoyers HQ -- {inter.guild.name}", dm_text, "error", f"Case #{cid}"),  # type: ignore[union-attr]
    )

    delete_seconds = min(delete_messages * 86400, 604800)
    await inter.guild.ban(member, reason=reason, delete_message_seconds=delete_seconds)  # type: ignore[union-attr]

    if dur_td is not None:
        tempbans, flush = save("tempbans")
        gk = _guild_key(inter.guild)
        tempbans.setdefault(gk, {})[str(member.id)] = {
            "expires": now_ts() + int(dur_td.total_seconds()),
            "reason": reason,
            "mod": str(inter.author.id),
        }
        flush()

    fields = [
        ("Member", f"{member} ({member.id})"),
        ("Reason", reason),
        ("Moderator", inter.author.mention),
        ("Case", f"#{cid}"),
    ]
    if dur_str:
        fields.insert(2, ("Duration", dur_str))

    c = card("Ban", fields, "error", f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}")
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="unban",
    description="Unban a user by ID",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def unban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    user_id: str,
    reason: str = "Not specified",
) -> None:
    try:
        user = await bot.fetch_user(int(user_id))
    except (ValueError, disnake.NotFound):
        return await _respond(
            inter,
            simple("Error", "User not found", "error"),
            ephemeral=True,
        )

    try:
        await inter.guild.unban(user, reason=reason)  # type: ignore[union-attr]
    except disnake.NotFound:
        return await _respond(
            inter,
            simple("Error", "User not found in ban list", "error"),
            ephemeral=True,
        )

    tempbans, flush = save("tempbans")
    gk = _guild_key(inter.guild)
    if gk in tempbans and user_id in tempbans[gk]:
        del tempbans[gk][user_id]
        flush()

    c = card(
        "Unban",
        [
            ("User", f"{user} ({user.id})"),
            ("Reason", reason),
            ("Moderator", inter.author.mention),
        ],
        "ok",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="massban",
    description="Ban multiple users by ID (space-separated)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def massban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    user_ids: str = commands.Param(description="IDs separated by spaces"),
    reason: str = "Mass ban",
) -> None:
    await inter.response.defer(ephemeral=True)
    ids = user_ids.split()
    banned: list[str] = []
    failed: list[str] = []
    for uid_str in ids:
        try:
            uid = int(uid_str)
            user = await bot.fetch_user(uid)
            await inter.guild.ban(user, reason=reason)  # type: ignore[union-attr]
            add_case(inter.guild, user, "massban", inter.author, reason)  # type: ignore[arg-type]
            banned.append(f"{user} ({uid})")
        except Exception:
            failed.append(uid_str)

    text = f"**Banned:** {len(banned)}"
    if failed:
        text += f"\n**Failed:** {', '.join(failed)}"
    await inter.followup.send(
        components=simple("Mass Ban", text, "error" if failed else "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Mass Ban",
            f"**Banned:** {len(banned)}\n**Moderator:** {inter.author.mention}\n**Reason:** {reason}",
            "error",
        ),
    )


@tasks.loop(seconds=20)
async def tempban_check() -> None:
    tempbans, flush = save("tempbans")
    now = now_ts()
    changed = False
    for gk in list(tempbans):
        guild = bot.get_guild(int(gk))
        if guild is None:
            continue
        for uid in list(tempbans[gk]):
            entry = tempbans[gk][uid]
            if now >= entry["expires"]:
                try:
                    user = await bot.fetch_user(int(uid))
                    await guild.unban(user, reason="Temporary ban expired")
                    await send_log(
                        guild,
                        card(
                            "Auto-Unban",
                            [
                                ("User", f"{user} ({user.id})"),
                                ("Ban reason", entry.get("reason", "N/A")),
                            ],
                            "ok",
                            "Temporary ban expired",
                        ),
                    )
                except Exception:
                    pass
                del tempbans[gk][uid]
                changed = True
    if changed:
        flush()


@tempban_check.before_loop
async def before_tempban_check() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
#  /purge
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="purge",
    description="Delete messages",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def purge_cmd(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=100),
    member: disnake.Member | None = None,
    contains: str | None = None,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)

    def check(m: disnake.Message) -> bool:
        if member is not None and m.author.id != member.id:
            return False
        if contains is not None and contains.lower() not in m.content.lower():
            return False
        return True

    deleted = await inter.channel.purge(limit=amount, check=check)
    c = simple(
        "Purge",
        f"**Channel:** {inter.channel.mention}\n"
        f"**Deleted:** {len(deleted)} message(s)\n"
        f"**Moderator:** {inter.author.mention}"
        + (f"\n**Member filter:** {member.mention}" if member else "")
        + (f"\n**Text filter:** {contains}" if contains else ""),
        "ok",
        "Uma Enjoyers HQ",
    )
    await inter.followup.send(
        components=c,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /slowmode
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="slowmode",
    description="Slowmode",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def slowmode_cmd(
    inter: disnake.ApplicationCommandInteraction,
    seconds: int = commands.Param(ge=0, le=21600),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.channel.edit(slowmode_delay=seconds)
    if seconds:
        text = f"Slowmode set to {seconds}s in {inter.channel.mention}"
    else:
        text = f"Slowmode disabled in {inter.channel.mention}"
    await _respond(inter, simple("Slowmode", text, "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /lock, /unlock
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="lock",
    description="Lock a channel",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def lock_cmd(
    inter: disnake.ApplicationCommandInteraction,
    reason: str = "Not specified",
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = False
    await inter.channel.set_permissions(
        inter.guild.default_role, overwrite=overwrite, reason=reason  # type: ignore[union-attr]
    )
    c = card(
        "Channel Locked",
        [
            ("Channel", inter.channel.mention),
            ("Reason", reason),
            ("Moderator", inter.author.mention),
        ],
        "warn",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="unlock",
    description="Unlock a channel",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def unlock_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = None
    await inter.channel.set_permissions(inter.guild.default_role, overwrite=overwrite)  # type: ignore[union-attr]
    c = card(
        "Channel Unlocked",
        [
            ("Channel", inter.channel.mention),
            ("Moderator", inter.author.mention),
        ],
        "ok",
        f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /nuke
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="nuke",
    description="Recreate a channel (delete all messages)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def nuke_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    assert inter.guild is not None
    channel = inter.channel
    position = channel.position
    category = channel.category
    overwrites = channel.overwrites
    topic = channel.topic
    slowmode = channel.slowmode_delay
    nsfw = channel.is_nsfw()
    name = channel.name

    await _respond(
        inter,
        simple("Nuke", "Channel will be recreated in 2 seconds...", "error", "Uma Enjoyers HQ"),
    )

    await send_log(
        inter.guild,
        simple(
            "Nuke",
            f"**Channel:** #{name}\n**Moderator:** {inter.author.mention}",
            "error",
        ),
    )

    await asyncio.sleep(2)
    await channel.delete(reason=f"Nuke by {inter.author}")

    new_channel = await inter.guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites,
        topic=topic,
        slowmode_delay=slowmode,
        nsfw=nsfw,
        position=position,
        reason=f"Nuke by {inter.author}",
    )
    await new_channel.send(
        components=simple("Nuke", "Channel has been recreated", "error", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /userinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="userinfo",
    description="User information",
    contexts=GUILD_ONLY,
)
async def userinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member | None = None,
) -> None:
    member = member or inter.author  # type: ignore[assignment]
    assert isinstance(member, disnake.Member)
    assert inter.guild is not None

    warnings = _load("warnings.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)
    warn_count = len(warnings.get(gk, {}).get(uk, []))
    case_count = get_case_count(inter.guild, member)

    created = disnake.utils.format_dt(member.created_at, "F")
    created_r = disnake.utils.format_dt(member.created_at, "R")
    joined = disnake.utils.format_dt(member.joined_at, "F") if member.joined_at else "N/A"
    joined_r = disnake.utils.format_dt(member.joined_at, "R") if member.joined_at else ""

    roles = ", ".join(r.mention for r in reversed(member.roles[1:])) if len(member.roles) > 1 else "None"
    role_count = len(member.roles) - 1

    timeout_text = "None"
    if member.current_timeout and member.current_timeout > datetime.now(timezone.utc):
        timeout_text = f"Until {disnake.utils.format_dt(member.current_timeout, 'F')}"

    boosting = "None"
    if member.premium_since:
        boosting = f"Since {disnake.utils.format_dt(member.premium_since, 'F')}"

    perms_list: list[str] = []
    key_perms = [
        ("administrator", "Administrator"),
        ("manage_guild", "Manage Server"),
        ("manage_channels", "Manage Channels"),
        ("manage_roles", "Manage Roles"),
        ("manage_messages", "Manage Messages"),
        ("kick_members", "Kick Members"),
        ("ban_members", "Ban Members"),
        ("moderate_members", "Moderate Members"),
        ("mention_everyone", "Mention Everyone"),
    ]
    for perm, label in key_perms:
        if getattr(member.guild_permissions, perm, False):
            perms_list.append(label)

    text_lines = [
        f"**Nickname:** {member.display_name}",
        f"**Tag:** {member}",
        f"**ID:** {member.id}",
        f"**Bot:** {'Yes' if member.bot else 'No'}",
        "",
        f"**Account created:** {created} ({created_r})",
        f"**Joined:** {joined} ({joined_r})",
        f"**Boosting:** {boosting}",
        "",
        f"**Timeout:** {timeout_text}",
        f"**Warnings:** {warn_count} | **Cases:** {case_count}",
        "",
        f"**Roles ({role_count}):** {roles}",
    ]

    if perms_list:
        text_lines.append("")
        text_lines.append(f"**Key permissions:** {', '.join(perms_list)}")

    avatar_url = member.display_avatar.url

    await _respond(
        inter,
        profile_card(
            f"Info about {member.display_name}",
            text_lines,
            avatar_url,
            "info",
            f"Uma Enjoyers HQ v{VERSION}",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /avatar
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="avatar",
    description="Show user avatar",
    contexts=GUILD_ONLY,
)
async def avatar_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member | None = None,
) -> None:
    member = member or inter.author  # type: ignore[assignment]
    assert isinstance(member, disnake.Member)
    avatar_url = member.display_avatar.with_size(1024).url
    guild_avatar = member.guild_avatar
    global_avatar = member.avatar

    lines: list[str] = [f"**{member.display_name}** ({member.id})"]
    if global_avatar:
        lines.append(f"[Global avatar]({global_avatar.with_size(1024).url})")
    if guild_avatar:
        lines.append(f"[Server avatar]({guild_avatar.with_size(1024).url})")

    await _respond(
        inter,
        profile_card("Avatar", lines, avatar_url, "info"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /banner
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="banner",
    description="Show user banner",
    contexts=GUILD_ONLY,
)
async def banner_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member | None = None,
) -> None:
    member = member or inter.author  # type: ignore[assignment]
    assert isinstance(member, disnake.Member)
    user = await bot.fetch_user(member.id)
    if user.banner:
        banner_url = user.banner.with_size(1024).url
        await _respond(
            inter,
            profile_card("Banner", [f"**{member.display_name}**"], banner_url, "info"),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            simple("Banner", f"{member.mention} has no banner", "info"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /serverinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="serverinfo",
    description="Server information",
    contexts=GUILD_ONLY,
)
async def serverinfo_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    guild = inter.guild
    assert guild is not None

    total = guild.member_count or 0
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots

    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    categories = len(guild.categories)
    threads = len(guild.threads)

    roles = len(guild.roles) - 1
    emojis = len(guild.emojis)
    stickers = len(guild.stickers)

    created = disnake.utils.format_dt(guild.created_at, "F")
    created_r = disnake.utils.format_dt(guild.created_at, "R")

    boost_level = guild.premium_tier
    boost_count = guild.premium_subscription_count or 0

    verification = str(guild.verification_level).replace("VerificationLevel.", "").title()

    owner = guild.owner

    total_cases = get_total_cases(guild)

    fields: list[tuple[str, str]] = [
        ("Owner", f"{owner.mention}" if owner else "N/A"),
        ("ID", str(guild.id)),
        ("Created", f"{created} ({created_r})"),
        ("", ""),
        ("Members", f"Total: {total} | Humans: {humans} | Bots: {bots}"),
        ("Channels", f"Text: {text_channels} | Voice: {voice_channels} | Categories: {categories} | Threads: {threads}"),
        ("Roles", str(roles)),
        ("Emojis / Stickers", f"{emojis} / {stickers}"),
        ("", ""),
        ("Boost", f"Level {boost_level} | {boost_count} boost(s)"),
        ("Verification", verification),
        ("Moderation cases", str(total_cases)),
    ]

    icon_url = guild.icon.url if guild.icon else ""

    if icon_url:
        await _respond(
            inter,
            profile_card(
                guild.name,
                [f"**{k}:** {v}" if k else "" for k, v in fields],
                icon_url,
                "info",
                f"Uma Enjoyers HQ v{VERSION}",
            ),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            card(guild.name, [(k, v) for k, v in fields if k], "info", f"Uma Enjoyers HQ v{VERSION}"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /roleinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="roleinfo",
    description="Role information",
    contexts=GUILD_ONLY,
)
async def roleinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    created = disnake.utils.format_dt(role.created_at, "F")
    colour = f"#{role.colour.value:06x}" if role.colour.value else "Default"
    mentionable = "Yes" if role.mentionable else "No"
    hoisted = "Yes" if role.hoist else "No"
    managed = "Yes" if role.managed else "No"
    position = role.position
    member_count = len(role.members)

    key_perms: list[str] = []
    important_perms = [
        "administrator", "manage_guild", "manage_channels", "manage_roles",
        "manage_messages", "kick_members", "ban_members", "moderate_members",
    ]
    for p in important_perms:
        if getattr(role.permissions, p, False):
            key_perms.append(p)

    fields = [
        ("ID", str(role.id)),
        ("Color", colour),
        ("Members", str(member_count)),
        ("Position", str(position)),
        ("Created", created),
        ("Mentionable", mentionable),
        ("Hoisted", hoisted),
        ("Managed", managed),
    ]
    if key_perms:
        fields.append(("Key permissions", ", ".join(key_perms)))

    await _respond(
        inter,
        card(f"Role: {role.name}", fields, "info", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /channelinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="channelinfo",
    description="Channel information",
    contexts=GUILD_ONLY,
)
async def channelinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    created = disnake.utils.format_dt(ch.created_at, "F")
    fields = [
        ("ID", str(ch.id)),
        ("Category", ch.category.name if ch.category else "None"),
        ("Position", str(ch.position)),
        ("Created", created),
        ("Topic", ch.topic or "None"),
        ("NSFW", "Yes" if ch.is_nsfw() else "No"),
        ("Slowmode", f"{ch.slowmode_delay}s" if ch.slowmode_delay else "Off"),
    ]
    await _respond(
        inter,
        card(f"Channel: #{ch.name}", fields, "info", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /history
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="history",
    description="Member case history",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def history_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    cases = _load("cases.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)
    user_cases = cases.get(gk, {}).get(uk, [])
    if not user_cases:
        return await _respond(
            inter,
            simple("History", f"{member.mention} has no moderation cases", "info"),
            ephemeral=True,
        )

    last_10 = user_cases[-10:]
    items: list[str] = []
    for c in last_10:
        dur_part = f" | {c['duration']}" if c.get("duration") else ""
        items.append(
            f"**#{c['id']}** {c['action'].upper()} | {c['reason']} | <@{c['mod']}> | {c['ts'][:10]}{dur_part}"
        )
    await _respond(
        inter,
        list_card(
            f"History -- {member.display_name}",
            items,
            "info",
            f"Total cases: {len(user_cases)} | Showing last {len(last_10)}",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /note (moderator notes on users)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="note",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def note_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@note_group.sub_command(name="add", description="Add a note about a user")
async def note_add(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    text: str,
) -> None:
    notes, flush = save("notes")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    notes.setdefault(gk, {}).setdefault(uk, [])
    notes[gk][uk].append({
        "text": text,
        "mod": str(inter.author.id),
        "mod_name": str(inter.author),
        "ts": now_iso(),
    })
    flush()
    count = len(notes[gk][uk])
    await _respond(
        inter,
        simple(
            "Note Added",
            f"Note about {member.mention} saved (total: {count})",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@note_group.sub_command(name="list", description="List notes about a user")
async def note_list(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    notes = _load("notes.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)
    user_notes = notes.get(gk, {}).get(uk, [])
    if not user_notes:
        return await _respond(
            inter,
            simple("Notes", f"{member.mention} has no notes", "info"),
            ephemeral=True,
        )
    items: list[str] = []
    for i, n in enumerate(user_notes, 1):
        items.append(f"**#{i}** | {n['text']} | {n.get('mod_name', 'N/A')} | {n['ts'][:10]}")
    await _respond(
        inter,
        list_card(
            f"Notes -- {member.display_name}",
            items,
            "info",
            f"Total: {len(user_notes)}",
        ),
        ephemeral=True,
    )


@note_group.sub_command(name="clear", description="Clear all notes about a user")
async def note_clear(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    notes, flush = save("notes")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    count = len(notes.get(gk, {}).get(uk, []))
    if gk in notes and uk in notes[gk]:
        notes[gk][uk] = []
        flush()
    await _respond(
        inter,
        simple("Notes Cleared", f"Removed {count} notes about {member.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@note_group.sub_command(name="delete", description="Delete a specific note by number")
async def note_delete(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    number: int = commands.Param(ge=1),
) -> None:
    notes, flush = save("notes")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    user_notes = notes.get(gk, {}).get(uk, [])
    if number > len(user_notes) or number < 1:
        return await _respond(
            inter, simple("Error", f"Note #{number} not found", "error"), ephemeral=True
        )
    removed = user_notes.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple("Note Deleted", f"Deleted note #{number}: {truncate(removed['text'], 100)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /role (management)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="role",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_roles=True),
)
async def role_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@role_group.sub_command(name="add", description="Add a role to a member")
async def role_add(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    role: disnake.Role,
) -> None:
    if role >= inter.author.top_role and inter.author.id != inter.guild.owner_id:  # type: ignore[union-attr]
        return await _respond(
            inter, simple("Error", "You cannot assign a role higher than or equal to yours", "error"), ephemeral=True
        )
    await member.add_roles(role, reason=f"Assigned by {inter.author}")
    await _respond(
        inter,
        simple("Role Added", f"{role.mention} added to {member.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Role Added", f"**Member:** {member.mention}\n**Role:** {role.mention}\n**Moderator:** {inter.author.mention}", "info"),
    )


@role_group.sub_command(name="remove", description="Remove a role from a member")
async def role_remove(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    role: disnake.Role,
) -> None:
    if role >= inter.author.top_role and inter.author.id != inter.guild.owner_id:  # type: ignore[union-attr]
        return await _respond(
            inter, simple("Error", "You cannot remove a role higher than or equal to yours", "error"), ephemeral=True
        )
    await member.remove_roles(role, reason=f"Removed by {inter.author}")
    await _respond(
        inter,
        simple("Role Removed", f"{role.mention} removed from {member.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Role Removed", f"**Member:** {member.mention}\n**Role:** {role.mention}\n**Moderator:** {inter.author.mention}", "info"),
    )


@role_group.sub_command(name="members", description="List members with a role")
async def role_members(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    members = role.members
    if not members:
        return await _respond(
            inter, simple("Role", f"{role.mention} has no members", "info"), ephemeral=True
        )
    member_list = [f"{m.mention} ({m.id})" for m in members[:25]]
    footer = f"Showing {len(member_list)} of {len(members)}" if len(members) > 25 else f"Total: {len(members)}"
    await _respond(
        inter,
        list_card(f"Members with role {role.name}", member_list, "info", footer),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /reactionrole
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="reactionrole",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def rr_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@rr_group.sub_command(name="create", description="Create a reaction role panel")
async def rr_create(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    title: str = "Choose roles",
    description: str = "Click a button to get or remove a role.",
) -> None:
    assert inter.guild is not None
    rr_data, flush = save("reactionroles")
    gk = _guild_key(inter.guild)
    panel_id = _gen_id()

    rr_data.setdefault(gk, {})[panel_id] = {
        "channel_id": str(channel.id),
        "message_id": None,
        "title": title,
        "description": description,
        "roles": [],
    }
    flush()

    await _respond(
        inter,
        simple(
            "Panel Created",
            f"Panel ID: `{panel_id}`\n"
            f"Channel: {channel.mention}\n\n"
            f"Use `/reactionrole addrole {panel_id} <role> <button text>` to add roles.\n"
            f"Then `/reactionrole send {panel_id}` to send the panel.",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@rr_group.sub_command(name="addrole", description="Add a role to a panel")
async def rr_addrole(
    inter: disnake.ApplicationCommandInteraction,
    panel_id: str,
    role: disnake.Role,
    label: str,
) -> None:
    assert inter.guild is not None
    rr_data, flush = save("reactionroles")
    gk = _guild_key(inter.guild)
    panel = rr_data.get(gk, {}).get(panel_id)
    if not panel:
        return await _respond(inter, simple("Error", f"Panel `{panel_id}` not found", "error"), ephemeral=True)
    if len(panel["roles"]) >= 20:
        return await _respond(inter, simple("Error", "Maximum 20 roles per panel", "error"), ephemeral=True)
    panel["roles"].append({"role_id": str(role.id), "label": label})
    flush()
    await _respond(
        inter,
        simple(
            "Role Added",
            f"**{label}** ({role.mention}) added to panel `{panel_id}`\nTotal roles: {len(panel['roles'])}",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@rr_group.sub_command(name="send", description="Send a reaction role panel")
async def rr_send(
    inter: disnake.ApplicationCommandInteraction,
    panel_id: str,
) -> None:
    assert inter.guild is not None
    rr_data, flush = save("reactionroles")
    gk = _guild_key(inter.guild)
    panel = rr_data.get(gk, {}).get(panel_id)
    if not panel:
        return await _respond(inter, simple("Error", f"Panel `{panel_id}` not found", "error"), ephemeral=True)
    if not panel["roles"]:
        return await _respond(inter, simple("Error", "Panel has no roles", "error"), ephemeral=True)

    channel = inter.guild.get_channel(int(panel["channel_id"]))
    if not channel or not isinstance(channel, disnake.TextChannel):
        return await _respond(inter, simple("Error", "Channel not found", "error"), ephemeral=True)

    children: list[Any] = [
        disnake.ui.TextDisplay(f"### {panel['title']}"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
        disnake.ui.TextDisplay(panel["description"]),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
    ]

    rows: list[list[Any]] = [[]]
    for role_entry in panel["roles"]:
        if len(rows[-1]) >= 5:
            rows.append([])
        rows[-1].append(
            disnake.ui.Button(
                label=role_entry["label"],
                custom_id=f"rr:{panel_id}:{role_entry['role_id']}",
                style=ButtonStyle.secondary,
            )
        )

    for row in rows:
        if row:
            children.append(disnake.ui.ActionRow(*row))

    comps = [disnake.ui.Container(*children, accent_colour=Color(COLORS["info"]))]
    msg = await channel.send(components=comps, flags=MessageFlags(is_components_v2=True))
    panel["message_id"] = str(msg.id)
    flush()

    await _respond(
        inter,
        simple("Panel Sent", f"Role panel sent to {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def rr_button_click(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("rr:"):
        return
    parts = cid.split(":", 2)
    if len(parts) < 3:
        return
    role_id = int(parts[2])
    guild = inter.guild
    if guild is None:
        return
    role = guild.get_role(role_id)
    if role is None:
        return await _respond(inter, simple("Error", "Role not found", "error"), ephemeral=True)
    member = inter.author
    assert isinstance(member, disnake.Member)
    if role in member.roles:
        await member.remove_roles(role)
        await _respond(
            inter,
            simple("Role Removed", f"Role {role.mention} removed", "ok"),
            ephemeral=True,
        )
    else:
        await member.add_roles(role)
        await _respond(
            inter,
            simple("Role Received", f"Role {role.mention} assigned", "ok"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /afk
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="afk",
    description="Set AFK status",
    contexts=GUILD_ONLY,
)
async def afk_cmd(
    inter: disnake.ApplicationCommandInteraction,
    message: str = "AFK",
) -> None:
    assert inter.guild is not None
    key = f"{inter.guild.id}:{inter.author.id}"
    _afk_users[key] = {"message": message, "ts": now_iso()}
    await _respond(
        inter,
        simple("AFK", f"{inter.author.mention} is now AFK: {message}", "info", "Uma Enjoyers HQ"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /remind
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="remind",
    description="Set a reminder",
    contexts=GUILD_ONLY,
)
async def remind_cmd(
    inter: disnake.ApplicationCommandInteraction,
    time_str: str = commands.Param(name="time", description="Duration (10m / 2h / 1d)"),
    text: str = "Reminder",
) -> None:
    dur = parse_dur(time_str)
    if dur is None:
        return await _respond(
            inter,
            simple("Error", "Invalid time format. Use: 10m / 2h / 1d", "error"),
            ephemeral=True,
        )
    reminders, flush = save("reminders")
    gk = _guild_key(inter.guild)
    uk = _user_key(inter.author)
    reminders.setdefault(gk, {}).setdefault(uk, [])
    reminders[gk][uk].append({
        "text": text,
        "channel_id": str(inter.channel.id),
        "expires": now_ts() + int(dur.total_seconds()),
        "ts": now_iso(),
    })
    flush()
    await _respond(
        inter,
        simple(
            "Reminder",
            f"Reminding in {fmt_dur(dur)}: {text}",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@tasks.loop(seconds=15)
async def reminder_check() -> None:
    reminders, flush = save("reminders")
    now = now_ts()
    changed = False
    for gk in list(reminders):
        guild = bot.get_guild(int(gk))
        if guild is None:
            continue
        for uk in list(reminders[gk]):
            user_reminders = reminders[gk][uk]
            remaining: list[dict] = []
            for r in user_reminders:
                if now >= r["expires"]:
                    ch = guild.get_channel(int(r["channel_id"]))
                    if ch and isinstance(ch, disnake.TextChannel):
                        try:
                            await ch.send(
                                components=simple(
                                    "Reminder",
                                    f"<@{uk}>, you asked to be reminded:\n\n**{r['text']}**",
                                    "info",
                                    f"Set: {r['ts'][:16]}",
                                ),
                                flags=MessageFlags(is_components_v2=True),
                            )
                        except Exception:
                            pass
                    changed = True
                else:
                    remaining.append(r)
            reminders[gk][uk] = remaining
    if changed:
        flush()


@reminder_check.before_loop
async def before_reminder_check() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
#  /giveaway
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="giveaway",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def giveaway_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@giveaway_group.sub_command(name="start", description="Start a giveaway")
async def giveaway_start(
    inter: disnake.ApplicationCommandInteraction,
    prize: str,
    duration: str = commands.Param(description="Duration (1h / 1d / 7d)"),
    winners: int = commands.Param(default=1, ge=1, le=20, description="Number of winners"),
    channel: disnake.TextChannel | None = None,
) -> None:
    assert inter.guild is not None
    dur = parse_dur(duration)
    if dur is None:
        return await _respond(inter, simple("Error", "Invalid duration format", "error"), ephemeral=True)

    target_channel = channel or inter.channel
    assert isinstance(target_channel, disnake.TextChannel)

    ga_id = _gen_id()
    ends_at = now_ts() + int(dur.total_seconds())

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### Giveaway"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                f"**Prize:** {prize}\n"
                f"**Winners:** {winners}\n"
                f"**Ends:** {disnake.utils.format_dt(datetime.fromtimestamp(ends_at, tz=timezone.utc), 'F')} "
                f"({disnake.utils.format_dt(datetime.fromtimestamp(ends_at, tz=timezone.utc), 'R')})\n"
                f"**Host:** {inter.author.mention}"
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Enter",
                    custom_id=f"giveaway_join:{ga_id}",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["info"]),
        )
    ]

    msg = await target_channel.send(components=comps, flags=MessageFlags(is_components_v2=True))

    giveaways, flush = save("giveaways")
    gk = _guild_key(inter.guild)
    giveaways.setdefault(gk, {})[ga_id] = {
        "prize": prize,
        "winners": winners,
        "ends_at": ends_at,
        "channel_id": str(target_channel.id),
        "message_id": str(msg.id),
        "host": str(inter.author.id),
        "participants": [],
        "ended": False,
    }
    flush()

    await _respond(
        inter,
        simple("Giveaway", f"Giveaway `{ga_id}` created in {target_channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def giveaway_join_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("giveaway_join:"):
        return
    ga_id = cid.split(":", 1)[1]
    guild = inter.guild
    if guild is None:
        return

    giveaways, flush = save("giveaways")
    gk = _guild_key(guild)
    ga = giveaways.get(gk, {}).get(ga_id)
    if not ga or ga.get("ended"):
        return await _respond(inter, simple("Giveaway", "Giveaway has ended", "error"), ephemeral=True)

    uid = str(inter.author.id)
    if uid in ga["participants"]:
        ga["participants"].remove(uid)
        flush()
        return await _respond(
            inter,
            simple("Giveaway", f"You left the giveaway. Participants: {len(ga['participants'])}", "info"),
            ephemeral=True,
        )
    ga["participants"].append(uid)
    flush()
    await _respond(
        inter,
        simple("Giveaway", f"You have entered! Participants: {len(ga['participants'])}", "ok"),
        ephemeral=True,
    )


@giveaway_group.sub_command(name="end", description="End a giveaway early")
async def giveaway_end(
    inter: disnake.ApplicationCommandInteraction,
    giveaway_id: str,
) -> None:
    assert inter.guild is not None
    giveaways, flush = save("giveaways")
    gk = _guild_key(inter.guild)
    ga = giveaways.get(gk, {}).get(giveaway_id)
    if not ga:
        return await _respond(inter, simple("Error", "Giveaway not found", "error"), ephemeral=True)
    await _end_giveaway(inter.guild, giveaway_id, ga, giveaways, flush)
    await _respond(
        inter,
        simple("Giveaway", f"Giveaway `{giveaway_id}` ended", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@giveaway_group.sub_command(name="reroll", description="Reroll winners")
async def giveaway_reroll(
    inter: disnake.ApplicationCommandInteraction,
    giveaway_id: str,
) -> None:
    assert inter.guild is not None
    giveaways, flush = save("giveaways")
    gk = _guild_key(inter.guild)
    ga = giveaways.get(gk, {}).get(giveaway_id)
    if not ga:
        return await _respond(inter, simple("Error", "Giveaway not found", "error"), ephemeral=True)
    if not ga["participants"]:
        return await _respond(inter, simple("Error", "No participants", "error"), ephemeral=True)

    winner_count = min(ga["winners"], len(ga["participants"]))
    winner_ids = random.sample(ga["participants"], winner_count)
    winners_text = ", ".join(f"<@{w}>" for w in winner_ids)

    channel = inter.guild.get_channel(int(ga["channel_id"]))
    if channel and isinstance(channel, disnake.TextChannel):
        await channel.send(
            components=simple(
                "Giveaway Reroll",
                f"**Prize:** {ga['prize']}\n**New winners:** {winners_text}",
                "ok",
                "Uma Enjoyers HQ",
            ),
            flags=MessageFlags(is_components_v2=True),
        )
    await _respond(inter, simple("Reroll", f"New winners: {winners_text}", "ok", "Uma Enjoyers HQ"), ephemeral=True)


async def _end_giveaway(
    guild: disnake.Guild,
    ga_id: str,
    ga: dict,
    giveaways: dict,
    flush: Any,
) -> None:
    ga["ended"] = True
    flush()

    channel = guild.get_channel(int(ga["channel_id"]))
    if not channel or not isinstance(channel, disnake.TextChannel):
        return

    if not ga["participants"]:
        await channel.send(
            components=simple("Giveaway Ended", f"**Prize:** {ga['prize']}\nNo participants", "warn", "Uma Enjoyers HQ"),
            flags=MessageFlags(is_components_v2=True),
        )
        return

    winner_count = min(ga["winners"], len(ga["participants"]))
    winner_ids = random.sample(ga["participants"], winner_count)
    winners_text = ", ".join(f"<@{w}>" for w in winner_ids)

    await channel.send(
        components=card(
            "Giveaway Ended",
            [
                ("Prize", ga["prize"]),
                ("Winners", winners_text),
                ("Participants", str(len(ga["participants"]))),
            ],
            "ok",
            "Uma Enjoyers HQ",
        ),
        flags=MessageFlags(is_components_v2=True),
    )


@tasks.loop(seconds=30)
async def giveaway_check() -> None:
    giveaways, flush = save("giveaways")
    now = now_ts()
    for gk in list(giveaways):
        guild = bot.get_guild(int(gk))
        if guild is None:
            continue
        for ga_id in list(giveaways[gk]):
            ga = giveaways[gk][ga_id]
            if ga.get("ended"):
                continue
            if now >= ga["ends_at"]:
                await _end_giveaway(guild, ga_id, ga, giveaways, flush)


@giveaway_check.before_loop
async def before_giveaway_check() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
#  /stats
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="stats",
    description="Server moderation statistics",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def stats_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    cases = _load("cases.json", {})
    warnings = _load("warnings.json", {})
    gk = _guild_key(inter.guild)

    guild_cases = cases.get(gk, {})
    total_cases = sum(len(v) for v in guild_cases.values())
    unique_users = len(guild_cases)

    action_counts: dict[str, int] = defaultdict(int)
    for user_cases in guild_cases.values():
        for c in user_cases:
            action_counts[c["action"]] += 1

    guild_warnings = warnings.get(gk, {})
    total_warnings = sum(len(v) for v in guild_warnings.values())

    fields: list[tuple[str, str]] = [
        ("Total cases", str(total_cases)),
        ("Unique offenders", str(unique_users)),
        ("Total warnings", str(total_warnings)),
    ]

    if action_counts:
        breakdown = " | ".join(f"{k}: {v}" for k, v in sorted(action_counts.items(), key=lambda x: -x[1]))
        fields.append(("By type", breakdown))

    top_offenders: list[tuple[str, int]] = []
    for uid, user_cases in guild_cases.items():
        top_offenders.append((uid, len(user_cases)))
    top_offenders.sort(key=lambda x: -x[1])

    if top_offenders:
        top_text = "\n".join(
            f"<@{uid}> -- {count} cases"
            for uid, count in top_offenders[:5]
        )
        fields.append(("Top offenders", top_text))

    await _respond(
        inter,
        card(f"Statistics -- {inter.guild.name}", fields, "info", f"Uma Enjoyers HQ v{VERSION}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /haven (bot info)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="haven",
    description="Bot information",
    contexts=GUILD_ONLY,
)
async def haven_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    guild_count = len(bot.guilds)
    total_members = sum(g.member_count or 0 for g in bot.guilds)
    uptime = datetime.now(timezone.utc) - _bot_start_time

    fields: list[tuple[str, str]] = [
        ("Version", VERSION),
        ("Library", f"disnake {disnake.__version__}"),
        ("Servers", str(guild_count)),
        ("Members (total)", str(total_members)),
        ("Uptime", fmt_dur(uptime)),
        ("Ping", f"{round(bot.latency * 1000)}ms"),
        ("Commands", str(len(list(bot.all_slash_commands)))),
    ]

    bot_avatar = bot.user.display_avatar.url if bot.user else ""
    if bot_avatar:
        await _respond(
            inter,
            profile_card("Uma Enjoyers HQ", [f"**{k}:** {v}" for k, v in fields], bot_avatar, "info", "Moderation bot"),
            ephemeral=True,
        )
    else:
        await _respond(inter, card("Uma Enjoyers HQ", fields, "info", "Moderation bot"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /ping
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="ping",
    description="Check bot latency",
    contexts=GUILD_ONLY,
)
async def ping_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    latency = round(bot.latency * 1000)
    kind = "ok" if latency < 200 else ("warn" if latency < 500 else "error")
    await _respond(
        inter,
        simple("Pong", f"Latency: {latency}ms", kind, "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  TICKET SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

_TICKET_LABELS: dict[str, str] = {
    "question": "Question",
    "complaint": "Complaint",
    "appeal": "Appeal",
    "bug": "Bug",
    "partnership": "Partnership",
    "other": "Other",
}


async def _get_or_create_category(guild: disnake.Guild, name: str) -> disnake.CategoryChannel:
    for cat in guild.categories:
        if cat.name.lower() == name.lower():
            return cat
    return await guild.create_category(name)


@bot.listen("on_button_click")
async def ticket_open(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket:"):
        return

    ticket_type = cid.split(":", 1)[1]
    label = _TICKET_LABELS.get(ticket_type, ticket_type)
    guild = inter.guild
    if guild is None:
        return
    author = inter.author

    tickets, flush = save("tickets")
    gk, uk = _guild_key(guild), _user_key(author)

    existing = tickets.get(gk, {}).get(uk)
    if existing:
        return await _respond(
            inter,
            simple("Ticket", f"You already have an open ticket: <#{existing}>", "warn", "Uma Enjoyers HQ"),
            ephemeral=True,
        )

    category = await _get_or_create_category(guild, TICKET_CATEGORY_NAME)

    overwrites: dict[disnake.Role | disnake.Member, disnake.PermissionOverwrite] = {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        guild.me: disnake.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, manage_messages=True
        ),
        author: disnake.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),  # type: ignore[index]
    }

    for role in guild.roles:
        if role.permissions.kick_members and not role.is_default():
            overwrites[role] = disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)

    cfg = get_cfg(guild)
    mod_role_id = cfg.get("mod_role")
    if mod_role_id:
        mod_role = guild.get_role(int(mod_role_id))
        if mod_role:
            overwrites[mod_role] = disnake.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True
            )

    ticket_num = len(tickets.get(gk, {})) + 1
    channel = await guild.create_text_channel(
        name=f"ticket-{ticket_num:04d}",
        category=category,
        topic=f"{label} | {author} | {author.id} | Medium",
        overwrites=overwrites,
    )

    tickets.setdefault(gk, {})[uk] = str(channel.id)
    flush()

    avatar_url = author.display_avatar.url

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"### Ticket #{ticket_num:04d}\n\n"
                    f"**Type:** {label}\n"
                    f"**Author:** {author.mention}\n"
                    f"**Priority:** Medium\n\n"
                    f"Please describe your issue in detail. Moderators will respond shortly."
                ),
                accessory=disnake.ui.Thumbnail(avatar_url),
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.StringSelect(
                    custom_id=f"ticket_priority:{channel.id}",
                    placeholder="Set priority",
                    options=[
                        SelectOption(label="Low", value="low"),
                        SelectOption(label="Medium", value="medium"),
                        SelectOption(label="High", value="high"),
                        SelectOption(label="Critical", value="critical"),
                    ],
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.UserSelect(
                    custom_id=f"ticket_add_user:{channel.id}",
                    placeholder="Add a member",
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Close ticket",
                    custom_id=f"ticket_close:{channel.id}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Save transcript",
                    custom_id=f"ticket_transcript:{channel.id}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Claim",
                    custom_id=f"ticket_claim:{channel.id}",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["plain"]),
        )
    ]

    await channel.send(components=comps, flags=MessageFlags(is_components_v2=True))

    await _respond(
        inter,
        simple("Ticket Created", f"Your ticket: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )

    await send_log(
        guild,
        card(
            "Ticket Opened",
            [
                ("Type", label),
                ("Author", f"{author.mention} ({author.id})"),
                ("Channel", channel.mention),
            ],
            "info",
            "Uma Enjoyers HQ",
        ),
    )


@bot.listen("on_string_select")
async def ticket_priority_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_priority:"):
        return
    if not inter.author.guild_permissions.kick_members:  # type: ignore[union-attr]
        return await _respond(
            inter,
            simple("Error", "Only moderators can change priority", "error"),
            ephemeral=True,
        )
    priority_map = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}
    priority = priority_map.get(inter.values[0], "Medium")
    channel = inter.channel
    if isinstance(channel, disnake.TextChannel) and channel.topic:
        parts = channel.topic.split(" | ")
        if len(parts) >= 4:
            parts[3] = priority
        new_topic = " | ".join(parts)
        await channel.edit(topic=new_topic)
    kind = "error" if inter.values[0] == "critical" else "ok"
    await _respond(
        inter,
        simple("Priority", f"Priority changed: **{priority}**", kind, "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_user_select")
async def ticket_add_user_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_add_user:"):
        return
    if not inter.author.guild_permissions.kick_members:  # type: ignore[union-attr]
        return await _respond(
            inter,
            simple("Error", "Only moderators can add members", "error"),
            ephemeral=True,
        )
    channel = inter.channel
    if not isinstance(channel, disnake.TextChannel):
        return
    added: list[str] = []
    for user in inter.resolved_values:
        if isinstance(user, disnake.Member):
            await channel.set_permissions(user, view_channel=True, send_messages=True)
            added.append(user.mention)
    await _respond(
        inter,
        simple("Members Added", f"{', '.join(added)} added to ticket", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def ticket_claim_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_claim:"):
        return
    guild = inter.guild
    if guild is None:
        return
    await _respond(
        inter,
        card(
            "Ticket Claimed",
            [
                ("Moderator", inter.author.mention),
                ("Time", fmt_ts(datetime.now(timezone.utc))),
            ],
            "info",
            "Uma Enjoyers HQ",
        ),
    )
    await send_log(
        guild,
        simple(
            "Claim",
            f"**Moderator:** {inter.author.mention}\n**Channel:** {inter.channel.mention}",  # type: ignore[union-attr]
            "info",
        ),
    )


@bot.listen("on_button_click")
async def ticket_transcript_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_transcript:"):
        return
    channel = inter.channel
    if not isinstance(channel, disnake.TextChannel):
        return
    transcript = await _build_transcript(channel)
    file = disnake.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
    await _respond(
        inter,
        simple("Transcript", f"Transcript saved ({len(transcript)} characters)", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
        file=file,
    )


async def _build_transcript(channel: disnake.TextChannel) -> str:
    messages: list[disnake.Message] = []
    async for msg in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
        messages.append(msg)
    lines: list[str] = [
        f"=== Transcript of #{channel.name} ===",
        f"=== Server: {channel.guild.name} ===",
        f"=== Date: {fmt_ts(datetime.now(timezone.utc))} ===",
        f"=== Messages: {len(messages)} ===",
        "",
    ]
    for msg in messages:
        ts = msg.created_at.strftime("%d.%m %H:%M")
        content = msg.content or "(no text)"
        attachments = ""
        if msg.attachments:
            attachments = " [Attachments: " + ", ".join(a.filename for a in msg.attachments) + "]"
        lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {content}{attachments}")
    return "\n".join(lines)


@bot.listen("on_button_click")
async def ticket_close_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_close:"):
        return

    channel_id = cid.split(":", 1)[1]

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("### Close Ticket"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay("Choose an action:"),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Delete channel",
                    custom_id=f"ticket_delete:{channel_id}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Archive",
                    custom_id=f"ticket_archive:{channel_id}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Cancel",
                    custom_id="ticket_close_cancel",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["warn"]),
        )
    ]
    await _respond(inter, comps, ephemeral=True)


@bot.listen("on_button_click")
async def ticket_close_cancel_btn(inter: disnake.MessageInteraction) -> None:
    if (inter.component.custom_id or "") != "ticket_close_cancel":
        return
    await _respond(inter, simple("Cancelled", "Ticket close cancelled", "plain"), ephemeral=True)


@bot.listen("on_button_click")
async def ticket_delete_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_delete:"):
        return
    channel_id = cid.split(":", 1)[1]
    guild = inter.guild
    if guild is None:
        return

    _remove_ticket_by_channel(guild, channel_id)

    await send_log(
        guild,
        card(
            "Ticket Deleted",
            [
                ("Channel ID", channel_id),
                ("Moderator", inter.author.mention),
            ],
            "error",
            "Uma Enjoyers HQ",
        ),
    )
    await _respond(
        inter,
        simple("Deletion", "Channel will be deleted in 2 seconds", "error", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await asyncio.sleep(2)
    channel = guild.get_channel(int(channel_id))
    if channel and isinstance(channel, disnake.TextChannel):
        await channel.delete(reason=f"Ticket deleted by moderator {inter.author}")


@bot.listen("on_button_click")
async def ticket_archive_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_archive:"):
        return
    channel_id = cid.split(":", 1)[1]
    guild = inter.guild
    if guild is None:
        return

    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, disnake.TextChannel):
        return

    transcript = await _build_transcript(channel)
    file_for_log = disnake.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")

    author_id = _find_ticket_author(guild, channel_id)
    _remove_ticket_by_channel(guild, channel_id)

    cfg = _load("config.json", {})
    gk = _guild_key(guild)
    log_ch_id = cfg.get(gk, {}).get("log_channel")
    if log_ch_id:
        log_ch = guild.get_channel(int(log_ch_id))
        if log_ch and isinstance(log_ch, disnake.TextChannel):
            await log_ch.send(
                components=card(
                    "Ticket Archived",
                    [
                        ("Channel", channel.name),
                        ("Moderator", inter.author.mention),
                    ],
                    "info",
                    "Uma Enjoyers HQ",
                ),
                flags=MessageFlags(is_components_v2=True),
                file=file_for_log,
            )

    if author_id:
        author_member = guild.get_member(int(author_id))
        if author_member:
            await channel.set_permissions(author_member, send_messages=False)
            file_for_dm = disnake.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
            try:
                await author_member.send(
                    components=simple(
                        "Ticket Archived",
                        f"Your ticket in **{guild.name}** has been archived.\nTranscript is attached below.",
                        "info",
                        "Uma Enjoyers HQ",
                    ),
                    flags=MessageFlags(is_components_v2=True),
                    file=file_for_dm,
                )
            except disnake.Forbidden:
                pass

    archive_category = await _get_or_create_category(guild, ARCHIVE_CATEGORY_NAME)
    await channel.edit(
        name=f"archived-{channel.name}",
        category=archive_category,
    )

    await _respond(
        inter,
        simple("Archived", "Ticket archived. Transcript sent to logs.", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


def _find_ticket_author(guild: disnake.Guild, channel_id: str) -> str | None:
    tickets = _load("tickets.json", {})
    gk = _guild_key(guild)
    guild_tickets = tickets.get(gk, {})
    for uid, ch_id in guild_tickets.items():
        if str(ch_id) == str(channel_id):
            return uid
    return None


def _remove_ticket_by_channel(guild: disnake.Guild, channel_id: str) -> None:
    tickets, flush = save("tickets")
    gk = _guild_key(guild)
    guild_tickets = tickets.get(gk, {})
    to_remove = [uid for uid, ch_id in guild_tickets.items() if str(ch_id) == str(channel_id)]
    for uid in to_remove:
        del guild_tickets[uid]
    flush()


# ═══════════════════════════════════════════════════════════════════════════
#  AFK HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _handle_afk_return(message: disnake.Message) -> None:
    assert message.guild is not None
    key = f"{message.guild.id}:{message.author.id}"
    if key in _afk_users:
        afk_data = _afk_users.pop(key)
        asyncio.create_task(
            message.channel.send(
                components=simple(
                    "AFK",
                    f"{message.author.mention} is back (was AFK: {afk_data['message']})",
                    "info",
                ),
                flags=MessageFlags(is_components_v2=True),
            )
        )


async def _handle_afk_mentions(message: disnake.Message) -> None:
    assert message.guild is not None
    for user in message.mentions:
        key = f"{message.guild.id}:{user.id}"
        if key in _afk_users:
            afk_data = _afk_users[key]
            await message.channel.send(
                components=simple(
                    "AFK",
                    f"{user.display_name} is currently AFK: {afk_data['message']}",
                    "info",
                ),
                flags=MessageFlags(is_components_v2=True),
            )


# ═══════════════════════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════════════════════





@bot.event
async def on_member_remove(member: disnake.Member) -> None:
    cfg = get_cfg(member.guild)
    ch_id = cfg.get("leave_channel")
    if not ch_id:
        return
    ch = member.guild.get_channel(int(ch_id))
    if not ch or not isinstance(ch, disnake.TextChannel):
        return
    avatar_url = member.display_avatar.url
    roles = ", ".join(r.name for r in member.roles[1:]) if len(member.roles) > 1 else "None"
    joined = member.joined_at
    if joined:
        delta = datetime.now(timezone.utc) - joined
        time_on_server = fmt_dur(delta)
    else:
        time_on_server = "N/A"

    comps = profile_card(
        "Member Left",
        [
            f"**{member}** ({member.id})",
            f"**Roles:** {roles}",
            f"**Time on server:** {time_on_server}",
        ],
        avatar_url,
        "error",
        f"Uma Enjoyers HQ | {member.guild.name}",
    )
    await ch.send(components=comps, flags=MessageFlags(is_components_v2=True))


@bot.event
async def on_member_update(before: disnake.Member, after: disnake.Member) -> None:
    if before.roles == after.roles:
        return
    before_roles = set(before.roles)
    after_roles = set(after.roles)
    added = after_roles - before_roles
    removed = before_roles - after_roles
    if not added and not removed:
        return

    fields: list[tuple[str, str]] = [("Member", f"{after.mention} ({after.id})")]
    if added:
        fields.append(("Added", ", ".join(r.mention for r in added)))
    if removed:
        fields.append(("Removed", ", ".join(r.mention for r in removed)))

    await send_log(after.guild, card("Role Change", fields, "info"))


@bot.event
async def on_message_delete(message: disnake.Message) -> None:
    if message.author.bot or not message.guild:
        return
    _snipe_cache[message.channel.id] = {
        "content": message.content or "(no text)",
        "author": str(message.author),
        "author_id": message.author.id,
        "avatar": message.author.display_avatar.url,
        "ts": datetime.now(timezone.utc),
        "attachments": [a.filename for a in message.attachments],
    }
    text = truncate(message.content, 1000) if message.content else "(empty)"
    attachments = ""
    if message.attachments:
        attachments = f"\n**Attachments:** {', '.join(a.filename for a in message.attachments)}"

    await send_log(
        message.guild,
        card(
            "Message Deleted",
            [
                ("Author", f"{message.author.mention} ({message.author.id})"),
                ("Channel", message.channel.mention),  # type: ignore[union-attr]
                ("Content", text),
            ],
            "error",
            f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}" + attachments,
        ),
    )


@bot.event
async def on_message_edit(before: disnake.Message, after: disnake.Message) -> None:
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return
    _editsnipe_cache[before.channel.id] = {
        "before": before.content or "(empty)",
        "after": after.content or "(empty)",
        "author": str(before.author),
        "author_id": before.author.id,
        "avatar": before.author.display_avatar.url,
        "ts": datetime.now(timezone.utc),
    }
    old = truncate(before.content, 500) if before.content else "(empty)"
    new = truncate(after.content, 500) if after.content else "(empty)"
    await send_log(
        before.guild,
        card(
            "Message Edited",
            [
                ("Author", f"{before.author.mention} ({before.author.id})"),
                ("Channel", before.channel.mention),  # type: ignore[union-attr]
                ("Before", old),
                ("After", new),
            ],
            "warn",
            f"Uma Enjoyers HQ | {fmt_ts(datetime.now(timezone.utc))}",
        ),
    )


@bot.event
async def on_guild_channel_create(channel: disnake.abc.GuildChannel) -> None:
    await send_log(
        channel.guild,
        card(
            "Channel Created",
            [
                ("Name", channel.name),
                ("Type", str(channel.type)),
                ("ID", str(channel.id)),
            ],
            "ok",
        ),
    )


@bot.event
async def on_guild_role_create(role: disnake.Role) -> None:
    await send_log(
        role.guild,
        card(
            "Role Created",
            [
                ("Name", role.name),
                ("ID", str(role.id)),
                ("Color", f"#{role.colour.value:06x}"),
            ],
            "ok",
        ),
    )


@bot.event
async def on_member_unban(guild: disnake.Guild, user: disnake.User) -> None:
    await send_log(
        guild,
        card(
            "Member Unbanned (event)",
            [
                ("User", f"{user} ({user.id})"),
            ],
            "ok",
        ),
    )


@bot.event
async def on_voice_state_update(
    member: disnake.Member,
    before: disnake.VoiceState,
    after: disnake.VoiceState,
) -> None:
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        await send_log(
            member.guild,
            simple(
                "Voice Channel",
                f"{member.mention} joined {after.channel.mention}",
                "ok",
            ),
        )
    elif before.channel is not None and after.channel is None:
        await send_log(
            member.guild,
            simple(
                "Voice Channel",
                f"{member.mention} left {before.channel.mention}",
                "info",
            ),
        )
    elif before.channel is not None and after.channel is not None:
        await send_log(
            member.guild,
            simple(
                "Voice Channel",
                f"{member.mention} moved from {before.channel.mention} to {after.channel.mention}",
                "info",
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════


@bot.event
async def on_slash_command_error(
    inter: disnake.ApplicationCommandInteraction,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.MissingPermissions):
        await _respond(
            inter,
            simple("Access Denied", "You do not have permission to use this command", "error", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    elif isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await _respond(
            inter,
            simple("Bot Error", f"The bot lacks permissions: {missing}", "error", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    elif isinstance(error, commands.MemberNotFound):
        await _respond(
            inter,
            simple("Error", "Member not found", "error", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    elif isinstance(error, commands.BadArgument):
        await _respond(
            inter,
            simple("Error", f"Invalid argument: {error}", "error", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    elif isinstance(error, commands.CommandOnCooldown):
        await _respond(
            inter,
            simple("Cooldown", f"Please wait {error.retry_after:.1f}s", "warn", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            simple("Error", truncate(str(error), 400), "error", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
        raise error


# ═══════════════════════════════════════════════════════════════════════════
#  /snipe, /editsnipe
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="snipe",
    description="Show last deleted message in channel",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def snipe_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    data = _snipe_cache.get(inter.channel.id)
    if not data:
        return await _respond(
            inter,
            simple("Snipe", "No deleted messages in this channel", "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    age = datetime.now(timezone.utc) - data["ts"]
    text = truncate(data["content"], 1000)
    attachments = ""
    if data.get("attachments"):
        attachments = f"\n**Attachments:** {', '.join(data['attachments'])}"
    await _respond(
        inter,
        profile_card(
            "Snipe",
            [
                f"**{data['author']}** ({data['author_id']})",
                f"",
                text,
                attachments,
                f"",
                f"Deleted {fmt_dur(age)} ago",
            ],
            data["avatar"],
            "error",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="editsnipe",
    description="Show last edited message",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def editsnipe_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    data = _editsnipe_cache.get(inter.channel.id)
    if not data:
        return await _respond(
            inter,
            simple("EditSnipe", "No edited messages in this channel", "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    age = datetime.now(timezone.utc) - data["ts"]
    await _respond(
        inter,
        profile_card(
            "EditSnipe",
            [
                f"**{data['author']}** ({data['author_id']})",
                f"",
                f"**Before:** {truncate(data['before'], 500)}",
                f"**After:** {truncate(data['after'], 500)}",
                f"",
                f"Edited {fmt_dur(age)} ago",
            ],
            data["avatar"],
            "warn",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /poll
# ═══════════════════════════════════════════════════════════════════════════

_poll_data: dict[str, dict[str, Any]] = {}


@bot.slash_command(
    name="poll",
    contexts=GUILD_ONLY,
)
async def poll_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@poll_group.sub_command(name="create", description="Create a poll")
async def poll_create(
    inter: disnake.ApplicationCommandInteraction,
    question: str,
    option1: str,
    option2: str,
    option3: str | None = None,
    option4: str | None = None,
    option5: str | None = None,
    option6: str | None = None,
    option7: str | None = None,
    option8: str | None = None,
) -> None:
    assert inter.guild is not None
    options = [o for o in [option1, option2, option3, option4, option5, option6, option7, option8] if o]
    poll_id = _gen_id()

    _poll_data[poll_id] = {
        "question": question,
        "options": options,
        "votes": {str(i): [] for i in range(len(options))},
        "author": str(inter.author.id),
        "guild": str(inter.guild.id),
        "ts": now_iso(),
    }

    children: list[Any] = [
        disnake.ui.TextDisplay(f"### Poll"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
        disnake.ui.TextDisplay(f"**{question}**\n\nAuthor: {inter.author.mention}"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
    ]

    for i, opt in enumerate(options):
        children.append(disnake.ui.TextDisplay(f"**{i + 1}.** {opt} -- 0 votes"))

    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))

    rows: list[list[Any]] = [[]]
    for i, opt in enumerate(options):
        if len(rows[-1]) >= 5:
            rows.append([])
        rows[-1].append(
            disnake.ui.Button(
                label=str(i + 1),
                custom_id=f"poll_vote:{poll_id}:{i}",
                style=ButtonStyle.secondary,
            )
        )

    for row in rows:
        if row:
            children.append(disnake.ui.ActionRow(*row))

    children.append(
        disnake.ui.ActionRow(
            disnake.ui.Button(
                label="End",
                custom_id=f"poll_end:{poll_id}",
                style=ButtonStyle.danger,
            ),
        )
    )

    comps = [disnake.ui.Container(*children, accent_colour=Color(COLORS["info"]))]

    await inter.response.send_message(
        components=comps,
        flags=MessageFlags(is_components_v2=True),
    )


@bot.listen("on_button_click")
async def poll_vote_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("poll_vote:"):
        return
    parts = cid.split(":", 2)
    poll_id = parts[1]
    option_idx = parts[2]

    poll = _poll_data.get(poll_id)
    if not poll:
        return await _respond(inter, simple("Error", "Poll not found", "error"), ephemeral=True)

    uid = str(inter.author.id)
    for idx, voters in poll["votes"].items():
        if uid in voters:
            voters.remove(uid)

    poll["votes"][option_idx].append(uid)
    total = sum(len(v) for v in poll["votes"].values())
    my_choice = poll["options"][int(option_idx)]

    await _respond(
        inter,
        simple(
            "Vote Accepted",
            f"You voted for: **{my_choice}**\nTotal votes: {total}",
            "ok",
        ),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def poll_end_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("poll_end:"):
        return
    poll_id = cid.split(":", 1)[1]
    poll = _poll_data.get(poll_id)
    if not poll:
        return await _respond(inter, simple("Error", "Poll not found", "error"), ephemeral=True)

    if str(inter.author.id) != poll["author"] and not inter.author.guild_permissions.administrator:  # type: ignore[union-attr]
        return await _respond(inter, simple("Error", "Only the author or an administrator can end this", "error"), ephemeral=True)

    total = sum(len(v) for v in poll["votes"].values())
    items: list[str] = []
    for i, opt in enumerate(poll["options"]):
        votes = len(poll["votes"][str(i)])
        pct = f" ({votes * 100 // total}%)" if total > 0 else ""
        bar_len = (votes * 20 // total) if total > 0 else 0
        bar = "`" + "#" * bar_len + "-" * (20 - bar_len) + "`"
        items.append(f"**{i + 1}.** {opt} -- {votes} vote(s){pct}\n{bar}")

    del _poll_data[poll_id]

    await _respond(
        inter,
        list_card(
            f"Results: {poll['question']}",
            items,
            "info",
            f"Total votes: {total}",
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /starboard
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="starboard", description="Set up starboard channel and threshold")
async def setup_starboard(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    threshold: int = commands.Param(default=3, ge=1, le=25, description="Minimum reactions to qualify"),
    emoji: str = commands.Param(default="star", description="Reaction (star, fire, heart, thumbsup)"),
) -> None:
    emoji_map = {"star": "\u2b50", "fire": "\U0001f525", "heart": "\u2764\ufe0f", "thumbsup": "\U0001f44d"}
    actual_emoji = emoji_map.get(emoji, "\u2b50")
    set_cfg(inter.guild, "starboard_channel", str(channel.id))
    set_cfg(inter.guild, "starboard_threshold", threshold)
    set_cfg(inter.guild, "starboard_emoji", actual_emoji)
    await _respond(
        inter,
        simple(
            "Setup",
            f"Starboard: {channel.mention}\nThreshold: {threshold} reactions\nEmoji: {actual_emoji}",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


_starboard_sent: set[int] = set()


@bot.event
async def on_raw_reaction_add(payload: disnake.RawReactionActionEvent) -> None:
    if not payload.guild_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    cfg = get_cfg(guild)
    sb_channel_id = cfg.get("starboard_channel")
    sb_threshold = cfg.get("starboard_threshold", 3)
    sb_emoji = cfg.get("starboard_emoji", "\u2b50")
    if not sb_channel_id:
        return
    if str(payload.emoji) != sb_emoji:
        return
    if payload.message_id in _starboard_sent:
        return

    channel = guild.get_channel(payload.channel_id)
    if not isinstance(channel, disnake.TextChannel):
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except disnake.NotFound:
        return

    for reaction in message.reactions:
        if str(reaction.emoji) == sb_emoji and reaction.count >= sb_threshold:
            sb_channel = guild.get_channel(int(sb_channel_id))
            if not sb_channel or not isinstance(sb_channel, disnake.TextChannel):
                return
            _starboard_sent.add(payload.message_id)
            text = truncate(message.content, 500) if message.content else "(no text)"
            await sb_channel.send(
                components=profile_card(
                    f"Starboard ({sb_emoji} {reaction.count})",
                    [
                        f"**{message.author}** in {channel.mention}",
                        "",
                        text,
                        "",
                        f"[Jump to message]({message.jump_url})",
                    ],
                    message.author.display_avatar.url,
                    "warn",
                    f"Uma Enjoyers HQ | {fmt_ts(message.created_at)}",
                ),
                flags=MessageFlags(is_components_v2=True),
            )
            break


# ═══════════════════════════════════════════════════════════════════════════
#  /autorole
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="autorole", description="Auto-role on join")
async def setup_autorole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role | None = None,
) -> None:
    if role:
        set_cfg(inter.guild, "autorole", str(role.id))
        await _respond(
            inter,
            simple("Setup", f"Auto-role: {role.mention}", "ok", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    else:
        set_cfg(inter.guild, "autorole", None)
        await _respond(
            inter,
            simple("Setup", "Auto-role disabled", "ok", "Uma Enjoyers HQ"),
            ephemeral=True,
        )


@setup_group.sub_command(name="welcomedm", description="Welcome DM message")
async def setup_welcomedm(
    inter: disnake.ApplicationCommandInteraction,
    message: str = commands.Param(description="Welcome text ({user} = mention, {server} = server)"),
) -> None:
    set_cfg(inter.guild, "welcome_dm", message)
    await _respond(
        inter,
        simple("Setup", f"Welcome DM: {message}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /nick
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="nick",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_nicknames=True),
)
async def nick_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@nick_group.sub_command(name="set", description="Set a member's nickname")
async def nick_set(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    nickname: str,
) -> None:
    old = member.display_name
    await member.edit(nick=nickname, reason=f"Changed by {inter.author}")
    await _respond(
        inter,
        simple("Nickname Changed", f"{member.mention}: **{old}** -> **{nickname}**", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Nickname Changed",
            f"**Member:** {member.mention}\n**Before:** {old}\n**After:** {nickname}\n**Moderator:** {inter.author.mention}",
            "info",
        ),
    )


@nick_group.sub_command(name="reset", description="Reset a member's nickname")
async def nick_reset(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    old = member.display_name
    await member.edit(nick=None, reason=f"Reset by {inter.author}")
    await _respond(
        inter,
        simple("Nickname Reset", f"{member.mention}: **{old}** -> original", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /roleall
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="roleall",
    description="Add or remove a role from all members",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def roleall_cmd(
    inter: disnake.ApplicationCommandInteraction,
    action: str = commands.Param(choices=["add", "remove"]),
    role: disnake.Role = commands.Param(),
) -> None:
    assert inter.guild is not None
    await inter.response.defer(ephemeral=True)
    count = 0
    errors = 0
    for member in inter.guild.members:
        if member.bot:
            continue
        try:
            if action == "add" and role not in member.roles:
                await member.add_roles(role, reason=f"Roleall by {inter.author}")
                count += 1
            elif action == "remove" and role in member.roles:
                await member.remove_roles(role, reason=f"Roleall by {inter.author}")
                count += 1
        except disnake.Forbidden:
            errors += 1
    verb = "added to" if action == "add" else "removed from"
    text = f"Role {role.mention} {verb} {count} members"
    if errors:
        text += f"\nErrors: {errors}"
    await inter.followup.send(
        components=simple("Roleall", text, "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /report (user reporting system)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="report",
    description="Report a member to moderators",
    contexts=GUILD_ONLY,
)
async def report_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str,
) -> None:
    assert inter.guild is not None
    reports, flush = save("reports")
    gk = _guild_key(inter.guild)
    reports.setdefault(gk, [])
    report_id = len(reports[gk]) + 1
    reports[gk].append({
        "id": report_id,
        "target": str(member.id),
        "target_name": str(member),
        "reporter": str(inter.author.id),
        "reporter_name": str(inter.author),
        "reason": reason,
        "ts": now_iso(),
        "status": "open",
    })
    flush()

    await _respond(
        inter,
        simple(
            "Report Sent",
            f"Report #{report_id} on {member.mention} sent to moderators.\n**Reason:** {reason}",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )

    await send_log(
        inter.guild,
        card(
            f"Report #{report_id}",
            [
                ("Offender", f"{member.mention} ({member.id})"),
                ("Reporter", f"{inter.author.mention} ({inter.author.id})"),
                ("Reason", reason),
                ("Status", "Open"),
            ],
            "warn",
            "Uma Enjoyers HQ",
        ),
    )


@bot.slash_command(
    name="reports",
    description="List reports",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def reports_cmd(
    inter: disnake.ApplicationCommandInteraction,
    status: str = commands.Param(default="open", choices=["open", "closed", "all"]),
) -> None:
    reports = _load("reports.json", {})
    gk = _guild_key(inter.guild)
    all_reports = reports.get(gk, [])
    if status != "all":
        filtered = [r for r in all_reports if r["status"] == status]
    else:
        filtered = all_reports

    if not filtered:
        return await _respond(
            inter,
            simple("Reports", f"No reports (filter: {status})", "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )

    items: list[str] = []
    for r in filtered[-15:]:
        items.append(
            f"**#{r['id']}** | <@{r['target']}> | {r['reason'][:50]} | {r['status']} | {r['ts'][:10]}"
        )
    await _respond(
        inter,
        list_card(
            "Reports",
            items,
            "warn",
            f"Total: {len(filtered)} | Showing last {len(items)}",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="resolve",
    description="Close a report",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def resolve_cmd(
    inter: disnake.ApplicationCommandInteraction,
    report_id: int,
) -> None:
    reports, flush = save("reports")
    gk = _guild_key(inter.guild)
    all_reports = reports.get(gk, [])
    for r in all_reports:
        if r["id"] == report_id:
            r["status"] = "closed"
            r["resolved_by"] = str(inter.author.id)
            r["resolved_at"] = now_iso()
            flush()
            return await _respond(
                inter,
                simple("Report Closed", f"Report #{report_id} closed by {inter.author.mention}", "ok", "Uma Enjoyers HQ"),
                ephemeral=True,
            )
    await _respond(
        inter,
        simple("Error", f"Report #{report_id} not found", "error"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /verify (verification system)
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="verify", description="Set up verification (role + panel)")
async def setup_verify(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
    channel: disnake.TextChannel,
    message: str = "Click the button below to gain access to the server.",
) -> None:
    set_cfg(inter.guild, "verify_role", str(role.id))
    set_cfg(inter.guild, "verify_channel", str(channel.id))

    panel = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("### Verification"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(message),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Verify",
                    custom_id="verify_btn",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["ok"]),
        )
    ]
    await channel.send(components=panel, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Setup", f"Verification: role {role.mention}, channel {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def verify_btn_click(inter: disnake.MessageInteraction) -> None:
    if (inter.component.custom_id or "") != "verify_btn":
        return
    guild = inter.guild
    if guild is None:
        return
    cfg = get_cfg(guild)
    role_id = cfg.get("verify_role")
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    if not role:
        return await _respond(inter, simple("Error", "Verification role not found", "error"), ephemeral=True)
    member = inter.author
    assert isinstance(member, disnake.Member)
    if role in member.roles:
        return await _respond(inter, simple("Verification", "You are already verified", "info"), ephemeral=True)
    await member.add_roles(role, reason="Verification")
    await _respond(
        inter,
        simple("Verification", "You have been verified!", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await send_log(guild, simple("Verification", f"{member.mention} verified", "ok"))


# ═══════════════════════════════════════════════════════════════════════════
#  /whois
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="whois",
    description="Detailed user info by ID (even if not on server)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def whois_cmd(
    inter: disnake.ApplicationCommandInteraction,
    user_id: str,
) -> None:
    try:
        user = await bot.fetch_user(int(user_id))
    except (ValueError, disnake.NotFound):
        return await _respond(
            inter,
            simple("Error", "User not found", "error"),
            ephemeral=True,
        )

    created = disnake.utils.format_dt(user.created_at, "F")
    created_r = disnake.utils.format_dt(user.created_at, "R")

    lines = [
        f"**Tag:** {user}",
        f"**ID:** {user.id}",
        f"**Bot:** {'Yes' if user.bot else 'No'}",
        f"**Created:** {created} ({created_r})",
    ]

    assert inter.guild is not None
    member = inter.guild.get_member(user.id)
    if member:
        lines.append(f"**On server:** Yes")
        if member.joined_at:
            lines.append(f"**Joined:** {disnake.utils.format_dt(member.joined_at, 'F')}")
        warn_count = len(_load("warnings.json", {}).get(_guild_key(inter.guild), {}).get(str(user.id), []))
        case_count = get_case_count(inter.guild, user)
        lines.append(f"**Warnings:** {warn_count} | **Cases:** {case_count}")
    else:
        lines.append(f"**On server:** No")

    try:
        ban = await inter.guild.fetch_ban(user)
        lines.append(f"**Banned:** Yes (reason: {ban.reason or 'N/A'})")
    except disnake.NotFound:
        lines.append(f"**Banned:** No")

    await _respond(
        inter,
        profile_card("Whois", lines, user.display_avatar.url, "info", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /invites
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="invites",
    description="Top server invites",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def invites_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    invites = await inter.guild.invites()
    if not invites:
        return await _respond(
            inter,
            simple("Invites", "No active invites", "info"),
            ephemeral=True,
        )

    sorted_invites = sorted(invites, key=lambda i: i.uses or 0, reverse=True)[:15]
    items: list[str] = []
    for inv in sorted_invites:
        inviter = inv.inviter.mention if inv.inviter else "N/A"
        items.append(f"**{inv.code}** | {inviter} | {inv.uses or 0} uses | {inv.channel.mention if inv.channel else 'N/A'}")  # type: ignore[union-attr]

    await _respond(
        inter,
        list_card(
            "Invites",
            items,
            "info",
            f"Total: {len(invites)} active invites",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /tempbans, /banlist
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="tempbans",
    description="List active temporary bans",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def tempbans_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    tempbans = _load("tempbans.json", {})
    gk = _guild_key(inter.guild)
    guild_bans = tempbans.get(gk, {})
    if not guild_bans:
        return await _respond(
            inter,
            simple("Temp Bans", "No active temporary bans", "info"),
            ephemeral=True,
        )
    now = now_ts()
    items: list[str] = []
    for uid, entry in guild_bans.items():
        remaining = entry["expires"] - now
        if remaining > 0:
            items.append(
                f"<@{uid}> ({uid}) | Remaining: {fmt_dur(timedelta(seconds=remaining))} | Reason: {entry.get('reason', 'N/A')}"
            )
    if not items:
        return await _respond(
            inter,
            simple("Temp Bans", "No active temporary bans", "info"),
            ephemeral=True,
        )
    await _respond(
        inter,
        list_card("Temp Bans", items, "error", f"Total: {len(items)}"),
        ephemeral=True,
    )


@bot.slash_command(
    name="banlist",
    description="Ban list (last 25)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def banlist_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    await inter.response.defer(ephemeral=True)
    bans: list[disnake.BanEntry] = []
    async for ban in inter.guild.bans(limit=25):
        bans.append(ban)
    if not bans:
        return await inter.followup.send(
            components=simple("Ban List", "Ban list is empty", "info"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    items: list[str] = []
    for ban in bans:
        items.append(f"**{ban.user}** ({ban.user.id}) | {ban.reason or 'No reason'}")
    await inter.followup.send(
        components=list_card("Ban List", items, "error", f"Showing last {len(items)}"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /modleaderboard
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="modleaderboard",
    description="Top moderators by action count",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def modleaderboard_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    cases = _load("cases.json", {})
    gk = _guild_key(inter.guild)
    guild_cases = cases.get(gk, {})

    mod_counts: dict[str, int] = defaultdict(int)
    for user_cases in guild_cases.values():
        for c in user_cases:
            mod_counts[c["mod"]] += 1

    if not mod_counts:
        return await _respond(
            inter,
            simple("Leaderboard", "No moderation data", "info"),
            ephemeral=True,
        )

    sorted_mods = sorted(mod_counts.items(), key=lambda x: -x[1])[:10]
    items: list[str] = []
    for i, (mod_id, count) in enumerate(sorted_mods, 1):
        medal = {1: "[1]", 2: "[2]", 3: "[3]"}.get(i, f"[{i}]")
        items.append(f"**{medal}** <@{mod_id}> -- {count} action(s)")

    await _respond(
        inter,
        list_card("Mod Leaderboard", items, "info", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /emojiinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="emojiinfo",
    description="Server emoji information",
    contexts=GUILD_ONLY,
)
async def emojiinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    emoji: disnake.Emoji | None = None,
) -> None:
    assert inter.guild is not None
    if emoji:
        fields = [
            ("Name", emoji.name),
            ("ID", str(emoji.id)),
            ("Animated", "Yes" if emoji.animated else "No"),
            ("Created", disnake.utils.format_dt(emoji.created_at, "F")),
            ("URL", emoji.url),
        ]
        await _respond(
            inter,
            card(f"Emoji: {emoji.name}", fields, "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    else:
        regular = [e for e in inter.guild.emojis if not e.animated]
        animated = [e for e in inter.guild.emojis if e.animated]
        await _respond(
            inter,
            card(
                "Server Emojis",
                [
                    ("Total", str(len(inter.guild.emojis))),
                    ("Static", str(len(regular))),
                    ("Animated", str(len(animated))),
                    ("Limit", str(inter.guild.emoji_limit)),
                ],
                "info",
                "Uma Enjoyers HQ",
            ),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /counting
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="counting", description="Set up counting channel")
async def setup_counting(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "counting_channel", str(channel.id))
    set_cfg(inter.guild, "counting_current", 0)
    set_cfg(inter.guild, "counting_last_user", None)
    await _respond(
        inter,
        simple("Setup", f"Counting channel: {channel.mention}\nCount started from 0.", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /suggest
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="suggestions", description="Suggestions channel")
async def setup_suggestions(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "suggestions_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Suggestions channel: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.slash_command(
    name="suggest",
    description="Submit a suggestion",
    contexts=GUILD_ONLY,
)
async def suggest_cmd(
    inter: disnake.ApplicationCommandInteraction,
    suggestion: str,
) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    ch_id = cfg.get("suggestions_channel")
    if not ch_id:
        return await _respond(
            inter,
            simple("Error", "Suggestions channel not set up. Ask an admin to use /setup suggestions", "error"),
            ephemeral=True,
        )

    channel = inter.guild.get_channel(int(ch_id))
    if not channel or not isinstance(channel, disnake.TextChannel):
        return await _respond(inter, simple("Error", "Channel not found", "error"), ephemeral=True)

    suggestions, flush = save("suggestions")
    gk = _guild_key(inter.guild)
    suggestions.setdefault(gk, [])
    sg_id = len(suggestions[gk]) + 1
    suggestions[gk].append({
        "id": sg_id,
        "text": suggestion,
        "author": str(inter.author.id),
        "author_name": str(inter.author),
        "ts": now_iso(),
        "votes_up": [],
        "votes_down": [],
        "status": "open",
    })
    flush()

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"### Suggestion #{sg_id}\n\n"
                    f"{suggestion}\n\n"
                    f"**Author:** {inter.author.mention}\n"
                    f"**For:** 0 | **Against:** 0"
                ),
                accessory=disnake.ui.Thumbnail(inter.author.display_avatar.url),
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="For",
                    custom_id=f"suggest_up:{gk}:{sg_id}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Against",
                    custom_id=f"suggest_down:{gk}:{sg_id}",
                    style=ButtonStyle.secondary,
                ),
            ),
            accent_colour=Color(COLORS["info"]),
        )
    ]

    await channel.send(components=comps, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Suggestion", f"Suggestion #{sg_id} sent to {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def suggest_vote_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("suggest_up:") and not cid.startswith("suggest_down:"):
        return
    parts = cid.split(":", 2)
    vote_type = "up" if "up" in parts[0] else "down"
    gk = parts[1]
    sg_id = int(parts[2])

    suggestions, flush = save("suggestions")
    sg_list = suggestions.get(gk, [])
    sg = None
    for s in sg_list:
        if s["id"] == sg_id:
            sg = s
            break
    if not sg:
        return await _respond(inter, simple("Error", "Suggestion not found", "error"), ephemeral=True)

    uid = str(inter.author.id)
    if uid in sg["votes_up"]:
        sg["votes_up"].remove(uid)
    if uid in sg["votes_down"]:
        sg["votes_down"].remove(uid)

    if vote_type == "up":
        sg["votes_up"].append(uid)
    else:
        sg["votes_down"].append(uid)
    flush()

    await _respond(
        inter,
        simple(
            "Vote Accepted",
            f"For: {len(sg['votes_up'])} | Against: {len(sg['votes_down'])}",
            "ok",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /embed (simple text panel builder)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="say",
    description="Send a message as the bot",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def say_cmd(
    inter: disnake.ApplicationCommandInteraction,
    text: str,
    channel: disnake.TextChannel | None = None,
    color: str = commands.Param(default="plain", choices=["plain", "ok", "warn", "error", "info"]),
) -> None:
    target = channel or inter.channel
    assert isinstance(target, disnake.TextChannel)
    await target.send(
        components=simple("Uma Enjoyers HQ", text, color),
        flags=MessageFlags(is_components_v2=True),
    )
    await _respond(
        inter,
        simple("Sent", f"Message sent to {target.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.slash_command(
    name="announce",
    description="Send an announcement",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def announce_cmd(
    inter: disnake.ApplicationCommandInteraction,
    title: str,
    text: str,
    channel: disnake.TextChannel | None = None,
) -> None:
    target = channel or inter.channel
    assert isinstance(target, disnake.TextChannel)

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### {title}"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(text),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(f"-# {inter.author} | {fmt_ts(datetime.now(timezone.utc))}"),
            accent_colour=Color(COLORS["info"]),
        )
    ]
    await target.send(components=comps, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Sent", f"Announcement sent to {target.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /customcmd (custom commands per guild)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="customcmd",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def customcmd_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@customcmd_group.sub_command(name="add", description="Add a custom command")
async def customcmd_add(
    inter: disnake.ApplicationCommandInteraction,
    trigger: str = commands.Param(description="Trigger (word or phrase)"),
    response: str = commands.Param(description="Bot response"),
) -> None:
    assert inter.guild is not None
    ccmds, flush = save("customcmds")
    gk = _guild_key(inter.guild)
    ccmds.setdefault(gk, {})
    ccmds[gk][trigger.lower()] = {
        "response": response,
        "author": str(inter.author.id),
        "ts": now_iso(),
        "uses": 0,
    }
    flush()
    await _respond(
        inter,
        simple("Command Added", f"Trigger: `{trigger}`\nResponse: {truncate(response, 200)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@customcmd_group.sub_command(name="remove", description="Remove a custom command")
async def customcmd_remove(
    inter: disnake.ApplicationCommandInteraction,
    trigger: str,
) -> None:
    assert inter.guild is not None
    ccmds, flush = save("customcmds")
    gk = _guild_key(inter.guild)
    if trigger.lower() in ccmds.get(gk, {}):
        del ccmds[gk][trigger.lower()]
        flush()
        await _respond(inter, simple("Command Removed", f"Trigger `{trigger}` removed", "ok", "Uma Enjoyers HQ"), ephemeral=True)
    else:
        await _respond(inter, simple("Error", f"Command `{trigger}` not found", "error"), ephemeral=True)


@customcmd_group.sub_command(name="list", description="List custom commands")
async def customcmd_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    ccmds = _load("customcmds.json", {})
    gk = _guild_key(inter.guild)
    guild_cmds = ccmds.get(gk, {})
    if not guild_cmds:
        return await _respond(inter, simple("Commands", "No custom commands", "info"), ephemeral=True)
    items = [f"`{t}` -- {truncate(d['response'], 60)} ({d.get('uses', 0)} uses)" for t, d in guild_cmds.items()]
    await _respond(
        inter,
        list_card("Custom Commands", items, "info", f"Total: {len(guild_cmds)}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /autoresponder
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="autoresponder",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def ar_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@ar_group.sub_command(name="add", description="Add an auto-response")
async def ar_add(
    inter: disnake.ApplicationCommandInteraction,
    trigger: str = commands.Param(description="Trigger word"),
    response: str = commands.Param(description="Response"),
    exact: bool = commands.Param(default=False, description="Exact match (not contains)"),
) -> None:
    assert inter.guild is not None
    ar, flush = save("autoresponders")
    gk = _guild_key(inter.guild)
    ar.setdefault(gk, [])
    ar[gk].append({
        "trigger": trigger.lower(),
        "response": response,
        "exact": exact,
        "author": str(inter.author.id),
        "ts": now_iso(),
    })
    flush()
    mode = "exact match" if exact else "contains"
    await _respond(
        inter,
        simple("Auto-Response Added", f"Trigger: `{trigger}` ({mode})\nResponse: {truncate(response, 200)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@ar_group.sub_command(name="remove", description="Remove an auto-response by number")
async def ar_remove(
    inter: disnake.ApplicationCommandInteraction,
    number: int = commands.Param(ge=1),
) -> None:
    assert inter.guild is not None
    ar, flush = save("autoresponders")
    gk = _guild_key(inter.guild)
    items = ar.get(gk, [])
    if number > len(items):
        return await _respond(inter, simple("Error", f"Auto-response #{number} not found", "error"), ephemeral=True)
    removed = items.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple("Auto-Response Removed", f"Removed: `{removed['trigger']}`", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@ar_group.sub_command(name="list", description="List auto-responses")
async def ar_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    ar = _load("autoresponders.json", {})
    gk = _guild_key(inter.guild)
    items = ar.get(gk, [])
    if not items:
        return await _respond(inter, simple("Auto-Responses", "No auto-responses", "info"), ephemeral=True)
    lines = [
        f"**#{i+1}** `{a['trigger']}` -> {truncate(a['response'], 60)} ({'exact' if a.get('exact') else 'contains'})"
        for i, a in enumerate(items)
    ]
    await _respond(inter, list_card("Auto-Responses", lines, "info", f"Total: {len(items)}"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /sticky
# ═══════════════════════════════════════════════════════════════════════════

_sticky_messages: dict[int, int] = {}  # channel_id -> message_id


@bot.slash_command(
    name="sticky",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def sticky_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@sticky_group.sub_command(name="set", description="Pin a message (will be re-sent periodically)")
async def sticky_set(
    inter: disnake.ApplicationCommandInteraction,
    text: str,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    stickies, flush = save("stickies")
    gk = _guild_key(inter.guild)
    stickies.setdefault(gk, {})
    stickies[gk][str(inter.channel.id)] = {
        "text": text,
        "message_id": None,
    }
    flush()

    msg = await inter.channel.send(
        components=simple("Sticky Message", text, "info", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
    )
    stickies[gk][str(inter.channel.id)]["message_id"] = str(msg.id)
    _sticky_messages[inter.channel.id] = msg.id
    flush()

    await _respond(
        inter,
        simple("Sticky", f"Sticky message has been set", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@sticky_group.sub_command(name="remove", description="Remove sticky message")
async def sticky_remove(inter: disnake.ApplicationCommandInteraction) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    stickies, flush = save("stickies")
    gk = _guild_key(inter.guild)
    ch_id = str(inter.channel.id)
    if gk in stickies and ch_id in stickies[gk]:
        old_msg_id = stickies[gk][ch_id].get("message_id")
        if old_msg_id:
            try:
                old_msg = await inter.channel.fetch_message(int(old_msg_id))
                await old_msg.delete()
            except Exception:
                pass
        del stickies[gk][ch_id]
        _sticky_messages.pop(inter.channel.id, None)
        flush()
    await _respond(inter, simple("Sticky", "Sticky message removed", "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /lockdown (server-wide lockdown)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="lockdown",
    description="Lock all text channels",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def lockdown_cmd(
    inter: disnake.ApplicationCommandInteraction,
    reason: str = "Emergency lockdown",
) -> None:
    assert inter.guild is not None
    await inter.response.defer(ephemeral=True)
    locked_count = 0
    for channel in inter.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(inter.guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(inter.guild.default_role, overwrite=overwrite, reason=reason)
            locked_count += 1
        except disnake.Forbidden:
            pass
    await inter.followup.send(
        components=card(
            "Server Locked Down",
            [
                ("Locked channels", str(locked_count)),
                ("Reason", reason),
                ("Moderator", inter.author.mention),
            ],
            "error",
            "Uma Enjoyers HQ | Use /unlockdown to lift",
        ),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,
        card(
            "Lockdown",
            [
                ("Channels", str(locked_count)),
                ("Reason", reason),
                ("Moderator", inter.author.mention),
            ],
            "error",
            "Uma Enjoyers HQ",
        ),
    )


@bot.slash_command(
    name="unlockdown",
    description="Unlock all text channels",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def unlockdown_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    await inter.response.defer(ephemeral=True)
    unlocked = 0
    for channel in inter.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(inter.guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(inter.guild.default_role, overwrite=overwrite)
            unlocked += 1
        except disnake.Forbidden:
            pass
    await inter.followup.send(
        components=simple("Server Unlocked", f"Unlocked channels: {unlocked}", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,
        simple("Unlockdown", f"**Channels:** {unlocked}\n**Moderator:** {inter.author.mention}", "ok"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /quarantine
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="quarantine", description="Set up quarantine role")
async def setup_quarantine(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "quarantine_role", str(role.id))
    await _respond(
        inter,
        simple("Setup", f"Quarantine role: {role.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.slash_command(
    name="quarantine",
    description="Quarantine a member",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def quarantine_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    role_id = cfg.get("quarantine_role")
    if not role_id:
        return await _respond(
            inter,
            simple("Error", "Quarantine role not set up. Use /setup quarantine", "error"),
            ephemeral=True,
        )
    role = inter.guild.get_role(int(role_id))
    if not role:
        return await _respond(inter, simple("Error", "Role not found", "error"), ephemeral=True)

    saved_roles = [r.id for r in member.roles[1:] if r < inter.guild.me.top_role and not r.managed]
    quarantined, flush = save("quarantined")
    gk = _guild_key(inter.guild)
    quarantined.setdefault(gk, {})
    quarantined[gk][str(member.id)] = {
        "saved_roles": saved_roles,
        "reason": reason,
        "mod": str(inter.author.id),
        "ts": now_iso(),
    }
    flush()

    roles_to_remove = [r for r in member.roles[1:] if r < inter.guild.me.top_role and not r.managed]
    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason=f"Quarantine: {reason}")
    await member.add_roles(role, reason=f"Quarantine: {reason}")

    cid = add_case(inter.guild, member, "quarantine", inter.author, reason)
    c = card(
        "Quarantine",
        [
            ("Member", f"{member.mention} ({member.id})"),
            ("Reason", reason),
            ("Saved roles", str(len(saved_roles))),
            ("Moderator", inter.author.mention),
            ("Case", f"#{cid}"),
        ],
        "error",
        "Uma Enjoyers HQ",
    )
    await _respond(inter, c, ephemeral=True)
    await send_log(inter.guild, c)
    await _try_dm(
        member,
        simple(
            f"Uma Enjoyers HQ -- {inter.guild.name}",
            f"You have been quarantined.\n\n**Reason:** {reason}",
            "error",
            f"Case #{cid}",
        ),
    )


@bot.slash_command(
    name="unquarantine",
    description="Remove quarantine and restore roles",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def unquarantine_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    role_id = cfg.get("quarantine_role")

    quarantined, flush = save("quarantined")
    gk = _guild_key(inter.guild)
    data = quarantined.get(gk, {}).get(str(member.id))

    if role_id:
        role = inter.guild.get_role(int(role_id))
        if role and role in member.roles:
            await member.remove_roles(role, reason="Quarantine lifted")

    if data:
        roles_to_add = []
        for rid in data.get("saved_roles", []):
            r = inter.guild.get_role(rid)
            if r:
                roles_to_add.append(r)
        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Restoring roles after quarantine")
        del quarantined[gk][str(member.id)]
        flush()
        restored = len(roles_to_add)
    else:
        restored = 0

    c = card(
        "Quarantine Lifted",
        [
            ("Member", f"{member.mention}"),
            ("Restored roles", str(restored)),
            ("Moderator", inter.author.mention),
        ],
        "ok",
        "Uma Enjoyers HQ",
    )
    await _respond(inter, c, ephemeral=True)
    await send_log(inter.guild, c)


# ═══════════════════════════════════════════════════════════════════════════
#  /voicetools
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="voice",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(move_members=True),
)
async def voice_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@voice_group.sub_command(name="kick", description="Disconnect a member from voice channel")
async def voice_kick(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Not specified",
) -> None:
    if not member.voice or not member.voice.channel:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    ch_name = member.voice.channel.name
    await member.move_to(None, reason=reason)
    await _respond(
        inter,
        simple("Voice Kick", f"{member.mention} disconnected from {ch_name}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Voice Kick", f"**Member:** {member.mention}\n**Channel:** {ch_name}\n**Moderator:** {inter.author.mention}", "info"),
    )


@voice_group.sub_command(name="move", description="Move a member to another voice channel")
async def voice_move(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    channel: disnake.VoiceChannel,
) -> None:
    if not member.voice or not member.voice.channel:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    old_ch = member.voice.channel.name
    await member.move_to(channel)
    await _respond(
        inter,
        simple("Move", f"{member.mention}: {old_ch} -> {channel.name}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@voice_group.sub_command(name="moveall", description="Move all members from one channel to another")
async def voice_moveall(
    inter: disnake.ApplicationCommandInteraction,
    source: disnake.VoiceChannel,
    target: disnake.VoiceChannel,
) -> None:
    await inter.response.defer(ephemeral=True)
    moved = 0
    for member in source.members:
        try:
            await member.move_to(target)
            moved += 1
        except Exception:
            pass
    await inter.followup.send(
        components=simple("Move", f"Moved {moved} members from {source.name} to {target.name}", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@voice_group.sub_command(name="limit", description="Set member limit for a voice channel")
async def voice_limit(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.VoiceChannel,
    limit: int = commands.Param(ge=0, le=99, description="0 = no limit"),
) -> None:
    await channel.edit(user_limit=limit)
    text = f"Limit {channel.name}: {limit}" if limit else f"Limit {channel.name} removed"
    await _respond(inter, simple("Voice Channel", text, "ok", "Uma Enjoyers HQ"), ephemeral=True)


@voice_group.sub_command(name="mute", description="Server mute a member in voice channel")
async def voice_mute(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    await member.edit(mute=True, reason=f"Voice mute by {inter.author}")
    await _respond(
        inter,
        simple("Voice Mute", f"{member.mention} has been muted in voice", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@voice_group.sub_command(name="unmute", description="Server unmute a member in voice channel")
async def voice_unmute(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    await member.edit(mute=False, reason=f"Voice unmute by {inter.author}")
    await _respond(
        inter,
        simple("Voice Unmuted", f"{member.mention} has been unmuted", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@voice_group.sub_command(name="deafen", description="Server deafen a member in voice channel")
async def voice_deafen(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    await member.edit(deafen=True, reason=f"Voice deafen by {inter.author}")
    await _respond(inter, simple("Deafened", f"{member.mention} has been deafened", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@voice_group.sub_command(name="undeafen", description="Remove server deafen")
async def voice_undeafen(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Error", "Member is not in a voice channel", "error"), ephemeral=True)
    await member.edit(deafen=False, reason=f"Voice undeafen by {inter.author}")
    await _respond(inter, simple("Undeafened", f"{member.mention} has been undeafened", "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /thread
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="thread",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_threads=True),
)
async def thread_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@thread_group.sub_command(name="create", description="Create a thread")
async def thread_create(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
    auto_archive: int = commands.Param(default=1440, choices=[60, 1440, 4320, 10080]),
    private: bool = False,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    thread_type = disnake.ChannelType.private_thread if private else disnake.ChannelType.public_thread
    thread = await inter.channel.create_thread(
        name=name,
        type=thread_type,
        auto_archive_duration=auto_archive,
    )
    await _respond(
        inter,
        simple("Thread Created", f"{thread.mention} ({('private' if private else 'public')})", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@thread_group.sub_command(name="archive", description="Archive a thread")
async def thread_archive(
    inter: disnake.ApplicationCommandInteraction,
) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Error", "This command only works in a thread", "error"), ephemeral=True)
    await inter.channel.edit(archived=True)
    await _respond(inter, simple("Thread", "Thread archived", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@thread_group.sub_command(name="lock", description="Lock a thread")
async def thread_lock(inter: disnake.ApplicationCommandInteraction) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Error", "This command only works in a thread", "error"), ephemeral=True)
    await inter.channel.edit(locked=True)
    await _respond(inter, simple("Thread", "Thread locked", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@thread_group.sub_command(name="unlock", description="Unlock a thread")
async def thread_unlock(inter: disnake.ApplicationCommandInteraction) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Error", "This command only works in a thread", "error"), ephemeral=True)
    await inter.channel.edit(locked=False)
    await _respond(inter, simple("Thread", "Thread unlocked", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@thread_group.sub_command(name="rename", description="Rename a thread")
async def thread_rename(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Error", "This command only works in a thread", "error"), ephemeral=True)
    old = inter.channel.name
    await inter.channel.edit(name=name)
    await _respond(inter, simple("Thread", f"Renamed: {old} -> {name}", "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /channel (management)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="channel",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def channel_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@channel_group.sub_command(name="clone", description="Clone a channel")
async def channel_clone(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    cloned = await ch.clone(reason=f"Cloned by {inter.author}")
    await _respond(
        inter,
        simple("Channel Cloned", f"Created {cloned.mention} (clone of {ch.mention})", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@channel_group.sub_command(name="rename", description="Rename a channel")
async def channel_rename(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    old = ch.name
    await ch.edit(name=name)
    await _respond(
        inter,
        simple("Channel Renamed", f"#{old} -> #{name}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@channel_group.sub_command(name="topic", description="Set channel topic")
async def channel_topic(
    inter: disnake.ApplicationCommandInteraction,
    topic: str,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    await ch.edit(topic=topic)
    await _respond(
        inter,
        simple("Topic Set", f"#{ch.name}: {truncate(topic, 100)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@channel_group.sub_command(name="create", description="Create a text channel")
async def channel_create(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
    category: disnake.CategoryChannel | None = None,
) -> None:
    assert inter.guild is not None
    ch = await inter.guild.create_text_channel(name=name, category=category)
    await _respond(
        inter,
        simple("Channel Created", f"{ch.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@channel_group.sub_command(name="delete", description="Delete a channel")
async def channel_delete(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    reason: str = "Not specified",
) -> None:
    name = channel.name
    await channel.delete(reason=reason)
    await _respond(
        inter,
        simple("Channel Deleted", f"#{name}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /massnick
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="massnick",
    description="Mass nickname change",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def massnick_cmd(
    inter: disnake.ApplicationCommandInteraction,
    action: str = commands.Param(choices=["set", "reset"]),
    nickname: str | None = None,
) -> None:
    assert inter.guild is not None
    if action == "set" and not nickname:
        return await _respond(inter, simple("Error", "Specify a nickname to set", "error"), ephemeral=True)
    await inter.response.defer(ephemeral=True)
    changed = 0
    errors = 0
    for member in inter.guild.members:
        if member.bot or member.id == inter.guild.owner_id:
            continue
        try:
            if action == "set":
                await member.edit(nick=nickname)
            else:
                await member.edit(nick=None)
            changed += 1
        except disnake.Forbidden:
            errors += 1
    verb = "set" if action == "set" else "reset"
    text = f"Nickname {verb} for {changed} members"
    if errors:
        text += f"\nErrors: {errors}"
    await inter.followup.send(
        components=simple("Massnick", text, "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /permissions
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="permissions",
    description="Check member permissions in a channel",
    contexts=GUILD_ONLY,
)
async def permissions_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member | None = None,
    channel: disnake.TextChannel | None = None,
) -> None:
    member = member or inter.author  # type: ignore[assignment]
    ch = channel or inter.channel
    assert isinstance(member, disnake.Member)
    assert isinstance(ch, disnake.TextChannel)

    perms = ch.permissions_for(member)
    allowed: list[str] = []
    denied: list[str] = []

    perm_labels = {
        "view_channel": "View Channel",
        "send_messages": "Send Messages",
        "manage_messages": "Manage Messages",
        "manage_channels": "Manage Channel",
        "embed_links": "Embed Links",
        "attach_files": "Attach Files",
        "add_reactions": "Add Reactions",
        "use_external_emojis": "Use External Emojis",
        "mention_everyone": "Mention Everyone",
        "read_message_history": "Read Message History",
        "manage_threads": "Manage Threads",
        "create_public_threads": "Create Public Threads",
        "send_messages_in_threads": "Send Messages in Threads",
    }

    for perm, label in perm_labels.items():
        if getattr(perms, perm, False):
            allowed.append(label)
        else:
            denied.append(label)

    fields: list[tuple[str, str]] = [
        ("Member", f"{member.mention}"),
        ("Channel", ch.mention),
    ]
    if allowed:
        fields.append(("Allowed", ", ".join(allowed)))
    if denied:
        fields.append(("Denied", ", ".join(denied)))

    await _respond(inter, card("Permissions", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /rolehierarchy
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="rolehierarchy",
    description="Show role hierarchy",
    contexts=GUILD_ONLY,
)
async def rolehierarchy_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    roles = sorted(inter.guild.roles, key=lambda r: r.position, reverse=True)
    items: list[str] = []
    for i, role in enumerate(roles[:30]):
        count = len(role.members)
        items.append(f"**[{role.position}]** {role.mention} -- {count} members")
    footer = f"Total: {len(inter.guild.roles)} roles"
    if len(roles) > 30:
        footer += " (showing first 30)"
    await _respond(inter, list_card("Role Hierarchy", items, "info", footer), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /boosts
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="boosts",
    description="Server boost information",
    contexts=GUILD_ONLY,
)
async def boosts_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    boosters = [m for m in inter.guild.members if m.premium_since]
    boosters.sort(key=lambda m: m.premium_since or datetime.min.replace(tzinfo=timezone.utc))

    fields: list[tuple[str, str]] = [
        ("Boost level", str(inter.guild.premium_tier)),
        ("Boost count", str(inter.guild.premium_subscription_count or 0)),
        ("Boosters", str(len(boosters))),
    ]

    if boosters:
        booster_list = "\n".join(
            f"{m.mention} -- since {disnake.utils.format_dt(m.premium_since, 'R')}"
            for m in boosters[:10]
        )
        fields.append(("Boosters", booster_list))

    await _respond(inter, card("Server Boosts", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /schedule (scheduled messages)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="schedule",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def schedule_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@schedule_group.sub_command(name="message", description="Schedule a message")
async def schedule_message(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    delay: str = commands.Param(description="Duration (10m / 2h / 1d)"),
    text: str = commands.Param(description="Message text"),
) -> None:
    dur = parse_dur(delay)
    if dur is None:
        return await _respond(inter, simple("Error", "Invalid time format", "error"), ephemeral=True)
    scheduled, flush = save("scheduled")
    gk = _guild_key(inter.guild)
    scheduled.setdefault(gk, [])
    scheduled[gk].append({
        "channel_id": str(channel.id),
        "text": text,
        "send_at": now_ts() + int(dur.total_seconds()),
        "author": str(inter.author.id),
        "ts": now_iso(),
    })
    flush()
    await _respond(
        inter,
        simple("Scheduled", f"Message will be sent to {channel.mention} in {fmt_dur(dur)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@schedule_group.sub_command(name="list", description="List scheduled messages")
async def schedule_list(inter: disnake.ApplicationCommandInteraction) -> None:
    scheduled = _load("scheduled.json", {})
    gk = _guild_key(inter.guild)
    items_data = scheduled.get(gk, [])
    now = now_ts()
    pending = [s for s in items_data if s["send_at"] > now]
    if not pending:
        return await _respond(inter, simple("Schedule", "No scheduled messages", "info"), ephemeral=True)
    items: list[str] = []
    for i, s in enumerate(pending, 1):
        remaining = s["send_at"] - now
        items.append(f"**#{i}** <#{s['channel_id']}> | in {fmt_dur(timedelta(seconds=remaining))} | {truncate(s['text'], 50)}")
    await _respond(inter, list_card("Scheduled Messages", items, "info", f"Total: {len(pending)}"), ephemeral=True)


@tasks.loop(seconds=30)
async def scheduled_check() -> None:
    scheduled, flush = save("scheduled")
    now = now_ts()
    changed = False
    for gk in list(scheduled):
        guild = bot.get_guild(int(gk))
        if guild is None:
            continue
        remaining: list[dict] = []
        for s in scheduled[gk]:
            if now >= s["send_at"]:
                ch = guild.get_channel(int(s["channel_id"]))
                if ch and isinstance(ch, disnake.TextChannel):
                    try:
                        await ch.send(
                            components=simple("Uma Enjoyers HQ", s["text"], "info"),
                            flags=MessageFlags(is_components_v2=True),
                        )
                    except Exception:
                        pass
                changed = True
            else:
                remaining.append(s)
        scheduled[gk] = remaining
    if changed:
        flush()


@scheduled_check.before_loop
async def before_scheduled_check() -> None:
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
#  /backup, /restore (guild config)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="backup",
    description="Export bot settings",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def backup_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    gk = _guild_key(inter.guild)
    backup_data: dict[str, Any] = {}
    for key in ["config", "warnings", "cases", "notes", "customcmds", "autoresponders", "reactionroles", "stickies"]:
        data = _load(f"{key}.json", {})
        if gk in data:
            backup_data[key] = data[gk]

    export = json.dumps(backup_data, ensure_ascii=False, indent=2)
    file = disnake.File(io.StringIO(export), filename=f"haven-backup-{inter.guild.id}.json")
    await _respond(
        inter,
        simple("Backup", f"Settings exported ({len(export)} bytes)", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
        file=file,
    )


@bot.slash_command(
    name="restore",
    description="Import bot settings from a file",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def restore_cmd(
    inter: disnake.ApplicationCommandInteraction,
    file: disnake.Attachment,
) -> None:
    assert inter.guild is not None
    if not file.filename.endswith(".json"):
        return await _respond(inter, simple("Error", "File must be .json", "error"), ephemeral=True)
    content = await file.read()
    try:
        backup_data = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return await _respond(inter, simple("Error", "Invalid JSON format", "error"), ephemeral=True)

    gk = _guild_key(inter.guild)
    restored = 0
    for key, value in backup_data.items():
        if key in ["config", "warnings", "cases", "notes", "customcmds", "autoresponders", "reactionroles", "stickies"]:
            data = _load(f"{key}.json", {})
            data[gk] = value
            _save(f"{key}.json", data)
            restored += 1

    await _respond(
        inter,
        simple("Restored", f"Imported {restored} settings categories", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /milestone
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="milestones", description="Set up member milestones")
async def setup_milestones(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "milestone_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Milestones channel: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


_MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]


# ═══════════════════════════════════════════════════════════════════════════
#  /color
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="color",
    description="Color information (hex)",
    contexts=GUILD_ONLY,
)
async def color_cmd(
    inter: disnake.ApplicationCommandInteraction,
    hex_color: str = commands.Param(description="Hex color (e.g., #ff5733 or ff5733)"),
) -> None:
    clean = hex_color.lstrip("#")
    if len(clean) != 6:
        return await _respond(inter, simple("Error", "Invalid format (use 6 hex characters)", "error"), ephemeral=True)
    try:
        value = int(clean, 16)
    except ValueError:
        return await _respond(inter, simple("Error", "Invalid hex code", "error"), ephemeral=True)

    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF

    fields = [
        ("HEX", f"#{clean.upper()}"),
        ("RGB", f"{r}, {g}, {b}"),
        ("Decimal", str(value)),
    ]
    await _respond(
        inter,
        [disnake.ui.Container(
            disnake.ui.TextDisplay(f"### Color: #{clean.upper()}"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(f"**HEX:** #{clean.upper()}\n**RGB:** {r}, {g}, {b}\n**Decimal:** {value}"),
            accent_colour=Color(value),
        )],
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /membercount
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="membercount",
    description="Server member statistics",
    contexts=GUILD_ONLY,
)
async def membercount_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    total = inter.guild.member_count or 0
    bots = sum(1 for m in inter.guild.members if m.bot)
    humans = total - bots
    online = sum(1 for m in inter.guild.members if m.status != disnake.Status.offline and not m.bot)
    boosters = sum(1 for m in inter.guild.members if m.premium_since)

    fields = [
        ("Total", str(total)),
        ("Humans", str(humans)),
        ("Bots", str(bots)),
        ("Online", str(online)),
        ("Boosters", str(boosters)),
    ]
    await _respond(inter, card("Members", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /firstmessage
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="firstmessage",
    description="Link to the first message in a channel",
    contexts=GUILD_ONLY,
)
async def firstmessage_cmd(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    async for msg in ch.history(limit=1, oldest_first=True):
        await _respond(
            inter,
            simple("First Message", f"[Jump to message]({msg.jump_url})\n**Author:** {msg.author}\n**Date:** {fmt_ts(msg.created_at)}", "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
        return
    await _respond(inter, simple("Error", "No messages found", "error"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /topic
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="topic",
    description="Show or set channel topic",
    contexts=GUILD_ONLY,
)
async def topic_cmd(
    inter: disnake.ApplicationCommandInteraction,
    new_topic: str | None = None,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    if new_topic is not None:
        if not inter.author.guild_permissions.manage_channels:  # type: ignore[union-attr]
            return await _respond(inter, simple("Error", "Insufficient permissions", "error"), ephemeral=True)
        await inter.channel.edit(topic=new_topic)
        await _respond(
            inter,
            simple("Topic Set", truncate(new_topic, 200), "ok", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    else:
        topic_text = inter.channel.topic or "No topic set"
        await _respond(
            inter,
            simple(f"Topic #{inter.channel.name}", topic_text, "info", "Uma Enjoyers HQ"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Sticky message handler (in on_message integration)
# ═══════════════════════════════════════════════════════════════════════════

_sticky_counter: dict[int, int] = defaultdict(int)
STICKY_INTERVAL = 5


# ═══════════════════════════════════════════════════════════════════════════
#  Custom command + autoresponder handler (integrated into on_message)
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: disnake.Message) -> None:  # type: ignore[no-redef]
    if message.author.bot or not message.guild:
        return

    cfg = get_cfg(message.guild)

    # Sticky messages
    stickies = _load("stickies.json", {})
    gk = _guild_key(message.guild)
    ch_str = str(message.channel.id)
    sticky_data = stickies.get(gk, {}).get(ch_str)
    if sticky_data:
        _sticky_counter[message.channel.id] += 1
        if _sticky_counter[message.channel.id] >= STICKY_INTERVAL:
            _sticky_counter[message.channel.id] = 0
            old_id = sticky_data.get("message_id")
            if old_id:
                try:
                    old_msg = await message.channel.fetch_message(int(old_id))
                    await old_msg.delete()
                except Exception:
                    pass
            try:
                new_msg = await message.channel.send(
                    components=simple("Sticky Message", sticky_data["text"], "info", "Uma Enjoyers HQ"),
                    flags=MessageFlags(is_components_v2=True),
                )
                stickies_w, flush = save("stickies")
                if gk in stickies_w and ch_str in stickies_w[gk]:
                    stickies_w[gk][ch_str]["message_id"] = str(new_msg.id)
                    flush()
            except Exception:
                pass

    # Custom commands
    ccmds = _load("customcmds.json", {})
    guild_cmds = ccmds.get(gk, {})
    msg_lower = message.content.lower().strip()
    if msg_lower in guild_cmds:
        cmd_data = guild_cmds[msg_lower]
        try:
            await message.channel.send(
                components=simple("Uma Enjoyers HQ", cmd_data["response"], "info"),
                flags=MessageFlags(is_components_v2=True),
            )
            ccmds_w, flush = save("customcmds")
            if gk in ccmds_w and msg_lower in ccmds_w[gk]:
                ccmds_w[gk][msg_lower]["uses"] = ccmds_w[gk][msg_lower].get("uses", 0) + 1
                flush()
        except Exception:
            pass

    # Auto-responders
    ar = _load("autoresponders.json", {})
    guild_ar = ar.get(gk, [])
    for entry in guild_ar:
        trigger = entry["trigger"]
        if entry.get("exact") and msg_lower == trigger:
            try:
                await message.channel.send(
                    components=simple("Uma Enjoyers HQ", entry["response"], "info"),
                    flags=MessageFlags(is_components_v2=True),
                )
            except Exception:
                pass
            break
        elif not entry.get("exact") and trigger in msg_lower:
            try:
                await message.channel.send(
                    components=simple("Uma Enjoyers HQ", entry["response"], "info"),
                    flags=MessageFlags(is_components_v2=True),
                )
            except Exception:
                pass
            break

    # Counting game
    counting_channel = cfg.get("counting_channel")
    if counting_channel and str(message.channel.id) == counting_channel:
        current = cfg.get("counting_current", 0)
        last_user = cfg.get("counting_last_user")

        try:
            num = int(message.content.strip())
        except ValueError:
            await message.delete()
            return

        if str(message.author.id) == last_user:
            await message.delete()
            try:
                await message.channel.send(
                    components=simple("Counting", f"{message.author.mention}, you can't count twice in a row!", "warn"),
                    flags=MessageFlags(is_components_v2=True),
                    delete_after=5,
                )
            except Exception:
                pass
            return

        if num != current + 1:
            set_cfg(message.guild, "counting_current", 0)
            set_cfg(message.guild, "counting_last_user", None)
            try:
                await message.channel.send(
                    components=simple(
                        "Counting",
                        f"{message.author.mention} got it wrong! The correct number was **{current + 1}**. Count reset.",
                        "error",
                    ),
                    flags=MessageFlags(is_components_v2=True),
                )
            except Exception:
                pass
            return

        set_cfg(message.guild, "counting_current", num)
        set_cfg(message.guild, "counting_last_user", str(message.author.id))
        try:
            await message.add_reaction("\u2705")
        except Exception:
            pass
        return

    # AFK
    assert isinstance(message.author, disnake.Member)
    _handle_afk_return(message)
    await _handle_afk_mentions(message)

    if message.author.guild_permissions.administrator:
        return

    # Auto-moderation
    automod = get_automod_cfg(message.guild)

    if automod.get("anti_spam"):
        now_time = time.time()
        uid = message.author.id
        _spam_cache[uid] = [t for t in _spam_cache[uid] if now_time - t < ANTI_SPAM_WINDOW]
        _spam_cache[uid].append(now_time)
        if len(_spam_cache[uid]) >= ANTI_SPAM_THRESHOLD:
            _spam_cache[uid].clear()
            try:
                await message.author.timeout(
                    duration=timedelta(minutes=5),
                    reason="Auto-moderation: spam",
                )
                add_case(message.guild, message.author, "auto-mute", message.guild.me, "Spam (auto-moderation)", "5m")
                await message.channel.send(
                    components=simple(
                        "Auto-Moderation",
                        f"{message.author.mention} has been timed out for spam",
                        "warn",
                        "Uma Enjoyers HQ",
                    ),
                    flags=MessageFlags(is_components_v2=True),
                )
            except disnake.Forbidden:
                pass
            return

    if automod.get("anti_caps") and len(message.content) >= CAPS_MIN_LENGTH:
        upper = sum(1 for c in message.content if c.isupper())
        if upper / len(message.content) >= CAPS_THRESHOLD:
            try:
                await message.delete()
                await message.channel.send(
                    components=simple(
                        "Auto-Moderation",
                        f"{message.author.mention}, do not use excessive capital letters",
                        "warn",
                        "Uma Enjoyers HQ",
                    ),
                    flags=MessageFlags(is_components_v2=True),
                )
            except disnake.Forbidden:
                pass
            return

    if automod.get("anti_invites") and INVITE_PATTERN.search(message.content):
        try:
            await message.delete()
            await message.channel.send(
                components=simple(
                    "Auto-Moderation",
                    f"{message.author.mention}, invite links are not allowed",
                    "warn",
                    "Uma Enjoyers HQ",
                ),
                flags=MessageFlags(is_components_v2=True),
            )
        except disnake.Forbidden:
            pass
        return

    if automod.get("anti_links") and URL_PATTERN.search(message.content):
        try:
            await message.delete()
            await message.channel.send(
                components=simple(
                    "Auto-Moderation",
                    f"{message.author.mention}, links are not allowed",
                    "warn",
                    "Uma Enjoyers HQ",
                ),
                flags=MessageFlags(is_components_v2=True),
            )
        except disnake.Forbidden:
            pass
        return

    if automod.get("anti_mentions"):
        max_mentions = automod.get("max_mentions", MAX_MENTIONS_PER_MSG)
        if len(message.mentions) > max_mentions:
            try:
                await message.delete()
                await message.channel.send(
                    components=simple(
                        "Auto-Moderation",
                        f"{message.author.mention}, too many mentions ({len(message.mentions)}/{max_mentions})",
                        "warn",
                        "Uma Enjoyers HQ",
                    ),
                    flags=MessageFlags(is_components_v2=True),
                )
            except disnake.Forbidden:
                pass
            return


# ═══════════════════════════════════════════════════════════════════════════
#  Milestone check on member join
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_member_join(member: disnake.Member) -> None:  # type: ignore[no-redef]
    cfg = get_cfg(member.guild)

    # Autorole
    autorole_id = cfg.get("autorole")
    if autorole_id:
        role = member.guild.get_role(int(autorole_id))
        if role:
            try:
                await member.add_roles(role, reason="Auto-role")
            except disnake.Forbidden:
                pass

    # Welcome DM
    welcome_dm = cfg.get("welcome_dm")
    if welcome_dm:
        text = welcome_dm.replace("{user}", member.mention).replace("{server}", member.guild.name)
        try:
            await member.send(
                components=simple(f"Uma Enjoyers HQ -- {member.guild.name}", text, "ok"),
                flags=MessageFlags(is_components_v2=True),
            )
        except disnake.Forbidden:
            pass

    # Anti-alt detection
    account_age = datetime.now(timezone.utc) - member.created_at
    automod = cfg.get("automod", {})
    if automod.get("anti_raid") and account_age < timedelta(days=7):
        await send_log(
            member.guild,
            card(
                "Suspicious Account",
                [
                    ("Member", f"{member.mention} ({member.id})"),
                    ("Account age", fmt_dur(account_age)),
                ],
                "warn",
                "Uma Enjoyers HQ | Possible alt account",
            ),
        )

    # Anti-raid
    now_time = time.time()
    gid = member.guild.id
    _join_cache[gid] = [t for t in _join_cache[gid] if now_time - t < ANTI_RAID_WINDOW]
    _join_cache[gid].append(now_time)
    if automod.get("anti_raid") and len(_join_cache[gid]) >= ANTI_RAID_THRESHOLD:
        _join_cache[gid].clear()
        await send_log(
            member.guild,
            simple(
                "Anti-Raid",
                f"Possible raid detected! {ANTI_RAID_THRESHOLD} joins in {ANTI_RAID_WINDOW} seconds.",
                "error",
                "Uma Enjoyers HQ",
            ),
        )

    # Welcome message
    ch_id = cfg.get("welcome_channel")
    if ch_id:
        ch = member.guild.get_channel(int(ch_id))
        if ch and isinstance(ch, disnake.TextChannel):
            avatar_url = member.display_avatar.url
            comps = profile_card(
                "Welcome!",
                [
                    f"{member.mention} joined the server.",
                    f"**Member #{member.guild.member_count}**",
                    f"**Account created:** {disnake.utils.format_dt(member.created_at, 'R')} ({fmt_dur(account_age)} ago)",
                ],
                avatar_url,
                "ok",
                f"Uma Enjoyers HQ | {member.guild.name}",
            )
            await ch.send(components=comps, flags=MessageFlags(is_components_v2=True))

    # Milestone check
    milestone_ch_id = cfg.get("milestone_channel")
    if milestone_ch_id and member.guild.member_count in _MILESTONES:
        ms_ch = member.guild.get_channel(int(milestone_ch_id))
        if ms_ch and isinstance(ms_ch, disnake.TextChannel):
            await ms_ch.send(
                components=simple(
                    "Milestone Reached!",
                    f"The server now has **{member.guild.member_count}** members!",
                    "ok",
                    "Uma Enjoyers HQ",
                ),
                flags=MessageFlags(is_components_v2=True),
            )


# ═══════════════════════════════════════════════════════════════════════════
#  /rules (full rules system)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="rules",
    contexts=GUILD_ONLY,
)
async def rules_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@rules_group.sub_command(name="add", description="Add a rule")
@commands.has_permissions(administrator=True)
async def rules_add(
    inter: disnake.ApplicationCommandInteraction,
    title: str = commands.Param(description="Rule title"),
    text: str = commands.Param(description="Rule text"),
) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    rules_data.setdefault(gk, {"rules": [], "accept_role": None, "channel": None})
    rules_data[gk]["rules"].append({
        "title": title,
        "text": text,
        "author": str(inter.author.id),
        "ts": now_iso(),
    })
    flush()
    num = len(rules_data[gk]["rules"])
    await _respond(
        inter,
        simple("Rule Added", f"Rule #{num}: **{title}**\n{truncate(text, 200)}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@rules_group.sub_command(name="remove", description="Remove a rule by number")
@commands.has_permissions(administrator=True)
async def rules_remove(
    inter: disnake.ApplicationCommandInteraction,
    number: int = commands.Param(ge=1, description="Rule number"),
) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    items = rules_data.get(gk, {}).get("rules", [])
    if number > len(items) or number < 1:
        return await _respond(inter, simple("Error", f"Rule #{number} not found", "error"), ephemeral=True)
    removed = items.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple("Rule Removed", f"Removed: **{removed['title']}**", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@rules_group.sub_command(name="edit", description="Edit a rule")
@commands.has_permissions(administrator=True)
async def rules_edit(
    inter: disnake.ApplicationCommandInteraction,
    number: int = commands.Param(ge=1, description="Rule number"),
    title: str | None = None,
    text: str | None = None,
) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    items = rules_data.get(gk, {}).get("rules", [])
    if number > len(items) or number < 1:
        return await _respond(inter, simple("Error", f"Rule #{number} not found", "error"), ephemeral=True)
    if title:
        items[number - 1]["title"] = title
    if text:
        items[number - 1]["text"] = text
    flush()
    await _respond(
        inter,
        simple("Rule Updated", f"Rule #{number} updated", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@rules_group.sub_command(name="list", description="Show all rules")
async def rules_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    rules_data = _load("rules.json", {})
    gk = _guild_key(inter.guild)
    items = rules_data.get(gk, {}).get("rules", [])
    if not items:
        return await _respond(inter, simple("Rules", "No rules on this server", "info"), ephemeral=True)
    lines: list[str] = []
    for i, r in enumerate(items, 1):
        lines.append(f"**Rule #{i}: {r['title']}**\n{r['text']}")
    await _respond(
        inter,
        list_card("Server Rules", lines, "info", f"Total rules: {len(items)}"),
        ephemeral=True,
    )


@rules_group.sub_command(name="display", description="Send rules to a channel")
@commands.has_permissions(administrator=True)
async def rules_display(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
) -> None:
    assert inter.guild is not None
    target = channel or inter.channel
    assert isinstance(target, disnake.TextChannel)

    rules_data = _load("rules.json", {})
    gk = _guild_key(inter.guild)
    rd = rules_data.get(gk, {})
    items = rd.get("rules", [])
    if not items:
        return await _respond(inter, simple("Error", "Add rules first using /rules add", "error"), ephemeral=True)

    children: list[Any] = [
        disnake.ui.TextDisplay(f"# {inter.guild.name} Server Rules"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
    ]
    for i, r in enumerate(items, 1):
        children.append(disnake.ui.TextDisplay(f"### Rule {i}. {r['title']}\n{r['text']}"))
        if i < len(items):
            children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))

    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    children.append(disnake.ui.TextDisplay(f"-# Total rules: {len(items)} | {inter.guild.name}"))

    accept_role_id = rd.get("accept_role")
    components: list[Any] = [
        disnake.ui.Container(*children, accent_colour=Color(COLORS["info"])),
    ]
    if accept_role_id:
        components.append(
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    style=disnake.ButtonStyle.secondary,
                    label="Accept Rules",
                    custom_id="rules_accept",
                ),
            )
        )

    await target.send(components=components, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Done", f"Rules sent to {target.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@rules_group.sub_command(name="setrole", description="Set role granted when rules are accepted")
@commands.has_permissions(administrator=True)
async def rules_setrole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    rules_data.setdefault(gk, {"rules": [], "accept_role": None, "channel": None})
    rules_data[gk]["accept_role"] = str(role.id)
    flush()
    await _respond(
        inter,
        simple("Setup", f"Rule acceptance role: {role.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


_DEFAULT_RULES: list[dict[str, str]] = [
    {
        "title": "Respect for Members",
        "text": "Insults, harassment, humiliation, and discrimination of any kind are prohibited. "
                "Treat others the way you would like to be treated.",
    },
    {
        "title": "No Spam or Flooding",
        "text": "Do not send repetitive messages, meaningless text, excessive characters or stickers. "
                "Do not abuse mentions (@everyone, @here, roles).",
    },
    {
        "title": "No NSFW Content",
        "text": "All 18+ content is strictly prohibited in all server channels, including avatars, "
                "nicknames, statuses, and media files.",
    },
    {
        "title": "No Advertising",
        "text": "Advertising servers, channels, websites, products, and services is prohibited without "
                "admin permission. This includes links sent via DMs to members.",
    },
    {
        "title": "Use Channels Properly",
        "text": "Use channels for their intended purpose. Do not clutter topic-specific channels "
                "with irrelevant messages. Read channel descriptions.",
    },
    {
        "title": "Personal Information",
        "text": "Publishing personal data of others (doxxing) is prohibited: real names, "
                "addresses, phone numbers, photos without consent.",
    },
    {
        "title": "Nicknames and Avatars",
        "text": "Nicknames must be readable and not contain offensive language. "
                "Nicknames impersonating moderators or other members are prohibited.",
    },
    {
        "title": "Voice Channels",
        "text": "Do not use voice changers, soundboards, or music without the consent "
                "of other channel members. Do not channel-hop.",
    },
    {
        "title": "Compliance with Moderation",
        "text": "Moderator decisions are final. If you disagree with a decision, "
                "use tickets or modmail. Public disputes with moderators are prohibited.",
    },
    {
        "title": "Punishment Evasion",
        "text": "Creating alternative accounts to evade a ban or mute will result in "
                "a permanent ban of all associated accounts.",
    },
]


@rules_group.sub_command(name="default", description="Load default rules for a community server")
@commands.has_permissions(administrator=True)
async def rules_default(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    rules_data.setdefault(gk, {"rules": [], "accept_role": None, "channel": None})
    ts = now_iso()
    aid = str(inter.author.id)
    for r in _DEFAULT_RULES:
        rules_data[gk]["rules"].append({
            "title": r["title"],
            "text": r["text"],
            "author": aid,
            "ts": ts,
        })
    flush()
    total = len(rules_data[gk]["rules"])
    await _respond(
        inter,
        simple(
            "Rules Loaded",
            f"Added {len(_DEFAULT_RULES)} default rules (total: {total}).\n"
            "Use /rules display to send them to a channel.",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@rules_group.sub_command(name="clear", description="Delete all rules")
@commands.has_permissions(administrator=True)
async def rules_clear(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    rules_data, flush = save("rules")
    gk = _guild_key(inter.guild)
    if gk in rules_data:
        rules_data[gk]["rules"] = []
        flush()
    await _respond(inter, simple("Rules", "All rules deleted", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@bot.listen("on_button_click")
async def on_rules_accept(inter: disnake.MessageInteraction) -> None:
    if inter.component.custom_id != "rules_accept":
        return
    assert inter.guild is not None and isinstance(inter.author, disnake.Member)
    rules_data = _load("rules.json", {})
    gk = _guild_key(inter.guild)
    rd = rules_data.get(gk, {})
    role_id = rd.get("accept_role")
    if not role_id:
        return await _respond(
            inter, simple("Error", "Rule acceptance role not configured", "error"), ephemeral=True
        )
    role = inter.guild.get_role(int(role_id))
    if not role:
        return await _respond(inter, simple("Error", "Role not found", "error"), ephemeral=True)
    if role in inter.author.roles:
        return await _respond(inter, simple("Rules", "You have already accepted the rules", "info"), ephemeral=True)
    try:
        await inter.author.add_roles(role, reason="Rules accepted")
    except disnake.Forbidden:
        return await _respond(inter, simple("Error", "Bot has insufficient permissions", "error"), ephemeral=True)
    await _respond(
        inter,
        simple("Rules Accepted", f"You have been given the role {role.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /warnthresholds (auto-actions on warn count)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="warnthresholds",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def warnthresholds_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@warnthresholds_group.sub_command(name="set", description="Set auto-action for warning count")
async def warnthresholds_set(
    inter: disnake.ApplicationCommandInteraction,
    count: int = commands.Param(ge=1, le=50, description="Number of warnings"),
    action: str = commands.Param(
        choices=["mute_1h", "mute_6h", "mute_1d", "mute_7d", "kick", "ban"],
        description="Action",
    ),
) -> None:
    assert inter.guild is not None
    thresholds, flush = save("warnthresholds")
    gk = _guild_key(inter.guild)
    thresholds.setdefault(gk, {})
    thresholds[gk][str(count)] = action
    flush()
    action_labels = {
        "mute_1h": "Mute 1 hour",
        "mute_6h": "Mute 6 hours",
        "mute_1d": "Mute 1 day",
        "mute_7d": "Mute 7 days",
        "kick": "Kick",
        "ban": "Ban",
    }
    await _respond(
        inter,
        simple(
            "Threshold Set",
            f"At **{count}** warnings: **{action_labels.get(action, action)}**",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@warnthresholds_group.sub_command(name="remove", description="Remove a threshold")
async def warnthresholds_remove(
    inter: disnake.ApplicationCommandInteraction,
    count: int = commands.Param(ge=1),
) -> None:
    assert inter.guild is not None
    thresholds, flush = save("warnthresholds")
    gk = _guild_key(inter.guild)
    if gk in thresholds and str(count) in thresholds[gk]:
        del thresholds[gk][str(count)]
        flush()
    await _respond(inter, simple("Threshold Removed", f"Threshold for {count} warnings removed", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@warnthresholds_group.sub_command(name="list", description="Show thresholds")
async def warnthresholds_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    thresholds = _load("warnthresholds.json", {})
    gk = _guild_key(inter.guild)
    gt = thresholds.get(gk, {})
    if not gt:
        return await _respond(inter, simple("Thresholds", "No thresholds configured", "info"), ephemeral=True)
    action_labels = {
        "mute_1h": "Mute 1h", "mute_6h": "Mute 6h", "mute_1d": "Mute 1d",
        "mute_7d": "Mute 7d", "kick": "Kick", "ban": "Ban",
    }
    items = [f"**{c} warnings** -> {action_labels.get(a, a)}" for c, a in sorted(gt.items(), key=lambda x: int(x[0]))]
    await _respond(inter, list_card("Warning Thresholds", items, "info", f"Total: {len(gt)}"), ephemeral=True)


async def _check_warn_threshold(guild: disnake.Guild, member: disnake.Member) -> None:
    thresholds = _load("warnthresholds.json", {})
    gk = _guild_key(guild)
    gt = thresholds.get(gk, {})
    if not gt:
        return
    warnings = _load("warnings.json", {})
    user_warns = warnings.get(gk, {}).get(str(member.id), [])
    warn_count = len(user_warns)

    for count_str, action in sorted(gt.items(), key=lambda x: int(x[0])):
        if warn_count >= int(count_str):
            try:
                if action.startswith("mute_"):
                    durations = {"mute_1h": 1, "mute_6h": 6, "mute_1d": 24, "mute_7d": 168}
                    hours = durations.get(action, 1)
                    await member.timeout(duration=timedelta(hours=hours), reason=f"Auto: {warn_count} warnings")
                    add_case(guild, member, "auto-mute", guild.me, f"Auto-mute: {warn_count} warnings", f"{hours}h")
                elif action == "kick":
                    await member.kick(reason=f"Auto-kick: {warn_count} warnings")
                    add_case(guild, member, "auto-kick", guild.me, f"Auto-kick: {warn_count} warnings")
                elif action == "ban":
                    await member.ban(reason=f"Auto-ban: {warn_count} warnings")
                    add_case(guild, member, "auto-ban", guild.me, f"Auto-ban: {warn_count} warnings")
                await send_log(
                    guild,
                    card(
                        "Auto-Action on Warnings",
                        [
                            ("Member", f"{member.mention}"),
                            ("Warnings", str(warn_count)),
                            ("Action", action),
                        ],
                        "error",
                        "Uma Enjoyers HQ",
                    ),
                )
            except disnake.Forbidden:
                pass
            break


# ═══════════════════════════════════════════════════════════════════════════
#  /dm
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="dm",
    description="Send a DM to a member as the bot",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def dm_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    text: str,
) -> None:
    assert inter.guild is not None
    success = await _try_dm(
        member,
        simple(f"Uma Enjoyers HQ -- {inter.guild.name}", text, "info"),
    )
    if success:
        await _respond(
            inter,
            simple("DM Sent", f"Message sent to {member.mention}", "ok", "Uma Enjoyers HQ"),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            simple("Error", "Could not send DM (DMs are closed)", "error"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /cleanup
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="cleanup",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def cleanup_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@cleanup_group.sub_command(name="bots", description="Delete bot messages")
async def cleanup_bots(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(limit=amount, check=lambda m: m.author.bot)
    await inter.followup.send(
        components=simple("Cleanup", f"Deleted {len(deleted)} bot messages", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@cleanup_group.sub_command(name="links", description="Delete messages with links")
async def cleanup_links(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(
        limit=amount,
        check=lambda m: bool(URL_PATTERN.search(m.content)) if m.content else False,
    )
    await inter.followup.send(
        components=simple("Cleanup", f"Deleted {len(deleted)} messages with links", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@cleanup_group.sub_command(name="images", description="Delete messages with attachments")
async def cleanup_images(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(
        limit=amount,
        check=lambda m: bool(m.attachments),
    )
    await inter.followup.send(
        components=simple("Cleanup", f"Deleted {len(deleted)} messages with attachments", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@cleanup_group.sub_command(name="contains", description="Delete messages containing text")
async def cleanup_contains(
    inter: disnake.ApplicationCommandInteraction,
    text: str,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    lower = text.lower()
    deleted = await inter.channel.purge(
        limit=amount,
        check=lambda m: lower in m.content.lower() if m.content else False,
    )
    await inter.followup.send(
        components=simple("Cleanup", f"Deleted {len(deleted)} messages containing `{truncate(text, 50)}`", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@cleanup_group.sub_command(name="embeds", description="Delete messages with embeds")
async def cleanup_embeds(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(
        limit=amount,
        check=lambda m: bool(m.embeds),
    )
    await inter.followup.send(
        components=simple("Cleanup", f"Deleted {len(deleted)} messages with embeds", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /slowmodeinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="slowmodeinfo",
    description="Show slowmode in all channels",
    contexts=GUILD_ONLY,
)
async def slowmodeinfo_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    channels_with_sm = [
        (ch.mention, ch.slowmode_delay)
        for ch in inter.guild.text_channels
        if ch.slowmode_delay > 0
    ]
    if not channels_with_sm:
        return await _respond(inter, simple("Slowmode", "No channels with slowmode", "info"), ephemeral=True)
    items = [f"{ch} -- {delay}s" for ch, delay in channels_with_sm]
    await _respond(
        inter,
        list_card("Channels with Slowmode", items, "info", f"Total: {len(channels_with_sm)}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /rolecolor
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="rolecolor",
    description="Change role color",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_roles=True),
)
async def rolecolor_cmd(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
    hex_color: str = commands.Param(description="Color in hex (e.g., ff5733)"),
) -> None:
    clean = hex_color.lstrip("#")
    try:
        value = int(clean, 16)
    except ValueError:
        return await _respond(inter, simple("Error", "Invalid hex code", "error"), ephemeral=True)
    await role.edit(colour=Color(value))
    await _respond(
        inter,
        simple("Role Color", f"Color of {role.mention} changed to #{clean.upper()}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /emojisteal
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="emojisteal",
    description="Copy an emoji to the server by link",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_emojis=True),
)
async def emojisteal_cmd(
    inter: disnake.ApplicationCommandInteraction,
    emoji: str = commands.Param(description="Emoji or image URL"),
    name: str = commands.Param(description="Name for the emoji"),
) -> None:
    assert inter.guild is not None
    import re as _re
    match = _re.match(r"<(a?):(\w+):(\d+)>", emoji)
    if match:
        animated = bool(match.group(1))
        eid = int(match.group(3))
        ext = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=128"
    elif emoji.startswith("http"):
        url = emoji
    else:
        return await _respond(inter, simple("Error", "Provide an emoji or URL", "error"), ephemeral=True)

    await inter.response.defer(ephemeral=True)
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return await inter.followup.send(
                    components=simple("Error", "Could not download image", "error"),
                    flags=MessageFlags(is_components_v2=True),
                    ephemeral=True,
                )
            data = await resp.read()

    try:
        created = await inter.guild.create_custom_emoji(name=name, image=data)
        await inter.followup.send(
            components=simple("Emoji Added", f"{created} -- `:{created.name}:`", "ok", "Uma Enjoyers HQ"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    except disnake.Forbidden:
        await inter.followup.send(
            components=simple("Error", "Insufficient permissions to add emoji", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    except disnake.HTTPException as e:
        await inter.followup.send(
            components=simple("Error", f"Discord error: {truncate(str(e), 200)}", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /oldestmembers, /newestmembers
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="oldestmembers",
    description="Oldest members on the server",
    contexts=GUILD_ONLY,
)
async def oldestmembers_cmd(
    inter: disnake.ApplicationCommandInteraction,
    count: int = commands.Param(default=10, ge=1, le=25),
) -> None:
    assert inter.guild is not None
    members = sorted(
        [m for m in inter.guild.members if not m.bot and m.joined_at],
        key=lambda m: m.joined_at or datetime.min.replace(tzinfo=timezone.utc),
    )[:count]
    items = [
        f"**{i+1}.** {m.mention} -- joined {disnake.utils.format_dt(m.joined_at, 'R')}"
        for i, m in enumerate(members)
        if m.joined_at
    ]
    await _respond(
        inter,
        list_card("Oldest Members", items, "info", f"Top {len(items)}"),
        ephemeral=True,
    )


@bot.slash_command(
    name="newestmembers",
    description="Newest members on the server",
    contexts=GUILD_ONLY,
)
async def newestmembers_cmd(
    inter: disnake.ApplicationCommandInteraction,
    count: int = commands.Param(default=10, ge=1, le=25),
) -> None:
    assert inter.guild is not None
    members = sorted(
        [m for m in inter.guild.members if not m.bot and m.joined_at],
        key=lambda m: m.joined_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:count]
    items = [
        f"**{i+1}.** {m.mention} -- joined {disnake.utils.format_dt(m.joined_at, 'R')}"
        for i, m in enumerate(members)
        if m.joined_at
    ]
    await _respond(
        inter,
        list_card("Newest Members", items, "info", f"Top {len(items)}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /rolecount, /channelcount, /botlist
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="rolecount",
    description="Member count for each role",
    contexts=GUILD_ONLY,
)
async def rolecount_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    roles = sorted(inter.guild.roles[1:], key=lambda r: len(r.members), reverse=True)[:20]
    items = [f"{r.mention} -- {len(r.members)} members" for r in roles]
    footer = f"Total roles: {len(inter.guild.roles)}"
    await _respond(inter, list_card("Roles by Members", items, "info", footer), ephemeral=True)


@bot.slash_command(
    name="channelcount",
    description="Channel statistics",
    contexts=GUILD_ONLY,
)
async def channelcount_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    text = sum(1 for c in inter.guild.channels if isinstance(c, disnake.TextChannel))
    voice = sum(1 for c in inter.guild.channels if isinstance(c, disnake.VoiceChannel))
    cats = sum(1 for c in inter.guild.channels if isinstance(c, disnake.CategoryChannel))
    stage = sum(1 for c in inter.guild.channels if isinstance(c, disnake.StageChannel))
    forum = sum(1 for c in inter.guild.channels if isinstance(c, disnake.ForumChannel))
    total = len(inter.guild.channels)
    fields = [
        ("Total", str(total)),
        ("Text", str(text)),
        ("Voice", str(voice)),
        ("Categories", str(cats)),
        ("Stage", str(stage)),
        ("Forums", str(forum)),
    ]
    await _respond(inter, card("Channel Statistics", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


@bot.slash_command(
    name="botlist",
    description="List bots on the server",
    contexts=GUILD_ONLY,
)
async def botlist_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    bots = [m for m in inter.guild.members if m.bot]
    if not bots:
        return await _respond(inter, simple("Bots", "No bots on the server", "info"), ephemeral=True)
    items = [
        f"{m.mention} ({m.name}) -- {'online' if m.status != disnake.Status.offline else 'offline'}"
        for m in sorted(bots, key=lambda b: b.name.lower())
    ]
    await _respond(
        inter,
        list_card("Bots on Server", items, "info", f"Total: {len(bots)}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /antilink whitelist
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="whitelist",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def whitelist_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@whitelist_group.sub_command(name="add", description="Add a domain to the link whitelist")
async def whitelist_add(
    inter: disnake.ApplicationCommandInteraction,
    domain: str = commands.Param(description="Domain (e.g., youtube.com)"),
) -> None:
    assert inter.guild is not None
    wl, flush = save("whitelist")
    gk = _guild_key(inter.guild)
    wl.setdefault(gk, [])
    d = domain.lower().strip()
    if d not in wl[gk]:
        wl[gk].append(d)
        flush()
    await _respond(
        inter,
        simple("Whitelist", f"`{d}` added to whitelist", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@whitelist_group.sub_command(name="remove", description="Remove a domain from whitelist")
async def whitelist_remove(
    inter: disnake.ApplicationCommandInteraction,
    domain: str,
) -> None:
    assert inter.guild is not None
    wl, flush = save("whitelist")
    gk = _guild_key(inter.guild)
    d = domain.lower().strip()
    if gk in wl and d in wl[gk]:
        wl[gk].remove(d)
        flush()
    await _respond(inter, simple("Whitelist", f"`{d}` removed from whitelist", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@whitelist_group.sub_command(name="list", description="Show domain whitelist")
async def whitelist_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    wl = _load("whitelist.json", {})
    gk = _guild_key(inter.guild)
    domains = wl.get(gk, [])
    if not domains:
        return await _respond(inter, simple("Whitelist", "Whitelist is empty", "info"), ephemeral=True)
    items = [f"`{d}`" for d in domains]
    await _respond(
        inter,
        list_card("Domain Whitelist", items, "info", f"Total: {len(domains)}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /modmail
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="modmail", description="Set up modmail channel")
async def setup_modmail(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "modmail_channel", str(channel.id))
    await _respond(
        inter,
        simple("Setup", f"Modmail channel: {channel.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.slash_command(
    name="modmail",
    description="Send an anonymous message to moderators",
    contexts=GUILD_ONLY,
)
async def modmail_cmd(
    inter: disnake.ApplicationCommandInteraction,
    message: str,
) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    ch_id = cfg.get("modmail_channel")
    if not ch_id:
        return await _respond(
            inter,
            simple("Error", "Modmail not set up. Ask an admin to run /setup modmail", "error"),
            ephemeral=True,
        )
    ch = inter.guild.get_channel(int(ch_id))
    if not ch or not isinstance(ch, disnake.TextChannel):
        return await _respond(inter, simple("Error", "Modmail channel not found", "error"), ephemeral=True)

    modmail_data, flush = save("modmail")
    gk = _guild_key(inter.guild)
    modmail_data.setdefault(gk, [])
    ticket_id = len(modmail_data[gk]) + 1
    modmail_data[gk].append({
        "id": ticket_id,
        "author": str(inter.author.id),
        "message": message,
        "ts": now_iso(),
        "status": "open",
        "replies": [],
    })
    flush()

    c = card(
        f"Modmail #{ticket_id}",
        [
            ("Message", message),
            ("Status", "Open"),
        ],
        "info",
        f"Uma Enjoyers HQ | Reply: /modmailreply {ticket_id} <reply>",
    )
    c_with_buttons = c + [
        disnake.ui.ActionRow(
            disnake.ui.Button(
                style=disnake.ButtonStyle.secondary,
                label=f"Reply #{ticket_id}",
                custom_id=f"modmail_reply_{ticket_id}",
            ),
            disnake.ui.Button(
                style=disnake.ButtonStyle.danger,
                label="Close",
                custom_id=f"modmail_close_{ticket_id}",
            ),
        ),
    ]
    await ch.send(components=c_with_buttons, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Modmail", f"Your message #{ticket_id} has been sent to moderators", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@bot.slash_command(
    name="modmailreply",
    description="Reply to a modmail message",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def modmailreply_cmd(
    inter: disnake.ApplicationCommandInteraction,
    ticket_id: int,
    reply: str,
) -> None:
    assert inter.guild is not None
    modmail_data, flush = save("modmail")
    gk = _guild_key(inter.guild)
    tickets = modmail_data.get(gk, [])
    ticket = next((t for t in tickets if t["id"] == ticket_id), None)
    if not ticket:
        return await _respond(inter, simple("Error", f"Message #{ticket_id} not found", "error"), ephemeral=True)

    ticket["replies"].append({
        "author": str(inter.author.id),
        "text": reply,
        "ts": now_iso(),
    })
    flush()

    author_id = int(ticket["author"])
    member = inter.guild.get_member(author_id)
    if member:
        await _try_dm(
            member,
            card(
                f"Reply to message #{ticket_id}",
                [
                    ("Your message", truncate(ticket["message"], 200)),
                    ("Reply", reply),
                    ("Moderator", inter.author.display_name),
                ],
                "info",
                f"Uma Enjoyers HQ | {inter.guild.name}",
            ),
        )

    await _respond(
        inter,
        simple("Reply Sent", f"Reply to message #{ticket_id} sent via DM to author", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /inviteinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="inviteinfo",
    description="Invite information",
    contexts=GUILD_ONLY,
)
async def inviteinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    code: str = commands.Param(description="Invite code or full link"),
) -> None:
    code = code.replace("https://discord.gg/", "").replace("https://discord.com/invite/", "").strip("/")
    try:
        invite = await bot.fetch_invite(code, with_counts=True)
    except disnake.NotFound:
        return await _respond(inter, simple("Error", "Invite not found", "error"), ephemeral=True)
    except disnake.HTTPException:
        return await _respond(inter, simple("Error", "Could not retrieve information", "error"), ephemeral=True)

    fields: list[tuple[str, str]] = [
        ("Code", invite.code),
        ("Server", invite.guild.name if invite.guild else "Unknown"),
    ]
    if invite.guild:
        fields.append(("Server ID", str(invite.guild.id)))
    if invite.approximate_member_count:
        fields.append(("Members", str(invite.approximate_member_count)))
    if invite.approximate_presence_count:
        fields.append(("Online", str(invite.approximate_presence_count)))
    if invite.inviter:
        fields.append(("Creator", str(invite.inviter)))
    if invite.channel:
        fields.append(("Channel", str(invite.channel)))
    if invite.max_age:
        fields.append(("Expires", f"{invite.max_age // 3600}h" if invite.max_age >= 3600 else f"{invite.max_age}s"))
    else:
        fields.append(("Expires", "Never"))
    if invite.max_uses:
        fields.append(("Max uses", str(invite.max_uses)))
    fields.append(("Uses", str(invite.uses or 0)))

    await _respond(inter, card("Invite Info", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /serversettings
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="serversettings",
    description="Show server settings",
    contexts=GUILD_ONLY,
)
async def serversettings_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    g = inter.guild
    fields: list[tuple[str, str]] = [
        ("Name", g.name),
        ("ID", str(g.id)),
        ("Owner", str(g.owner)),
        ("Verification level", str(g.verification_level)),
        ("Content filter", str(g.explicit_content_filter)),
        ("Notifications", str(g.default_notifications)),
        ("MFA for moderation", "Yes" if g.mfa_level else "No"),
        ("Boost level", str(g.premium_tier)),
        ("Boosts", str(g.premium_subscription_count or 0)),
        ("Max members", str(g.max_members or "Unknown")),
        ("Upload limit", f"{g.filesize_limit // (1024*1024)} MB"),
        ("System channel", g.system_channel.mention if g.system_channel else "None"),
        ("Rules channel", g.rules_channel.mention if g.rules_channel else "None"),
        ("AFK channel", f"{g.afk_channel.mention} ({g.afk_timeout // 60} min)" if g.afk_channel else "None"),
    ]
    features = ", ".join(g.features[:10]) if g.features else "None"
    fields.append(("Features", features))

    await _respond(inter, card("Server Settings", fields, "info", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /messagestats
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="messagestats",
    description="Channel message statistics (last N messages)",
    contexts=GUILD_ONLY,
)
async def messagestats_cmd(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
    limit: int = commands.Param(default=500, ge=50, le=2000),
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)

    author_counts: dict[str, int] = defaultdict(int)
    total = 0
    bot_count = 0

    async for msg in ch.history(limit=limit):
        total += 1
        author_counts[msg.author.display_name] += 1
        if msg.author.bot:
            bot_count += 1

    top = sorted(author_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    items = [f"**{name}** -- {count} msg(s) ({count*100//total}%)" for name, count in top]

    fields_text = "\n".join(items) if items else "No data"
    c = card(
        f"Statistics #{ch.name}",
        [
            ("Checked", str(total)),
            ("Unique authors", str(len(author_counts))),
            ("Bot messages", str(bot_count)),
            ("Top authors", fields_text),
        ],
        "info",
        "Uma Enjoyers HQ",
    )
    await inter.followup.send(components=c, flags=MessageFlags(is_components_v2=True), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /createinvite
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="createinvite",
    description="Create a server invite",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(create_instant_invite=True),
)
async def createinvite_cmd(
    inter: disnake.ApplicationCommandInteraction,
    max_age: int = commands.Param(default=0, description="Duration in seconds (0 = permanent)"),
    max_uses: int = commands.Param(default=0, description="Max uses (0 = unlimited)"),
    temporary: bool = commands.Param(default=False, description="Temporary membership"),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    invite = await inter.channel.create_invite(
        max_age=max_age,
        max_uses=max_uses,
        temporary=temporary,
        reason=f"Created by {inter.author}",
    )
    age_text = f"{max_age}s" if max_age else "Permanent"
    uses_text = str(max_uses) if max_uses else "Unlimited"
    fields = [
        ("Link", f"https://discord.gg/{invite.code}"),
        ("Channel", inter.channel.mention),
        ("Expires", age_text),
        ("Max uses", uses_text),
        ("Temporary", "Yes" if temporary else "No"),
    ]
    await _respond(inter, card("Invite Created", fields, "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /embed (create custom Components V2 message)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="embed",
    description="Create a custom message",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def embed_cmd(
    inter: disnake.ApplicationCommandInteraction,
    title: str,
    body: str,
    color: str = commands.Param(
        default="info",
        choices=["plain", "ok", "warn", "error", "info"],
        description="Accent color",
    ),
    channel: disnake.TextChannel | None = None,
    footer: str | None = None,
) -> None:
    target = channel or inter.channel
    assert isinstance(target, disnake.TextChannel)

    children: list[Any] = [
        disnake.ui.TextDisplay(f"### {title}"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
        disnake.ui.TextDisplay(body),
    ]
    if footer:
        children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
        children.append(disnake.ui.TextDisplay(f"-# {footer}"))

    await target.send(
        components=[disnake.ui.Container(*children, accent_colour=Color(COLORS[color]))],
        flags=MessageFlags(is_components_v2=True),
    )
    await _respond(
        inter,
        simple("Sent", f"Message sent to {target.mention}", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /antinuke
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="antinuke",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def antinuke_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@antinuke_group.sub_command(name="enable", description="Enable anti-nuke protection")
async def antinuke_enable(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    set_cfg(inter.guild, "antinuke", True)
    await _respond(
        inter,
        simple(
            "Anti-Nuke",
            "Protection enabled. The bot will monitor mass channel/role deletions and mass bans.",
            "ok",
            "Uma Enjoyers HQ",
        ),
        ephemeral=True,
    )


@antinuke_group.sub_command(name="disable", description="Disable anti-nuke protection")
async def antinuke_disable(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    set_cfg(inter.guild, "antinuke", False)
    await _respond(inter, simple("Anti-Nuke", "Protection disabled", "warn", "Uma Enjoyers HQ"), ephemeral=True)


@antinuke_group.sub_command(name="status", description="Anti-nuke protection status")
async def antinuke_status(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    enabled = cfg.get("antinuke", False)
    status = "Enabled" if enabled else "Disabled"
    await _respond(
        inter,
        simple("Anti-Nuke", f"Status: **{status}**", "info" if enabled else "warn", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@antinuke_group.sub_command(name="trusted", description="Add a trusted user")
async def antinuke_trusted(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    assert inter.guild is not None
    trusted, flush = save("antinuke_trusted")
    gk = _guild_key(inter.guild)
    trusted.setdefault(gk, [])
    uid = str(member.id)
    if uid not in trusted[gk]:
        trusted[gk].append(uid)
        flush()
    await _respond(
        inter,
        simple("Anti-Nuke", f"{member.mention} added to trusted", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


_nuke_action_cache: dict[str, list[float]] = defaultdict(list)
NUKE_THRESHOLD = 5
NUKE_WINDOW = 10


@bot.event
async def on_guild_channel_delete(channel: disnake.abc.GuildChannel) -> None:
    guild = channel.guild
    cfg = get_cfg(guild)
    if not cfg.get("antinuke"):
        return
    if not guild.me.guild_permissions.view_audit_log:
        return

    try:
        async for entry in guild.audit_logs(limit=1, action=disnake.AuditLogAction.channel_delete):
            if entry.user and not entry.user.bot and entry.user.id != guild.owner_id:
                trusted = _load("antinuke_trusted.json", {}).get(_guild_key(guild), [])
                if str(entry.user.id) in trusted:
                    return
                key = f"cd:{guild.id}:{entry.user.id}"
                now_time = time.time()
                _nuke_action_cache[key] = [t for t in _nuke_action_cache[key] if now_time - t < NUKE_WINDOW]
                _nuke_action_cache[key].append(now_time)
                if len(_nuke_action_cache[key]) >= NUKE_THRESHOLD:
                    _nuke_action_cache[key].clear()
                    member = guild.get_member(entry.user.id)
                    if member:
                        try:
                            await member.ban(reason="Anti-nuke: mass channel deletion")
                            add_case(guild, member, "auto-ban", guild.me, "Anti-nuke: mass channel deletion")
                        except disnake.Forbidden:
                            pass
                    await send_log(
                        guild,
                        card(
                            "Anti-Nuke: Mass Channel Deletion",
                            [
                                ("Offender", str(entry.user)),
                                ("Action", "Banned (auto)"),
                            ],
                            "error",
                            "Uma Enjoyers HQ",
                        ),
                    )
            break
    except disnake.Forbidden:
        pass

    await send_log(
        guild,
        simple(
            "Channel Deleted",
            f"**Channel:** {channel.name}\n**Type:** {channel.type}",
            "warn",
        ),
    )


@bot.event
async def on_guild_role_delete(role: disnake.Role) -> None:
    guild = role.guild
    cfg = get_cfg(guild)
    if not cfg.get("antinuke"):
        await send_log(guild, simple("Role Deleted", f"**Role:** {role.name} ({role.id})", "warn"))
        return
    if not guild.me.guild_permissions.view_audit_log:
        await send_log(guild, simple("Role Deleted", f"**Role:** {role.name} ({role.id})", "warn"))
        return

    try:
        async for entry in guild.audit_logs(limit=1, action=disnake.AuditLogAction.role_delete):
            if entry.user and not entry.user.bot and entry.user.id != guild.owner_id:
                trusted = _load("antinuke_trusted.json", {}).get(_guild_key(guild), [])
                if str(entry.user.id) in trusted:
                    break
                key = f"rd:{guild.id}:{entry.user.id}"
                now_time = time.time()
                _nuke_action_cache[key] = [t for t in _nuke_action_cache[key] if now_time - t < NUKE_WINDOW]
                _nuke_action_cache[key].append(now_time)
                if len(_nuke_action_cache[key]) >= NUKE_THRESHOLD:
                    _nuke_action_cache[key].clear()
                    member = guild.get_member(entry.user.id)
                    if member:
                        try:
                            await member.ban(reason="Anti-nuke: mass role deletion")
                            add_case(guild, member, "auto-ban", guild.me, "Anti-nuke: mass role deletion")
                        except disnake.Forbidden:
                            pass
                    await send_log(
                        guild,
                        card(
                            "Anti-Nuke: Mass Role Deletion",
                            [
                                ("Offender", str(entry.user)),
                                ("Action", "Banned (auto)"),
                            ],
                            "error",
                            "Uma Enjoyers HQ",
                        ),
                    )
            break
    except disnake.Forbidden:
        pass

    await send_log(guild, simple("Role Deleted", f"**Role:** {role.name} ({role.id})", "warn"))


@bot.event
async def on_member_ban(guild: disnake.Guild, user: disnake.User) -> None:
    cfg = get_cfg(guild)
    if not cfg.get("antinuke"):
        await send_log(guild, simple("Ban", f"**Member:** {user} ({user.id})", "error"))
        return
    if not guild.me.guild_permissions.view_audit_log:
        await send_log(guild, simple("Ban", f"**Member:** {user} ({user.id})", "error"))
        return

    try:
        async for entry in guild.audit_logs(limit=1, action=disnake.AuditLogAction.ban):
            if entry.user and not entry.user.bot and entry.user.id != guild.owner_id:
                trusted = _load("antinuke_trusted.json", {}).get(_guild_key(guild), [])
                if str(entry.user.id) in trusted:
                    break
                key = f"ban:{guild.id}:{entry.user.id}"
                now_time = time.time()
                _nuke_action_cache[key] = [t for t in _nuke_action_cache[key] if now_time - t < NUKE_WINDOW]
                _nuke_action_cache[key].append(now_time)
                if len(_nuke_action_cache[key]) >= NUKE_THRESHOLD:
                    _nuke_action_cache[key].clear()
                    member = guild.get_member(entry.user.id)
                    if member:
                        try:
                            await member.ban(reason="Anti-nuke: mass ban")
                            add_case(guild, member, "auto-ban", guild.me, "Anti-nuke: mass ban")
                        except disnake.Forbidden:
                            pass
                    await send_log(
                        guild,
                        card(
                            "Anti-Nuke: Mass Ban",
                            [
                                ("Offender", str(entry.user)),
                                ("Action", "Banned (auto)"),
                            ],
                            "error",
                            "Uma Enjoyers HQ",
                        ),
                    )
            break
    except disnake.Forbidden:
        pass

    await send_log(guild, simple("Ban", f"**Member:** {user} ({user.id})", "error"))


# ═══════════════════════════════════════════════════════════════════════════
#  /selfroles (self-assignable role panel)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="selfroles",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_roles=True),
)
async def selfroles_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@selfroles_group.sub_command(name="create", description="Create a self-assignable role panel")
async def selfroles_create(
    inter: disnake.ApplicationCommandInteraction,
    name: str = commands.Param(description="Panel name"),
    description: str = commands.Param(default="Choose a role from the list below"),
) -> None:
    assert inter.guild is not None
    sr, flush = save("selfroles")
    gk = _guild_key(inter.guild)
    sr.setdefault(gk, {})
    sr[gk][name.lower()] = {
        "name": name,
        "description": description,
        "roles": [],
        "author": str(inter.author.id),
    }
    flush()
    await _respond(
        inter,
        simple("Panel Created", f"Panel `{name}` created. Add roles via /selfroles addrole", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@selfroles_group.sub_command(name="addrole", description="Add a role to a panel")
async def selfroles_addrole(
    inter: disnake.ApplicationCommandInteraction,
    panel: str = commands.Param(description="Panel name"),
    role: disnake.Role = commands.Param(description="Role"),
    label: str | None = None,
) -> None:
    assert inter.guild is not None
    sr, flush = save("selfroles")
    gk = _guild_key(inter.guild)
    if panel.lower() not in sr.get(gk, {}):
        return await _respond(inter, simple("Error", f"Panel `{panel}` not found", "error"), ephemeral=True)
    sr[gk][panel.lower()]["roles"].append({
        "role_id": str(role.id),
        "label": label or role.name,
    })
    flush()
    await _respond(
        inter,
        simple("Role Added", f"{role.mention} added to panel `{panel}`", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


@selfroles_group.sub_command(name="send", description="Send a role panel to a channel")
async def selfroles_send(
    inter: disnake.ApplicationCommandInteraction,
    panel: str,
    channel: disnake.TextChannel | None = None,
) -> None:
    assert inter.guild is not None
    target = channel or inter.channel
    assert isinstance(target, disnake.TextChannel)

    sr = _load("selfroles.json", {})
    gk = _guild_key(inter.guild)
    pd = sr.get(gk, {}).get(panel.lower())
    if not pd:
        return await _respond(inter, simple("Error", f"Panel `{panel}` not found", "error"), ephemeral=True)
    if not pd["roles"]:
        return await _respond(inter, simple("Error", "Panel has no roles", "error"), ephemeral=True)

    options = []
    for rd in pd["roles"]:
        role = inter.guild.get_role(int(rd["role_id"]))
        if role:
            options.append(
                disnake.SelectOption(
                    label=rd["label"],
                    value=str(role.id),
                    description=f"Role: {role.name}",
                )
            )

    if not options:
        return await _respond(inter, simple("Error", "No available roles", "error"), ephemeral=True)

    components = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### {pd['name']}"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(pd["description"]),
            accent_colour=Color(COLORS["info"]),
        ),
        disnake.ui.ActionRow(
            disnake.ui.StringSelect(
                custom_id=f"selfrole_{panel.lower()}",
                placeholder="Choose a role...",
                min_values=0,
                max_values=len(options),
                options=options,
            ),
        ),
    ]
    await target.send(components=components, flags=MessageFlags(is_components_v2=True))
    await _respond(inter, simple("Done", f"Panel sent to {target.mention}", "ok", "Uma Enjoyers HQ"), ephemeral=True)


@bot.listen("on_dropdown")
async def on_selfrole_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id
    if not cid or not cid.startswith("selfrole_"):
        return
    assert inter.guild is not None and isinstance(inter.author, disnake.Member)

    selected = set(inter.values)
    sr = _load("selfroles.json", {})
    gk = _guild_key(inter.guild)
    panel_name = cid[9:]
    pd = sr.get(gk, {}).get(panel_name)
    if not pd:
        return

    all_role_ids = {rd["role_id"] for rd in pd["roles"]}
    added: list[str] = []
    removed: list[str] = []

    for rid in all_role_ids:
        role = inter.guild.get_role(int(rid))
        if not role:
            continue
        if rid in selected and role not in inter.author.roles:
            try:
                await inter.author.add_roles(role, reason="Self-assigned role")
                added.append(role.name)
            except disnake.Forbidden:
                pass
        elif rid not in selected and role in inter.author.roles:
            try:
                await inter.author.remove_roles(role, reason="Self-removed role")
                removed.append(role.name)
            except disnake.Forbidden:
                pass

    parts: list[str] = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if removed:
        parts.append(f"Removed: {', '.join(removed)}")
    if not parts:
        parts.append("No changes")

    await _respond(
        inter,
        simple("Roles Updated", "\n".join(parts), "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /purgeuser
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="purgeuser",
    description="Delete all messages from a user in a channel",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def purgeuser_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    amount: int = commands.Param(ge=1, le=500, default=100),
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.response.defer(ephemeral=True)
    deleted = await inter.channel.purge(limit=amount, check=lambda m: m.author.id == member.id)
    await inter.followup.send(
        components=simple("Purge", f"Deleted {len(deleted)} messages from {member.mention}", "ok", "Uma Enjoyers HQ"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )





# ═══════════════════════════════════════════════════════════════════════════
#  /slowmodepresets
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="slowmodepresets",
    description="Quick slowmode preset",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def slowmodepresets_cmd(
    inter: disnake.ApplicationCommandInteraction,
    preset: str = commands.Param(
        choices=["off", "calm", "moderate", "strict", "lockdown"],
        description="Preset: off(0), calm(5), moderate(15), strict(30), lockdown(120)",
    ),
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    presets = {"off": 0, "calm": 5, "moderate": 15, "strict": 30, "lockdown": 120}
    delay = presets[preset]
    await ch.edit(slowmode_delay=delay)
    if delay == 0:
        text = f"Slowmode disabled in {ch.mention}"
    else:
        text = f"Slowmode in {ch.mention}: {delay}s ({preset})"
    await _respond(inter, simple("Slowmode", text, "ok", "Uma Enjoyers HQ"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /joinposition
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="joinposition",
    description="Show member join position",
    contexts=GUILD_ONLY,
)
async def joinposition_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member | None = None,
) -> None:
    assert inter.guild is not None
    target = member or inter.author
    assert isinstance(target, disnake.Member)
    members = sorted(
        [m for m in inter.guild.members if m.joined_at],
        key=lambda m: m.joined_at or datetime.min.replace(tzinfo=timezone.utc),
    )
    position = next((i + 1 for i, m in enumerate(members) if m.id == target.id), None)
    total = len(members)
    if position:
        await _respond(
            inter,
            simple(
                "Join Position",
                f"{target.mention} was **#{position}** out of **{total}** members to join",
                "info",
                "Uma Enjoyers HQ",
            ),
            ephemeral=True,
        )
    else:
        await _respond(inter, simple("Error", "Could not determine position", "error"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /exportwarns
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="exportwarns",
    description="Export all warnings to a file",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def exportwarns_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    warns = _load("warnings.json", {})
    gk = _guild_key(inter.guild)
    guild_warns = warns.get(gk, {})
    if not guild_warns:
        return await _respond(inter, simple("Export", "No warnings to export", "info"), ephemeral=True)

    lines: list[str] = ["Member ID | Reason | Moderator | Date"]
    for uid, warn_list in guild_warns.items():
        for w in warn_list:
            lines.append(f"{uid} | {w.get('reason', '-')} | {w.get('mod', '-')} | {w.get('ts', '-')}")

    content = "\n".join(lines)
    file = disnake.File(io.StringIO(content), filename=f"warns-{inter.guild.id}.txt")
    await _respond(
        inter,
        simple("Export Warnings", f"Exported {sum(len(v) for v in guild_warns.values())} warnings", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
        file=file,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /exportcases
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="exportcases",
    description="Export all cases to a file",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def exportcases_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    cases = _load("cases.json", {})
    gk = _guild_key(inter.guild)
    guild_cases = cases.get(gk, {})
    if not guild_cases:
        return await _respond(inter, simple("Export", "No cases to export", "info"), ephemeral=True)

    lines: list[str] = ["# | Type | Member | Moderator | Reason | Duration | Date"]
    for uid, case_list in guild_cases.items():
        for c_item in case_list:
            lines.append(
                f"#{c_item.get('id', '?')} | {c_item.get('action', '-')} | {uid} | "
                f"{c_item.get('mod', '-')} | {c_item.get('reason', '-')} | "
                f"{c_item.get('duration', '-')} | {c_item.get('ts', '-')}"
            )

    content = "\n".join(lines)
    total = sum(len(v) for v in guild_cases.values())
    file = disnake.File(io.StringIO(content), filename=f"cases-{inter.guild.id}.txt")
    await _respond(
        inter,
        simple("Export Cases", f"Exported {total} cases", "ok", "Uma Enjoyers HQ"),
        ephemeral=True,
        file=file,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  BOT READY + LAUNCH
# ═══════════════════════════════════════════════════════════════════════════

_bot_start_time = datetime.now(timezone.utc)



@bot.event
async def on_ready() -> None:
    _json_load_all()
    print(f"Uma Enjoyers HQ v{VERSION} is online as {bot.user} ({bot.user.id})")  # type: ignore[union-attr]
    print(f"Guilds: {len(bot.guilds)} | Commands: {len(list(bot.all_slash_commands))}")
    print(f"Storage: JSON files ({_DATA_DIR})")
    if not tempban_check.is_running():
        tempban_check.start()
    if not reminder_check.is_running():
        reminder_check.start()
    if not giveaway_check.is_running():
        giveaway_check.start()
    if not scheduled_check.is_running():
        scheduled_check.start()
    await bot.change_presence(
        activity=disnake.Activity(
            type=disnake.ActivityType.watching,
            name=f"{len(bot.guilds)} servers",
        )
    )


def main() -> None:
    token = os.environ.get("HAVEN_TOKEN")
    if not token:
        print("HAVEN_TOKEN is not set")
        raise SystemExit(1)
    bot.run(token)


if __name__ == "__main__":
    main()
