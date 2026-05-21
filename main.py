"""Haven -- Discord moderation bot (disnake >= 2.11, Components V2).

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
import io
import json
import os
import random
import re
import string
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

VERSION = "4.0.0"
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

DATA = Path("data")
DATA.mkdir(exist_ok=True)

# In-memory caches for anti-spam / anti-raid
_spam_cache: dict[int, list[float]] = defaultdict(list)
_join_cache: dict[int, list[float]] = defaultdict(list)
_afk_users: dict[str, dict[str, str]] = {}  # guild:user -> {message, ts}
_snipe_cache: dict[int, dict[str, Any]] = {}
_editsnipe_cache: dict[int, dict[str, Any]] = {}

# ═══════════════════════════════════════════════════════════════════════════
#  STORAGE
# ═══════════════════════════════════════════════════════════════════════════


def _load(path: str, default: Any = None) -> Any:
    fp = DATA / path
    if fp.exists():
        with fp.open("r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def _save(path: str, data: Any) -> None:
    fp = DATA / path
    with fp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save(key: str) -> tuple[dict, Any]:
    """Return (data, flush) for a storage key."""
    path = f"{key}.json"
    data = _load(path, {})

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
        return "0с"
    parts: list[str] = []
    for label, div in (("д", 86400), ("ч", 3600), ("м", 60), ("с", 1)):
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
        disnake.Status.online: "В сети",
        disnake.Status.idle: "Не активен",
        disnake.Status.dnd: "Не беспокоить",
        disnake.Status.offline: "Не в сети",
        disnake.Status.invisible: "Невидимый",
    }
    return mapping.get(status, "Неизвестно")


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


@setup_group.sub_command(name="logs", description="Канал для логов модерации")
async def setup_logs(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "log_channel", str(channel.id))
    await _respond(
        inter,
        simple("Настройка", f"Лог-канал установлен: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="welcome", description="Канал приветствий")
async def setup_welcome(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "welcome_channel", str(channel.id))
    await _respond(
        inter,
        simple("Настройка", f"Канал приветствий: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="leave", description="Канал прощаний")
async def setup_leave(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "leave_channel", str(channel.id))
    await _respond(
        inter,
        simple("Настройка", f"Канал прощаний: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="modrole", description="Роль модератора")
async def setup_modrole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "mod_role", str(role.id))
    await _respond(
        inter,
        simple("Настройка", f"Роль модератора: {role.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="muterole", description="Роль мута (дополнительная)")
async def setup_muterole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "mute_role", str(role.id))
    await _respond(
        inter,
        simple("Настройка", f"Роль мута: {role.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="tickets", description="Отправить панель тикетов в канал")
async def setup_tickets(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "ticket_panel_channel", str(channel.id))

    panel = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("### Система тикетов"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                "Выберите тип обращения ниже.\n"
                "Будет создан приватный канал для общения с командой модерации.\n\n"
                "Пожалуйста, опишите вашу проблему максимально подробно после открытия тикета."
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(label="Вопрос", custom_id="ticket:question", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Жалоба", custom_id="ticket:complaint", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Апелляция", custom_id="ticket:appeal", style=ButtonStyle.secondary),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(label="Баг", custom_id="ticket:bug", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Партнерство", custom_id="ticket:partnership", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Другое", custom_id="ticket:other", style=ButtonStyle.danger),
            ),
            accent_colour=Color(COLORS["plain"]),
        )
    ]

    await channel.send(components=panel, flags=MessageFlags(is_components_v2=True))
    await _respond(
        inter,
        simple("Настройка", f"Панель тикетов отправлена в {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@setup_group.sub_command(name="info", description="Текущие настройки сервера")
async def setup_info(inter: disnake.ApplicationCommandInteraction) -> None:
    cfg = get_cfg(inter.guild)
    automod = cfg.get("automod", {})

    fields: list[tuple[str, str]] = []
    for key, label in [
        ("log_channel", "Лог-канал"),
        ("welcome_channel", "Канал приветствий"),
        ("leave_channel", "Канал прощаний"),
        ("ticket_panel_channel", "Панель тикетов"),
        ("mod_role", "Роль модератора"),
        ("mute_role", "Роль мута"),
    ]:
        cid = cfg.get(key)
        if key.endswith("_role"):
            fields.append((label, f"<@&{cid}>" if cid else "Не задана"))
        else:
            fields.append((label, f"<#{cid}>" if cid else "Не задан"))

    am_status = []
    for k, label in [
        ("anti_spam", "Анти-спам"),
        ("anti_raid", "Анти-рейд"),
        ("anti_caps", "Анти-капс"),
        ("anti_links", "Анти-ссылки"),
        ("anti_invites", "Анти-инвайты"),
        ("anti_mentions", "Анти-упоминания"),
    ]:
        val = automod.get(k, False)
        am_status.append(f"{label}: {'вкл' if val else 'выкл'}")

    fields.append(("Авто-модерация", " | ".join(am_status)))

    await _respond(
        inter,
        card("Настройки Haven", fields, "info", f"Haven v{VERSION}"),
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


@automod_group.sub_command(name="toggle", description="Включить/выключить модуль авто-модерации")
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
    state = "включен" if automod[module] else "выключен"
    labels = {
        "anti_spam": "Анти-спам",
        "anti_raid": "Анти-рейд",
        "anti_caps": "Анти-капс",
        "anti_links": "Анти-ссылки",
        "anti_invites": "Анти-инвайты",
        "anti_mentions": "Анти-упоминания",
    }
    await _respond(
        inter,
        simple("Авто-модерация", f"**{labels.get(module, module)}** -- {state}", "ok", "Haven"),
        ephemeral=True,
    )


@automod_group.sub_command(name="status", description="Статус авто-модерации")
async def automod_status(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    automod = get_automod_cfg(inter.guild)
    fields: list[tuple[str, str]] = []
    for k, label in [
        ("anti_spam", "Анти-спам"),
        ("anti_raid", "Анти-рейд"),
        ("anti_caps", "Анти-капс"),
        ("anti_links", "Анти-ссылки"),
        ("anti_invites", "Анти-инвайты"),
        ("anti_mentions", "Анти-упоминания"),
    ]:
        val = automod.get(k, False)
        fields.append((label, "Включен" if val else "Выключен"))
    await _respond(inter, card("Авто-модерация", fields, "info", "Haven"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /mute, /unmute
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="mute",
    description="Тайм-аут участника",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def mute_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Ошибка", "Вы не можете замутить этого участника (иерархия ролей)", "error"),
            ephemeral=True,
        )
    if member.bot:
        return await _respond(
            inter,
            simple("Ошибка", "Нельзя замутить бота", "error"),
            ephemeral=True,
        )

    select_options = [
        SelectOption(label="60 секунд", value="60s"),
        SelectOption(label="5 минут", value="5m"),
        SelectOption(label="10 минут", value="10m"),
        SelectOption(label="30 минут", value="30m"),
        SelectOption(label="1 час", value="1h"),
        SelectOption(label="3 часа", value="3h"),
        SelectOption(label="6 часов", value="6h"),
        SelectOption(label="12 часов", value="12h"),
        SelectOption(label="1 день", value="1d"),
        SelectOption(label="3 дня", value="3d"),
        SelectOption(label="7 дней", value="7d"),
    ]

    warn_count = len(_load("warnings.json", {}).get(_guild_key(inter.guild), {}).get(_user_key(member), []))
    case_count = get_case_count(inter.guild, member)  # type: ignore[arg-type]

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### Тайм-аут: {member.display_name}"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                f"**Участник:** {member.mention} ({member.id})\n"
                f"**Причина:** {reason}\n"
                f"**Варнов:** {warn_count} | **Кейсов:** {case_count}"
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay("Выберите длительность:"),
            disnake.ui.ActionRow(
                disnake.ui.StringSelect(
                    custom_id=f"mute_dur:{member.id}:{reason}",
                    placeholder="Длительность тайм-аута",
                    options=select_options,
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="28 дней",
                    custom_id=f"mute_28d:{member.id}:{reason}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Свое время",
                    custom_id=f"mute_custom:{member.id}:{reason}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Отмена",
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
        await _respond(inter, simple("Ошибка", "Участник не найден на сервере", "error"), ephemeral=True)
        return

    await member.timeout(duration=duration, reason=reason)
    cid = add_case(guild, member, "mute", inter.author, reason, fmt_dur(duration))

    c = card(
        "Тайм-аут применен",
        [
            ("Участник", f"{member.mention} ({member.id})"),
            ("Длительность", fmt_dur(duration)),
            ("Причина", reason),
            ("Модератор", inter.author.mention),
            ("Кейс", f"#{cid}"),
        ],
        "warn",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )

    await _respond(inter, c, ephemeral=True)
    await send_log(guild, c)
    await _try_dm(
        member,
        simple(
            f"Haven -- {guild.name}",
            f"Вам выдан тайм-аут.\n\n"
            f"**Длительность:** {fmt_dur(duration)}\n"
            f"**Причина:** {reason}\n"
            f"**Модератор:** {inter.author}",
            "warn",
            f"Кейс #{cid}",
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
        await _respond(inter, simple("Отменено", "Тайм-аут отменен", "plain"), ephemeral=True)
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
            title="Свое время тайм-аута",
            custom_id=f"mute_modal:{member_id}:{reason}",
            components=[
                disnake.ui.TextInput(
                    label="Длительность (25m / 2h / 7d / 90s)",
                    custom_id="duration",
                    placeholder="Например: 25m",
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
            simple("Ошибка", f"Неверный формат: {raw}. Используйте: 25m / 2h / 7d / 90s", "error"),
            ephemeral=True,
        )
        return
    await _apply_mute(inter, member_id, reason, dur)


@bot.slash_command(
    name="unmute",
    description="Снять тайм-аут",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def unmute_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    await member.timeout(duration=None, reason=f"Снято модератором {inter.author}")
    c = card(
        "Тайм-аут снят",
        [
            ("Участник", f"{member.mention} ({member.id})"),
            ("Модератор", inter.author.mention),
        ],
        "ok",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c, ephemeral=True)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /warn, /warnings, /clearwarns, /delwarn
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="warn",
    description="Выдать предупреждение",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def warn_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Ошибка", "Иерархия ролей не позволяет", "error"),
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
        "Предупреждение",
        [
            ("Участник", f"{member.mention} ({member.id})"),
            ("Причина", reason),
            ("Модератор", inter.author.mention),
            ("Кейс", f"#{cid}"),
            ("Всего варнов", str(warn_count)),
        ],
        "warn",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вам выдано предупреждение.\n\n"
            f"**Причина:** {reason}\n"
            f"**Всего варнов:** {warn_count}\n"
            f"**Модератор:** {inter.author}",
            "warn",
            f"Кейс #{cid}",
        ),
    )


@bot.slash_command(
    name="warnings",
    description="Список предупреждений участника",
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
            simple("Предупреждения", f"У {member.mention} нет предупреждений", "info"),
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
            f"Предупреждения -- {member.display_name}",
            items,
            "warn",
            f"Всего: {_plural(len(warns), 'предупреждение', 'предупреждения', 'предупреждений')}",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="clearwarns",
    description="Очистить все варны участника",
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
            "Варны очищены",
            f"Все предупреждения {member.mention} удалены ({count} шт.)",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Варны очищены",
            f"**Участник:** {member.mention}\n**Удалено:** {count}\n**Модератор:** {inter.author.mention}",
            "info",
        ),
    )


@bot.slash_command(
    name="delwarn",
    description="Удалить конкретный варн по номеру",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def delwarn_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    number: int = commands.Param(ge=1, description="Номер предупреждения"),
) -> None:
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)
    warns = warnings.get(gk, {}).get(uk, [])
    if number > len(warns) or number < 1:
        return await _respond(
            inter,
            simple("Ошибка", f"Варн #{number} не найден", "error"),
            ephemeral=True,
        )
    removed = warns.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple(
            "Варн удален",
            f"Удален варн #{number} у {member.mention}\n**Причина была:** {removed['reason']}",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /kick
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="kick",
    description="Кикнуть участника",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(kick_members=True),
)
async def kick_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            ephemeral=True,
        )
    cid = add_case(inter.guild, member, "kick", inter.author, reason)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вы были кикнуты с сервера.\n\n**Причина:** {reason}\n**Модератор:** {inter.author}",
            "error",
            f"Кейс #{cid}",
        ),
    )
    await member.kick(reason=reason)
    c = card(
        "Кик",
        [
            ("Участник", f"{member} ({member.id})"),
            ("Причина", reason),
            ("Модератор", inter.author.mention),
            ("Кейс", f"#{cid}"),
        ],
        "error",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /softban
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="softban",
    description="Бан + разбан (удаление сообщений без перманентного бана)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def softban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
    delete_days: int = commands.Param(default=1, ge=0, le=7, description="Дней сообщений для удаления"),
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            ephemeral=True,
        )
    cid = add_case(inter.guild, member, "softban", inter.author, reason)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вы были софтбанены (кик с удалением сообщений).\n\n**Причина:** {reason}",
            "error",
            f"Кейс #{cid}",
        ),
    )
    await inter.guild.ban(member, reason=f"Softban: {reason}", delete_message_seconds=delete_days * 86400)  # type: ignore[union-attr]
    await inter.guild.unban(member, reason="Softban unban")  # type: ignore[union-attr]
    c = card(
        "Софтбан",
        [
            ("Участник", f"{member} ({member.id})"),
            ("Причина", reason),
            ("Удалено сообщений за", f"{delete_days} дн."),
            ("Модератор", inter.author.mention),
            ("Кейс", f"#{cid}"),
        ],
        "error",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /ban, /unban, /massban + tempban loop
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="ban",
    description="Забанить участника",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def ban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
    duration: str | None = None,
    delete_messages: int = commands.Param(default=0, ge=0, le=7, description="Дней сообщений для удаления"),
) -> None:
    if not _check_hierarchy(inter, member):
        return await _respond(
            inter,
            simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            ephemeral=True,
        )

    dur_td: timedelta | None = None
    dur_str: str | None = None
    if duration:
        dur_td = parse_dur(duration)
        if dur_td is None:
            return await _respond(
                inter,
                simple("Ошибка", f"Неверный формат длительности: {duration}", "error"),
                ephemeral=True,
            )
        dur_str = fmt_dur(dur_td)

    cid = add_case(inter.guild, member, "ban", inter.author, reason, dur_str)  # type: ignore[arg-type]

    dm_text = f"Вы были забанены на сервере.\n\n**Причина:** {reason}"
    if dur_str:
        dm_text += f"\n**Длительность:** {dur_str}"
    dm_text += f"\n**Модератор:** {inter.author}"

    await _try_dm(
        member,
        simple(f"Haven -- {inter.guild.name}", dm_text, "error", f"Кейс #{cid}"),  # type: ignore[union-attr]
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
        ("Участник", f"{member} ({member.id})"),
        ("Причина", reason),
        ("Модератор", inter.author.mention),
        ("Кейс", f"#{cid}"),
    ]
    if dur_str:
        fields.insert(2, ("Длительность", dur_str))

    c = card("Бан", fields, "error", f"Haven | {fmt_ts(datetime.now(timezone.utc))}")
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="unban",
    description="Разбанить пользователя по ID",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(ban_members=True),
)
async def unban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    user_id: str,
    reason: str = "Не указана",
) -> None:
    try:
        user = await bot.fetch_user(int(user_id))
    except (ValueError, disnake.NotFound):
        return await _respond(
            inter,
            simple("Ошибка", "Пользователь не найден", "error"),
            ephemeral=True,
        )

    try:
        await inter.guild.unban(user, reason=reason)  # type: ignore[union-attr]
    except disnake.NotFound:
        return await _respond(
            inter,
            simple("Ошибка", "Пользователь не найден в списке банов", "error"),
            ephemeral=True,
        )

    tempbans, flush = save("tempbans")
    gk = _guild_key(inter.guild)
    if gk in tempbans and user_id in tempbans[gk]:
        del tempbans[gk][user_id]
        flush()

    c = card(
        "Разбан",
        [
            ("Пользователь", f"{user} ({user.id})"),
            ("Причина", reason),
            ("Модератор", inter.author.mention),
        ],
        "ok",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="massban",
    description="Забанить нескольких пользователей по ID (через пробел)",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def massban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    user_ids: str = commands.Param(description="ID через пробел"),
    reason: str = "Массовый бан",
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

    text = f"**Забанено:** {len(banned)}"
    if failed:
        text += f"\n**Не удалось:** {', '.join(failed)}"
    await inter.followup.send(
        components=simple("Массовый бан", text, "error" if failed else "ok", "Haven"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Массовый бан",
            f"**Забанено:** {len(banned)}\n**Модератор:** {inter.author.mention}\n**Причина:** {reason}",
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
                    await guild.unban(user, reason="Временный бан истек")
                    await send_log(
                        guild,
                        card(
                            "Авто-разбан",
                            [
                                ("Пользователь", f"{user} ({user.id})"),
                                ("Причина бана", entry.get("reason", "N/A")),
                            ],
                            "ok",
                            "Временный бан истек",
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
    description="Удалить сообщения",
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
        "Очистка",
        f"**Канал:** {inter.channel.mention}\n"
        f"**Удалено:** {_plural(len(deleted), 'сообщение', 'сообщения', 'сообщений')}\n"
        f"**Модератор:** {inter.author.mention}"
        + (f"\n**Фильтр участника:** {member.mention}" if member else "")
        + (f"\n**Фильтр текста:** {contains}" if contains else ""),
        "ok",
        "Haven",
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
    description="Медленный режим",
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
        text = f"Медленный режим установлен: {seconds}с в {inter.channel.mention}"
    else:
        text = f"Медленный режим отключен в {inter.channel.mention}"
    await _respond(inter, simple("Slowmode", text, "ok", "Haven"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /lock, /unlock
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="lock",
    description="Заблокировать канал",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def lock_cmd(
    inter: disnake.ApplicationCommandInteraction,
    reason: str = "Не указана",
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = False
    await inter.channel.set_permissions(
        inter.guild.default_role, overwrite=overwrite, reason=reason  # type: ignore[union-attr]
    )
    c = card(
        "Канал заблокирован",
        [
            ("Канал", inter.channel.mention),
            ("Причина", reason),
            ("Модератор", inter.author.mention),
        ],
        "warn",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(
    name="unlock",
    description="Разблокировать канал",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_channels=True),
)
async def unlock_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = None
    await inter.channel.set_permissions(inter.guild.default_role, overwrite=overwrite)  # type: ignore[union-attr]
    c = card(
        "Канал разблокирован",
        [
            ("Канал", inter.channel.mention),
            ("Модератор", inter.author.mention),
        ],
        "ok",
        f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
    )
    await _respond(inter, c)
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
#  /nuke
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="nuke",
    description="Пересоздать канал (удалить все сообщения)",
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
        simple("Nuke", "Канал будет пересоздан через 2 секунды...", "error", "Haven"),
    )

    await send_log(
        inter.guild,
        simple(
            "Nuke",
            f"**Канал:** #{name}\n**Модератор:** {inter.author.mention}",
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
        components=simple("Nuke", "Канал был пересоздан", "error", "Haven"),
        flags=MessageFlags(is_components_v2=True),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /userinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="userinfo",
    description="Информация о пользователе",
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

    roles = ", ".join(r.mention for r in reversed(member.roles[1:])) if len(member.roles) > 1 else "Нет"
    role_count = len(member.roles) - 1

    timeout_text = "Нет"
    if member.current_timeout and member.current_timeout > datetime.now(timezone.utc):
        timeout_text = f"До {disnake.utils.format_dt(member.current_timeout, 'F')}"

    boosting = "Нет"
    if member.premium_since:
        boosting = f"С {disnake.utils.format_dt(member.premium_since, 'F')}"

    perms_list: list[str] = []
    key_perms = [
        ("administrator", "Администратор"),
        ("manage_guild", "Управление сервером"),
        ("manage_channels", "Управление каналами"),
        ("manage_roles", "Управление ролями"),
        ("manage_messages", "Управление сообщениями"),
        ("kick_members", "Кик участников"),
        ("ban_members", "Бан участников"),
        ("moderate_members", "Модерация участников"),
        ("mention_everyone", "Упоминание everyone"),
    ]
    for perm, label in key_perms:
        if getattr(member.guild_permissions, perm, False):
            perms_list.append(label)

    text_lines = [
        f"**Ник:** {member.display_name}",
        f"**Тег:** {member}",
        f"**ID:** {member.id}",
        f"**Бот:** {'Да' if member.bot else 'Нет'}",
        "",
        f"**Аккаунт создан:** {created} ({created_r})",
        f"**Присоединился:** {joined} ({joined_r})",
        f"**Буст:** {boosting}",
        "",
        f"**Тайм-аут:** {timeout_text}",
        f"**Варны:** {warn_count} | **Кейсов:** {case_count}",
        "",
        f"**Роли ({role_count}):** {roles}",
    ]

    if perms_list:
        text_lines.append("")
        text_lines.append(f"**Ключевые права:** {', '.join(perms_list)}")

    avatar_url = member.display_avatar.url

    await _respond(
        inter,
        profile_card(
            f"Информация о {member.display_name}",
            text_lines,
            avatar_url,
            "info",
            f"Haven v{VERSION}",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /avatar
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="avatar",
    description="Показать аватар пользователя",
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
        lines.append(f"[Глобальный аватар]({global_avatar.with_size(1024).url})")
    if guild_avatar:
        lines.append(f"[Серверный аватар]({guild_avatar.with_size(1024).url})")

    await _respond(
        inter,
        profile_card("Аватар", lines, avatar_url, "info"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /banner
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="banner",
    description="Показать баннер пользователя",
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
            profile_card("Баннер", [f"**{member.display_name}**"], banner_url, "info"),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            simple("Баннер", f"У {member.mention} нет баннера", "info"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /serverinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="serverinfo",
    description="Информация о сервере",
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
        ("Владелец", f"{owner.mention}" if owner else "N/A"),
        ("ID", str(guild.id)),
        ("Создан", f"{created} ({created_r})"),
        ("", ""),
        ("Участники", f"Всего: {total} | Люди: {humans} | Боты: {bots}"),
        ("Каналы", f"Текстовых: {text_channels} | Голосовых: {voice_channels} | Категорий: {categories} | Тредов: {threads}"),
        ("Роли", str(roles)),
        ("Эмодзи / Стикеры", f"{emojis} / {stickers}"),
        ("", ""),
        ("Буст", f"Уровень {boost_level} | {_plural(boost_count, 'буст', 'буста', 'бустов')}"),
        ("Верификация", verification),
        ("Кейсов модерации", str(total_cases)),
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
                f"Haven v{VERSION}",
            ),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            card(guild.name, [(k, v) for k, v in fields if k], "info", f"Haven v{VERSION}"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /roleinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="roleinfo",
    description="Информация о роли",
    contexts=GUILD_ONLY,
)
async def roleinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    created = disnake.utils.format_dt(role.created_at, "F")
    colour = f"#{role.colour.value:06x}" if role.colour.value else "По умолчанию"
    mentionable = "Да" if role.mentionable else "Нет"
    hoisted = "Да" if role.hoist else "Нет"
    managed = "Да" if role.managed else "Нет"
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
        ("Цвет", colour),
        ("Участников", str(member_count)),
        ("Позиция", str(position)),
        ("Создана", created),
        ("Упоминаемая", mentionable),
        ("Отображается отдельно", hoisted),
        ("Управляемая", managed),
    ]
    if key_perms:
        fields.append(("Ключевые права", ", ".join(key_perms)))

    await _respond(
        inter,
        card(f"Роль: {role.name}", fields, "info", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /channelinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="channelinfo",
    description="Информация о канале",
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
        ("Категория", ch.category.name if ch.category else "Нет"),
        ("Позиция", str(ch.position)),
        ("Создан", created),
        ("Тема", ch.topic or "Нет"),
        ("NSFW", "Да" if ch.is_nsfw() else "Нет"),
        ("Slowmode", f"{ch.slowmode_delay}с" if ch.slowmode_delay else "Выкл"),
    ]
    await _respond(
        inter,
        card(f"Канал: #{ch.name}", fields, "info", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /history
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="history",
    description="История кейсов участника",
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
            simple("История", f"У {member.mention} нет кейсов модерации", "info"),
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
            f"История -- {member.display_name}",
            items,
            "info",
            f"Всего кейсов: {len(user_cases)} | Показаны последние {len(last_10)}",
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


@note_group.sub_command(name="add", description="Добавить заметку о пользователе")
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
            "Заметка добавлена",
            f"Заметка о {member.mention} сохранена (всего: {count})",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )


@note_group.sub_command(name="list", description="Список заметок о пользователе")
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
            simple("Заметки", f"У {member.mention} нет заметок", "info"),
            ephemeral=True,
        )
    items: list[str] = []
    for i, n in enumerate(user_notes, 1):
        items.append(f"**#{i}** | {n['text']} | {n.get('mod_name', 'N/A')} | {n['ts'][:10]}")
    await _respond(
        inter,
        list_card(
            f"Заметки -- {member.display_name}",
            items,
            "info",
            f"Всего: {len(user_notes)}",
        ),
        ephemeral=True,
    )


@note_group.sub_command(name="clear", description="Очистить все заметки о пользователе")
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
        simple("Заметки очищены", f"Удалено {count} заметок о {member.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@note_group.sub_command(name="delete", description="Удалить конкретную заметку по номеру")
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
            inter, simple("Ошибка", f"Заметка #{number} не найдена", "error"), ephemeral=True
        )
    removed = user_notes.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple("Заметка удалена", f"Удалена заметка #{number}: {truncate(removed['text'], 100)}", "ok", "Haven"),
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


@role_group.sub_command(name="add", description="Выдать роль участнику")
async def role_add(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    role: disnake.Role,
) -> None:
    if role >= inter.author.top_role and inter.author.id != inter.guild.owner_id:  # type: ignore[union-attr]
        return await _respond(
            inter, simple("Ошибка", "Вы не можете выдать роль выше или равную вашей", "error"), ephemeral=True
        )
    await member.add_roles(role, reason=f"Выдано {inter.author}")
    await _respond(
        inter,
        simple("Роль выдана", f"{role.mention} выдана {member.mention}", "ok", "Haven"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Роль выдана", f"**Участник:** {member.mention}\n**Роль:** {role.mention}\n**Модератор:** {inter.author.mention}", "info"),
    )


@role_group.sub_command(name="remove", description="Забрать роль у участника")
async def role_remove(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    role: disnake.Role,
) -> None:
    if role >= inter.author.top_role and inter.author.id != inter.guild.owner_id:  # type: ignore[union-attr]
        return await _respond(
            inter, simple("Ошибка", "Вы не можете забрать роль выше или равную вашей", "error"), ephemeral=True
        )
    await member.remove_roles(role, reason=f"Забрано {inter.author}")
    await _respond(
        inter,
        simple("Роль забрана", f"{role.mention} забрана у {member.mention}", "ok", "Haven"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Роль забрана", f"**Участник:** {member.mention}\n**Роль:** {role.mention}\n**Модератор:** {inter.author.mention}", "info"),
    )


@role_group.sub_command(name="members", description="Список участников с ролью")
async def role_members(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    members = role.members
    if not members:
        return await _respond(
            inter, simple("Роль", f"У {role.mention} нет участников", "info"), ephemeral=True
        )
    member_list = [f"{m.mention} ({m.id})" for m in members[:25]]
    footer = f"Показано {len(member_list)} из {len(members)}" if len(members) > 25 else f"Всего: {len(members)}"
    await _respond(
        inter,
        list_card(f"Участники с ролью {role.name}", member_list, "info", footer),
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


@rr_group.sub_command(name="create", description="Создать панель реакционных ролей")
async def rr_create(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    title: str = "Выберите роли",
    description: str = "Нажмите на кнопку, чтобы получить или убрать роль.",
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
            "Панель создана",
            f"ID панели: `{panel_id}`\n"
            f"Канал: {channel.mention}\n\n"
            f"Используйте `/reactionrole addrole {panel_id} <роль> <текст кнопки>` чтобы добавить роли.\n"
            f"Затем `/reactionrole send {panel_id}` чтобы отправить панель.",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )


@rr_group.sub_command(name="addrole", description="Добавить роль в панель")
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
        return await _respond(inter, simple("Ошибка", f"Панель `{panel_id}` не найдена", "error"), ephemeral=True)
    if len(panel["roles"]) >= 20:
        return await _respond(inter, simple("Ошибка", "Максимум 20 ролей на панель", "error"), ephemeral=True)
    panel["roles"].append({"role_id": str(role.id), "label": label})
    flush()
    await _respond(
        inter,
        simple(
            "Роль добавлена",
            f"**{label}** ({role.mention}) добавлена в панель `{panel_id}`\nВсего ролей: {len(panel['roles'])}",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )


@rr_group.sub_command(name="send", description="Отправить панель реакционных ролей")
async def rr_send(
    inter: disnake.ApplicationCommandInteraction,
    panel_id: str,
) -> None:
    assert inter.guild is not None
    rr_data, flush = save("reactionroles")
    gk = _guild_key(inter.guild)
    panel = rr_data.get(gk, {}).get(panel_id)
    if not panel:
        return await _respond(inter, simple("Ошибка", f"Панель `{panel_id}` не найдена", "error"), ephemeral=True)
    if not panel["roles"]:
        return await _respond(inter, simple("Ошибка", "В панели нет ролей", "error"), ephemeral=True)

    channel = inter.guild.get_channel(int(panel["channel_id"]))
    if not channel or not isinstance(channel, disnake.TextChannel):
        return await _respond(inter, simple("Ошибка", "Канал не найден", "error"), ephemeral=True)

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
        simple("Панель отправлена", f"Панель ролей отправлена в {channel.mention}", "ok", "Haven"),
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
        return await _respond(inter, simple("Ошибка", "Роль не найдена", "error"), ephemeral=True)
    member = inter.author
    assert isinstance(member, disnake.Member)
    if role in member.roles:
        await member.remove_roles(role)
        await _respond(
            inter,
            simple("Роль убрана", f"Роль {role.mention} убрана", "ok"),
            ephemeral=True,
        )
    else:
        await member.add_roles(role)
        await _respond(
            inter,
            simple("Роль получена", f"Роль {role.mention} выдана", "ok"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /afk
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="afk",
    description="Установить AFK статус",
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
        simple("AFK", f"{inter.author.mention} теперь AFK: {message}", "info", "Haven"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /remind
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="remind",
    description="Установить напоминание",
    contexts=GUILD_ONLY,
)
async def remind_cmd(
    inter: disnake.ApplicationCommandInteraction,
    time_str: str = commands.Param(name="time", description="Через сколько (10m / 2h / 1d)"),
    text: str = "Напоминание",
) -> None:
    dur = parse_dur(time_str)
    if dur is None:
        return await _respond(
            inter,
            simple("Ошибка", "Неверный формат времени. Используйте: 10m / 2h / 1d", "error"),
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
            "Напоминание",
            f"Напомню через {fmt_dur(dur)}: {text}",
            "ok",
            "Haven",
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
                                    "Напоминание",
                                    f"<@{uk}>, вы просили напомнить:\n\n**{r['text']}**",
                                    "info",
                                    f"Установлено: {r['ts'][:16]}",
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


@giveaway_group.sub_command(name="start", description="Начать розыгрыш")
async def giveaway_start(
    inter: disnake.ApplicationCommandInteraction,
    prize: str,
    duration: str = commands.Param(description="Длительность (1h / 1d / 7d)"),
    winners: int = commands.Param(default=1, ge=1, le=20, description="Количество победителей"),
    channel: disnake.TextChannel | None = None,
) -> None:
    assert inter.guild is not None
    dur = parse_dur(duration)
    if dur is None:
        return await _respond(inter, simple("Ошибка", "Неверный формат длительности", "error"), ephemeral=True)

    target_channel = channel or inter.channel
    assert isinstance(target_channel, disnake.TextChannel)

    ga_id = _gen_id()
    ends_at = now_ts() + int(dur.total_seconds())

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"### Розыгрыш"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                f"**Приз:** {prize}\n"
                f"**Победителей:** {winners}\n"
                f"**Заканчивается:** {disnake.utils.format_dt(datetime.fromtimestamp(ends_at, tz=timezone.utc), 'F')} "
                f"({disnake.utils.format_dt(datetime.fromtimestamp(ends_at, tz=timezone.utc), 'R')})\n"
                f"**Организатор:** {inter.author.mention}"
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Участвовать",
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
        simple("Розыгрыш", f"Розыгрыш `{ga_id}` создан в {target_channel.mention}", "ok", "Haven"),
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
        return await _respond(inter, simple("Розыгрыш", "Розыгрыш завершен", "error"), ephemeral=True)

    uid = str(inter.author.id)
    if uid in ga["participants"]:
        ga["participants"].remove(uid)
        flush()
        return await _respond(
            inter,
            simple("Розыгрыш", f"Вы покинули розыгрыш. Участников: {len(ga['participants'])}", "info"),
            ephemeral=True,
        )
    ga["participants"].append(uid)
    flush()
    await _respond(
        inter,
        simple("Розыгрыш", f"Вы участвуете! Участников: {len(ga['participants'])}", "ok"),
        ephemeral=True,
    )


@giveaway_group.sub_command(name="end", description="Завершить розыгрыш досрочно")
async def giveaway_end(
    inter: disnake.ApplicationCommandInteraction,
    giveaway_id: str,
) -> None:
    assert inter.guild is not None
    giveaways, flush = save("giveaways")
    gk = _guild_key(inter.guild)
    ga = giveaways.get(gk, {}).get(giveaway_id)
    if not ga:
        return await _respond(inter, simple("Ошибка", "Розыгрыш не найден", "error"), ephemeral=True)
    await _end_giveaway(inter.guild, giveaway_id, ga, giveaways, flush)
    await _respond(
        inter,
        simple("Розыгрыш", f"Розыгрыш `{giveaway_id}` завершен", "ok", "Haven"),
        ephemeral=True,
    )


@giveaway_group.sub_command(name="reroll", description="Перевыбрать победителей")
async def giveaway_reroll(
    inter: disnake.ApplicationCommandInteraction,
    giveaway_id: str,
) -> None:
    assert inter.guild is not None
    giveaways, flush = save("giveaways")
    gk = _guild_key(inter.guild)
    ga = giveaways.get(gk, {}).get(giveaway_id)
    if not ga:
        return await _respond(inter, simple("Ошибка", "Розыгрыш не найден", "error"), ephemeral=True)
    if not ga["participants"]:
        return await _respond(inter, simple("Ошибка", "Нет участников", "error"), ephemeral=True)

    winner_count = min(ga["winners"], len(ga["participants"]))
    winner_ids = random.sample(ga["participants"], winner_count)
    winners_text = ", ".join(f"<@{w}>" for w in winner_ids)

    channel = inter.guild.get_channel(int(ga["channel_id"]))
    if channel and isinstance(channel, disnake.TextChannel):
        await channel.send(
            components=simple(
                "Реролл розыгрыша",
                f"**Приз:** {ga['prize']}\n**Новые победители:** {winners_text}",
                "ok",
                "Haven",
            ),
            flags=MessageFlags(is_components_v2=True),
        )
    await _respond(inter, simple("Реролл", f"Новые победители: {winners_text}", "ok", "Haven"), ephemeral=True)


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
            components=simple("Розыгрыш завершен", f"**Приз:** {ga['prize']}\nНет участников", "warn", "Haven"),
            flags=MessageFlags(is_components_v2=True),
        )
        return

    winner_count = min(ga["winners"], len(ga["participants"]))
    winner_ids = random.sample(ga["participants"], winner_count)
    winners_text = ", ".join(f"<@{w}>" for w in winner_ids)

    await channel.send(
        components=card(
            "Розыгрыш завершен",
            [
                ("Приз", ga["prize"]),
                ("Победители", winners_text),
                ("Участников", str(len(ga["participants"]))),
            ],
            "ok",
            "Haven",
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
    description="Статистика модерации сервера",
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
        ("Всего кейсов", str(total_cases)),
        ("Уникальных нарушителей", str(unique_users)),
        ("Всего предупреждений", str(total_warnings)),
    ]

    if action_counts:
        breakdown = " | ".join(f"{k}: {v}" for k, v in sorted(action_counts.items(), key=lambda x: -x[1]))
        fields.append(("По типам", breakdown))

    top_offenders: list[tuple[str, int]] = []
    for uid, user_cases in guild_cases.items():
        top_offenders.append((uid, len(user_cases)))
    top_offenders.sort(key=lambda x: -x[1])

    if top_offenders:
        top_text = "\n".join(
            f"<@{uid}> -- {count} кейсов"
            for uid, count in top_offenders[:5]
        )
        fields.append(("Топ нарушителей", top_text))

    await _respond(
        inter,
        card(f"Статистика -- {inter.guild.name}", fields, "info", f"Haven v{VERSION}"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /haven (bot info)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="haven",
    description="Информация о боте Haven",
    contexts=GUILD_ONLY,
)
async def haven_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    guild_count = len(bot.guilds)
    total_members = sum(g.member_count or 0 for g in bot.guilds)
    uptime = datetime.now(timezone.utc) - _bot_start_time

    fields: list[tuple[str, str]] = [
        ("Версия", VERSION),
        ("Библиотека", f"disnake {disnake.__version__}"),
        ("Серверов", str(guild_count)),
        ("Участников (всего)", str(total_members)),
        ("Аптайм", fmt_dur(uptime)),
        ("Пинг", f"{round(bot.latency * 1000)}мс"),
        ("Команд", str(len(list(bot.all_slash_commands)))),
    ]

    bot_avatar = bot.user.display_avatar.url if bot.user else ""
    if bot_avatar:
        await _respond(
            inter,
            profile_card("Haven", [f"**{k}:** {v}" for k, v in fields], bot_avatar, "info", "Модерационный бот"),
            ephemeral=True,
        )
    else:
        await _respond(inter, card("Haven", fields, "info", "Модерационный бот"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /ping
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="ping",
    description="Проверить задержку бота",
    contexts=GUILD_ONLY,
)
async def ping_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    latency = round(bot.latency * 1000)
    kind = "ok" if latency < 200 else ("warn" if latency < 500 else "error")
    await _respond(
        inter,
        simple("Pong", f"Задержка: {latency}мс", kind, "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  TICKET SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

_TICKET_LABELS: dict[str, str] = {
    "question": "Вопрос",
    "complaint": "Жалоба",
    "appeal": "Апелляция",
    "bug": "Баг",
    "partnership": "Партнерство",
    "other": "Другое",
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
            simple("Тикет", f"У вас уже открыт тикет: <#{existing}>", "warn", "Haven"),
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
        topic=f"{label} | {author} | {author.id} | Средний",
        overwrites=overwrites,
    )

    tickets.setdefault(gk, {})[uk] = str(channel.id)
    flush()

    avatar_url = author.display_avatar.url

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"### Тикет #{ticket_num:04d}\n\n"
                    f"**Тип:** {label}\n"
                    f"**Автор:** {author.mention}\n"
                    f"**Приоритет:** Средний\n\n"
                    f"Опишите вашу проблему подробно. Модераторы скоро ответят."
                ),
                accessory=disnake.ui.Thumbnail(avatar_url),
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.StringSelect(
                    custom_id=f"ticket_priority:{channel.id}",
                    placeholder="Установить приоритет",
                    options=[
                        SelectOption(label="Низкий", value="low"),
                        SelectOption(label="Средний", value="medium"),
                        SelectOption(label="Высокий", value="high"),
                        SelectOption(label="Критический", value="critical"),
                    ],
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.UserSelect(
                    custom_id=f"ticket_add_user:{channel.id}",
                    placeholder="Добавить участника",
                ),
            ),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Закрыть тикет",
                    custom_id=f"ticket_close:{channel.id}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Сохранить транскрипт",
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
        simple("Тикет создан", f"Ваш тикет: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )

    await send_log(
        guild,
        card(
            "Тикет открыт",
            [
                ("Тип", label),
                ("Автор", f"{author.mention} ({author.id})"),
                ("Канал", channel.mention),
            ],
            "info",
            "Haven",
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
            simple("Ошибка", "Только модераторы могут менять приоритет", "error"),
            ephemeral=True,
        )
    priority_map = {"low": "Низкий", "medium": "Средний", "high": "Высокий", "critical": "Критический"}
    priority = priority_map.get(inter.values[0], "Средний")
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
        simple("Приоритет", f"Приоритет изменен: **{priority}**", kind, "Haven"),
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
            simple("Ошибка", "Только модераторы могут добавлять участников", "error"),
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
        simple("Участники добавлены", f"{', '.join(added)} добавлены в тикет", "ok", "Haven"),
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
            "Тикет взят",
            [
                ("Модератор", inter.author.mention),
                ("Время", fmt_ts(datetime.now(timezone.utc))),
            ],
            "info",
            "Haven",
        ),
    )
    await send_log(
        guild,
        simple(
            "Claim",
            f"**Модератор:** {inter.author.mention}\n**Канал:** {inter.channel.mention}",  # type: ignore[union-attr]
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
        simple("Транскрипт", f"Транскрипт сохранен ({len(transcript)} символов)", "ok", "Haven"),
        ephemeral=True,
        file=file,
    )


async def _build_transcript(channel: disnake.TextChannel) -> str:
    messages: list[disnake.Message] = []
    async for msg in channel.history(limit=MAX_TRANSCRIPT_MESSAGES, oldest_first=True):
        messages.append(msg)
    lines: list[str] = [
        f"=== Транскрипт канала #{channel.name} ===",
        f"=== Сервер: {channel.guild.name} ===",
        f"=== Дата: {fmt_ts(datetime.now(timezone.utc))} ===",
        f"=== Сообщений: {len(messages)} ===",
        "",
    ]
    for msg in messages:
        ts = msg.created_at.strftime("%d.%m %H:%M")
        content = msg.content or "(без текста)"
        attachments = ""
        if msg.attachments:
            attachments = " [Вложения: " + ", ".join(a.filename for a in msg.attachments) + "]"
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
            disnake.ui.TextDisplay("### Закрытие тикета"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay("Выберите действие:"),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Удалить канал",
                    custom_id=f"ticket_delete:{channel_id}",
                    style=ButtonStyle.danger,
                ),
                disnake.ui.Button(
                    label="Архивировать",
                    custom_id=f"ticket_archive:{channel_id}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Отмена",
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
    await _respond(inter, simple("Отменено", "Закрытие тикета отменено", "plain"), ephemeral=True)


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
            "Тикет удален",
            [
                ("Канал ID", channel_id),
                ("Модератор", inter.author.mention),
            ],
            "error",
            "Haven",
        ),
    )
    await _respond(
        inter,
        simple("Удаление", "Канал будет удален через 2 секунды", "error", "Haven"),
        ephemeral=True,
    )
    await asyncio.sleep(2)
    channel = guild.get_channel(int(channel_id))
    if channel and isinstance(channel, disnake.TextChannel):
        await channel.delete(reason=f"Тикет удален модератором {inter.author}")


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
                    "Тикет архивирован",
                    [
                        ("Канал", channel.name),
                        ("Модератор", inter.author.mention),
                    ],
                    "info",
                    "Haven",
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
                        "Тикет архивирован",
                        f"Ваш тикет в **{guild.name}** был архивирован.\nТранскрипт прикреплен ниже.",
                        "info",
                        "Haven",
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
        simple("Архивировано", "Тикет архивирован. Транскрипт отправлен в логи.", "ok", "Haven"),
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
                    f"{message.author.mention} вернулся (был AFK: {afk_data['message']})",
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
                    f"{user.display_name} сейчас AFK: {afk_data['message']}",
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
    roles = ", ".join(r.name for r in member.roles[1:]) if len(member.roles) > 1 else "Нет"
    joined = member.joined_at
    if joined:
        delta = datetime.now(timezone.utc) - joined
        time_on_server = fmt_dur(delta)
    else:
        time_on_server = "N/A"

    comps = profile_card(
        "Участник покинул сервер",
        [
            f"**{member}** ({member.id})",
            f"**Роли:** {roles}",
            f"**Был на сервере:** {time_on_server}",
        ],
        avatar_url,
        "error",
        f"Haven | {member.guild.name}",
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

    fields: list[tuple[str, str]] = [("Участник", f"{after.mention} ({after.id})")]
    if added:
        fields.append(("Добавлены", ", ".join(r.mention for r in added)))
    if removed:
        fields.append(("Убраны", ", ".join(r.mention for r in removed)))

    await send_log(after.guild, card("Изменение ролей", fields, "info"))


@bot.event
async def on_message_delete(message: disnake.Message) -> None:
    if message.author.bot or not message.guild:
        return
    _snipe_cache[message.channel.id] = {
        "content": message.content or "(без текста)",
        "author": str(message.author),
        "author_id": message.author.id,
        "avatar": message.author.display_avatar.url,
        "ts": datetime.now(timezone.utc),
        "attachments": [a.filename for a in message.attachments],
    }
    text = truncate(message.content, 1000) if message.content else "(пусто)"
    attachments = ""
    if message.attachments:
        attachments = f"\n**Вложения:** {', '.join(a.filename for a in message.attachments)}"

    await send_log(
        message.guild,
        card(
            "Сообщение удалено",
            [
                ("Автор", f"{message.author.mention} ({message.author.id})"),
                ("Канал", message.channel.mention),  # type: ignore[union-attr]
                ("Текст", text),
            ],
            "error",
            f"Haven | {fmt_ts(datetime.now(timezone.utc))}" + attachments,
        ),
    )


@bot.event
async def on_message_edit(before: disnake.Message, after: disnake.Message) -> None:
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return
    _editsnipe_cache[before.channel.id] = {
        "before": before.content or "(пусто)",
        "after": after.content or "(пусто)",
        "author": str(before.author),
        "author_id": before.author.id,
        "avatar": before.author.display_avatar.url,
        "ts": datetime.now(timezone.utc),
    }
    old = truncate(before.content, 500) if before.content else "(пусто)"
    new = truncate(after.content, 500) if after.content else "(пусто)"
    await send_log(
        before.guild,
        card(
            "Сообщение отредактировано",
            [
                ("Автор", f"{before.author.mention} ({before.author.id})"),
                ("Канал", before.channel.mention),  # type: ignore[union-attr]
                ("Было", old),
                ("Стало", new),
            ],
            "warn",
            f"Haven | {fmt_ts(datetime.now(timezone.utc))}",
        ),
    )


@bot.event
async def on_guild_channel_create(channel: disnake.abc.GuildChannel) -> None:
    await send_log(
        channel.guild,
        card(
            "Канал создан",
            [
                ("Название", channel.name),
                ("Тип", str(channel.type)),
                ("ID", str(channel.id)),
            ],
            "ok",
        ),
    )


@bot.event
async def on_guild_channel_delete(channel: disnake.abc.GuildChannel) -> None:
    await send_log(
        channel.guild,
        card(
            "Канал удален",
            [
                ("Название", channel.name),
                ("Тип", str(channel.type)),
                ("ID", str(channel.id)),
            ],
            "error",
        ),
    )


@bot.event
async def on_guild_role_create(role: disnake.Role) -> None:
    await send_log(
        role.guild,
        card(
            "Роль создана",
            [
                ("Название", role.name),
                ("ID", str(role.id)),
                ("Цвет", f"#{role.colour.value:06x}"),
            ],
            "ok",
        ),
    )


@bot.event
async def on_guild_role_delete(role: disnake.Role) -> None:
    await send_log(
        role.guild,
        card(
            "Роль удалена",
            [
                ("Название", role.name),
                ("ID", str(role.id)),
            ],
            "error",
        ),
    )


@bot.event
async def on_member_ban(guild: disnake.Guild, user: disnake.User) -> None:
    await send_log(
        guild,
        card(
            "Участник забанен (событие)",
            [
                ("Пользователь", f"{user} ({user.id})"),
            ],
            "error",
        ),
    )


@bot.event
async def on_member_unban(guild: disnake.Guild, user: disnake.User) -> None:
    await send_log(
        guild,
        card(
            "Участник разбанен (событие)",
            [
                ("Пользователь", f"{user} ({user.id})"),
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
                "Голосовой канал",
                f"{member.mention} зашел в {after.channel.mention}",
                "ok",
            ),
        )
    elif before.channel is not None and after.channel is None:
        await send_log(
            member.guild,
            simple(
                "Голосовой канал",
                f"{member.mention} вышел из {before.channel.mention}",
                "info",
            ),
        )
    elif before.channel is not None and after.channel is not None:
        await send_log(
            member.guild,
            simple(
                "Голосовой канал",
                f"{member.mention} перешел из {before.channel.mention} в {after.channel.mention}",
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
            simple("Доступ закрыт", "Недостаточно прав для выполнения этой команды", "error", "Haven"),
            ephemeral=True,
        )
    elif isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await _respond(
            inter,
            simple("Ошибка бота", f"У бота недостаточно прав: {missing}", "error", "Haven"),
            ephemeral=True,
        )
    elif isinstance(error, commands.MemberNotFound):
        await _respond(
            inter,
            simple("Ошибка", "Участник не найден", "error", "Haven"),
            ephemeral=True,
        )
    elif isinstance(error, commands.BadArgument):
        await _respond(
            inter,
            simple("Ошибка", f"Неверный аргумент: {error}", "error", "Haven"),
            ephemeral=True,
        )
    elif isinstance(error, commands.CommandOnCooldown):
        await _respond(
            inter,
            simple("Кулдаун", f"Подождите {error.retry_after:.1f}с", "warn", "Haven"),
            ephemeral=True,
        )
    else:
        await _respond(
            inter,
            simple("Ошибка", truncate(str(error), 400), "error", "Haven"),
            ephemeral=True,
        )
        raise error


# ═══════════════════════════════════════════════════════════════════════════
#  /snipe, /editsnipe
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="snipe",
    description="Показать последнее удаленное сообщение в канале",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def snipe_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    data = _snipe_cache.get(inter.channel.id)
    if not data:
        return await _respond(
            inter,
            simple("Snipe", "Нет удаленных сообщений в этом канале", "info", "Haven"),
            ephemeral=True,
        )
    age = datetime.now(timezone.utc) - data["ts"]
    text = truncate(data["content"], 1000)
    attachments = ""
    if data.get("attachments"):
        attachments = f"\n**Вложения:** {', '.join(data['attachments'])}"
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
                f"Удалено {fmt_dur(age)} назад",
            ],
            data["avatar"],
            "error",
            "Haven",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="editsnipe",
    description="Показать последнее отредактированное сообщение",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_messages=True),
)
async def editsnipe_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    data = _editsnipe_cache.get(inter.channel.id)
    if not data:
        return await _respond(
            inter,
            simple("EditSnipe", "Нет отредактированных сообщений в этом канале", "info", "Haven"),
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
                f"**Было:** {truncate(data['before'], 500)}",
                f"**Стало:** {truncate(data['after'], 500)}",
                f"",
                f"Отредактировано {fmt_dur(age)} назад",
            ],
            data["avatar"],
            "warn",
            "Haven",
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


@poll_group.sub_command(name="create", description="Создать голосование")
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
        disnake.ui.TextDisplay(f"### Голосование"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
        disnake.ui.TextDisplay(f"**{question}**\n\nАвтор: {inter.author.mention}"),
        disnake.ui.Separator(spacing=SeparatorSpacing.small),
    ]

    for i, opt in enumerate(options):
        children.append(disnake.ui.TextDisplay(f"**{i + 1}.** {opt} -- 0 голосов"))

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
                label="Завершить",
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
        return await _respond(inter, simple("Ошибка", "Голосование не найдено", "error"), ephemeral=True)

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
            "Голос принят",
            f"Вы проголосовали за: **{my_choice}**\nВсего голосов: {total}",
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
        return await _respond(inter, simple("Ошибка", "Голосование не найдено", "error"), ephemeral=True)

    if str(inter.author.id) != poll["author"] and not inter.author.guild_permissions.administrator:  # type: ignore[union-attr]
        return await _respond(inter, simple("Ошибка", "Только автор или администратор может завершить", "error"), ephemeral=True)

    total = sum(len(v) for v in poll["votes"].values())
    items: list[str] = []
    for i, opt in enumerate(poll["options"]):
        votes = len(poll["votes"][str(i)])
        pct = f" ({votes * 100 // total}%)" if total > 0 else ""
        bar_len = (votes * 20 // total) if total > 0 else 0
        bar = "`" + "#" * bar_len + "-" * (20 - bar_len) + "`"
        items.append(f"**{i + 1}.** {opt} -- {votes} голосов{pct}\n{bar}")

    del _poll_data[poll_id]

    await _respond(
        inter,
        list_card(
            f"Итоги: {poll['question']}",
            items,
            "info",
            f"Всего голосов: {total}",
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /starboard
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="starboard", description="Настроить starboard канал и порог")
async def setup_starboard(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    threshold: int = commands.Param(default=3, ge=1, le=25, description="Минимум реакций для попадания"),
    emoji: str = commands.Param(default="star", description="Реакция (star, fire, heart, thumbsup)"),
) -> None:
    emoji_map = {"star": "\u2b50", "fire": "\U0001f525", "heart": "\u2764\ufe0f", "thumbsup": "\U0001f44d"}
    actual_emoji = emoji_map.get(emoji, "\u2b50")
    set_cfg(inter.guild, "starboard_channel", str(channel.id))
    set_cfg(inter.guild, "starboard_threshold", threshold)
    set_cfg(inter.guild, "starboard_emoji", actual_emoji)
    await _respond(
        inter,
        simple(
            "Настройка",
            f"Starboard: {channel.mention}\nПорог: {threshold} реакций\nЭмодзи: {actual_emoji}",
            "ok",
            "Haven",
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
            text = truncate(message.content, 500) if message.content else "(без текста)"
            await sb_channel.send(
                components=profile_card(
                    f"Starboard ({sb_emoji} {reaction.count})",
                    [
                        f"**{message.author}** в {channel.mention}",
                        "",
                        text,
                        "",
                        f"[Перейти к сообщению]({message.jump_url})",
                    ],
                    message.author.display_avatar.url,
                    "warn",
                    f"Haven | {fmt_ts(message.created_at)}",
                ),
                flags=MessageFlags(is_components_v2=True),
            )
            break


# ═══════════════════════════════════════════════════════════════════════════
#  /autorole
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="autorole", description="Автоматическая роль при входе")
async def setup_autorole(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role | None = None,
) -> None:
    if role:
        set_cfg(inter.guild, "autorole", str(role.id))
        await _respond(
            inter,
            simple("Настройка", f"Авто-роль: {role.mention}", "ok", "Haven"),
            ephemeral=True,
        )
    else:
        set_cfg(inter.guild, "autorole", None)
        await _respond(
            inter,
            simple("Настройка", "Авто-роль отключена", "ok", "Haven"),
            ephemeral=True,
        )


@setup_group.sub_command(name="welcomedm", description="Приветственное сообщение в ЛС")
async def setup_welcomedm(
    inter: disnake.ApplicationCommandInteraction,
    message: str = commands.Param(description="Текст приветствия ({user} = упоминание, {server} = сервер)"),
) -> None:
    set_cfg(inter.guild, "welcome_dm", message)
    await _respond(
        inter,
        simple("Настройка", f"Приветствие в ЛС: {message}", "ok", "Haven"),
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


@nick_group.sub_command(name="set", description="Установить ник участнику")
async def nick_set(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    nickname: str,
) -> None:
    old = member.display_name
    await member.edit(nick=nickname, reason=f"Изменено {inter.author}")
    await _respond(
        inter,
        simple("Ник изменен", f"{member.mention}: **{old}** -> **{nickname}**", "ok", "Haven"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Ник изменен",
            f"**Участник:** {member.mention}\n**Было:** {old}\n**Стало:** {nickname}\n**Модератор:** {inter.author.mention}",
            "info",
        ),
    )


@nick_group.sub_command(name="reset", description="Сбросить ник участника")
async def nick_reset(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    old = member.display_name
    await member.edit(nick=None, reason=f"Сброшено {inter.author}")
    await _respond(
        inter,
        simple("Ник сброшен", f"{member.mention}: **{old}** -> оригинальный", "ok", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /roleall
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="roleall",
    description="Выдать или забрать роль у всех участников",
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
    verb = "выдана" if action == "add" else "забрана"
    text = f"Роль {role.mention} {verb} у {count} участников"
    if errors:
        text += f"\nОшибок: {errors}"
    await inter.followup.send(
        components=simple("Roleall", text, "ok", "Haven"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /report (user reporting system)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="report",
    description="Пожаловаться на участника модераторам",
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
            "Жалоба отправлена",
            f"Жалоба #{report_id} на {member.mention} отправлена модераторам.\n**Причина:** {reason}",
            "ok",
            "Haven",
        ),
        ephemeral=True,
    )

    await send_log(
        inter.guild,
        card(
            f"Жалоба #{report_id}",
            [
                ("Нарушитель", f"{member.mention} ({member.id})"),
                ("Жалобщик", f"{inter.author.mention} ({inter.author.id})"),
                ("Причина", reason),
                ("Статус", "Открыта"),
            ],
            "warn",
            "Haven",
        ),
    )


@bot.slash_command(
    name="reports",
    description="Список жалоб",
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
            simple("Жалобы", f"Нет жалоб (фильтр: {status})", "info", "Haven"),
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
            "Жалобы",
            items,
            "warn",
            f"Всего: {len(filtered)} | Показаны последние {len(items)}",
        ),
        ephemeral=True,
    )


@bot.slash_command(
    name="resolve",
    description="Закрыть жалобу",
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
                simple("Жалоба закрыта", f"Жалоба #{report_id} закрыта {inter.author.mention}", "ok", "Haven"),
                ephemeral=True,
            )
    await _respond(
        inter,
        simple("Ошибка", f"Жалоба #{report_id} не найдена", "error"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /verify (verification system)
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="verify", description="Настроить верификацию (роль + панель)")
async def setup_verify(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
    channel: disnake.TextChannel,
    message: str = "Нажмите кнопку ниже для получения доступа к серверу.",
) -> None:
    set_cfg(inter.guild, "verify_role", str(role.id))
    set_cfg(inter.guild, "verify_channel", str(channel.id))

    panel = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("### Верификация"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(message),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Подтвердить",
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
        simple("Настройка", f"Верификация: роль {role.mention}, канал {channel.mention}", "ok", "Haven"),
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
        return await _respond(inter, simple("Ошибка", "Роль верификации не найдена", "error"), ephemeral=True)
    member = inter.author
    assert isinstance(member, disnake.Member)
    if role in member.roles:
        return await _respond(inter, simple("Верификация", "Вы уже верифицированы", "info"), ephemeral=True)
    await member.add_roles(role, reason="Верификация")
    await _respond(
        inter,
        simple("Верификация", "Вы успешно верифицированы!", "ok", "Haven"),
        ephemeral=True,
    )
    await send_log(guild, simple("Верификация", f"{member.mention} верифицирован", "ok"))


# ═══════════════════════════════════════════════════════════════════════════
#  /whois
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="whois",
    description="Подробная информация о пользователе по ID (даже не на сервере)",
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
            simple("Ошибка", "Пользователь не найден", "error"),
            ephemeral=True,
        )

    created = disnake.utils.format_dt(user.created_at, "F")
    created_r = disnake.utils.format_dt(user.created_at, "R")

    lines = [
        f"**Тег:** {user}",
        f"**ID:** {user.id}",
        f"**Бот:** {'Да' if user.bot else 'Нет'}",
        f"**Создан:** {created} ({created_r})",
    ]

    assert inter.guild is not None
    member = inter.guild.get_member(user.id)
    if member:
        lines.append(f"**На сервере:** Да")
        if member.joined_at:
            lines.append(f"**Присоединился:** {disnake.utils.format_dt(member.joined_at, 'F')}")
        warn_count = len(_load("warnings.json", {}).get(_guild_key(inter.guild), {}).get(str(user.id), []))
        case_count = get_case_count(inter.guild, user)
        lines.append(f"**Варнов:** {warn_count} | **Кейсов:** {case_count}")
    else:
        lines.append(f"**На сервере:** Нет")

    try:
        ban = await inter.guild.fetch_ban(user)
        lines.append(f"**Забанен:** Да (причина: {ban.reason or 'N/A'})")
    except disnake.NotFound:
        lines.append(f"**Забанен:** Нет")

    await _respond(
        inter,
        profile_card("Whois", lines, user.display_avatar.url, "info", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /invites
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="invites",
    description="Топ приглашений на сервере",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(manage_guild=True),
)
async def invites_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    invites = await inter.guild.invites()
    if not invites:
        return await _respond(
            inter,
            simple("Приглашения", "Нет активных приглашений", "info"),
            ephemeral=True,
        )

    sorted_invites = sorted(invites, key=lambda i: i.uses or 0, reverse=True)[:15]
    items: list[str] = []
    for inv in sorted_invites:
        inviter = inv.inviter.mention if inv.inviter else "N/A"
        items.append(f"**{inv.code}** | {inviter} | {inv.uses or 0} использований | {inv.channel.mention if inv.channel else 'N/A'}")  # type: ignore[union-attr]

    await _respond(
        inter,
        list_card(
            "Приглашения",
            items,
            "info",
            f"Всего: {len(invites)} активных приглашений",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /tempbans, /banlist
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="tempbans",
    description="Список активных временных банов",
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
            simple("Временные баны", "Нет активных временных банов", "info"),
            ephemeral=True,
        )
    now = now_ts()
    items: list[str] = []
    for uid, entry in guild_bans.items():
        remaining = entry["expires"] - now
        if remaining > 0:
            items.append(
                f"<@{uid}> ({uid}) | Осталось: {fmt_dur(timedelta(seconds=remaining))} | Причина: {entry.get('reason', 'N/A')}"
            )
    if not items:
        return await _respond(
            inter,
            simple("Временные баны", "Нет активных временных банов", "info"),
            ephemeral=True,
        )
    await _respond(
        inter,
        list_card("Временные баны", items, "error", f"Всего: {len(items)}"),
        ephemeral=True,
    )


@bot.slash_command(
    name="banlist",
    description="Список забаненных (последние 25)",
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
            components=simple("Бан-лист", "Список банов пуст", "info"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    items: list[str] = []
    for ban in bans:
        items.append(f"**{ban.user}** ({ban.user.id}) | {ban.reason or 'Без причины'}")
    await inter.followup.send(
        components=list_card("Бан-лист", items, "error", f"Показаны последние {len(items)}"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /modleaderboard
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="modleaderboard",
    description="Топ модераторов по количеству действий",
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
            simple("Лидерборд", "Нет данных модерации", "info"),
            ephemeral=True,
        )

    sorted_mods = sorted(mod_counts.items(), key=lambda x: -x[1])[:10]
    items: list[str] = []
    for i, (mod_id, count) in enumerate(sorted_mods, 1):
        medal = {1: "[1]", 2: "[2]", 3: "[3]"}.get(i, f"[{i}]")
        items.append(f"**{medal}** <@{mod_id}> -- {_plural(count, 'действие', 'действия', 'действий')}")

    await _respond(
        inter,
        list_card("Лидерборд модераторов", items, "info", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /emojiinfo
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="emojiinfo",
    description="Информация об эмодзи сервера",
    contexts=GUILD_ONLY,
)
async def emojiinfo_cmd(
    inter: disnake.ApplicationCommandInteraction,
    emoji: disnake.Emoji | None = None,
) -> None:
    assert inter.guild is not None
    if emoji:
        fields = [
            ("Имя", emoji.name),
            ("ID", str(emoji.id)),
            ("Анимированный", "Да" if emoji.animated else "Нет"),
            ("Создан", disnake.utils.format_dt(emoji.created_at, "F")),
            ("URL", emoji.url),
        ]
        await _respond(
            inter,
            card(f"Эмодзи: {emoji.name}", fields, "info", "Haven"),
            ephemeral=True,
        )
    else:
        regular = [e for e in inter.guild.emojis if not e.animated]
        animated = [e for e in inter.guild.emojis if e.animated]
        await _respond(
            inter,
            card(
                "Эмодзи сервера",
                [
                    ("Всего", str(len(inter.guild.emojis))),
                    ("Обычных", str(len(regular))),
                    ("Анимированных", str(len(animated))),
                    ("Лимит", str(inter.guild.emoji_limit)),
                ],
                "info",
                "Haven",
            ),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  /counting
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="counting", description="Настроить канал-счетчик")
async def setup_counting(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "counting_channel", str(channel.id))
    set_cfg(inter.guild, "counting_current", 0)
    set_cfg(inter.guild, "counting_last_user", None)
    await _respond(
        inter,
        simple("Настройка", f"Канал-счетчик: {channel.mention}\nСчет начат с 0.", "ok", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /suggest
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="suggestions", description="Канал для предложений")
async def setup_suggestions(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "suggestions_channel", str(channel.id))
    await _respond(
        inter,
        simple("Настройка", f"Канал предложений: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@bot.slash_command(
    name="suggest",
    description="Отправить предложение",
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
            simple("Ошибка", "Канал предложений не настроен. Попросите администратора использовать /setup suggestions", "error"),
            ephemeral=True,
        )

    channel = inter.guild.get_channel(int(ch_id))
    if not channel or not isinstance(channel, disnake.TextChannel):
        return await _respond(inter, simple("Ошибка", "Канал не найден", "error"), ephemeral=True)

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
                    f"### Предложение #{sg_id}\n\n"
                    f"{suggestion}\n\n"
                    f"**Автор:** {inter.author.mention}\n"
                    f"**За:** 0 | **Против:** 0"
                ),
                accessory=disnake.ui.Thumbnail(inter.author.display_avatar.url),
            ),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="За",
                    custom_id=f"suggest_up:{gk}:{sg_id}",
                    style=ButtonStyle.secondary,
                ),
                disnake.ui.Button(
                    label="Против",
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
        simple("Предложение", f"Предложение #{sg_id} отправлено в {channel.mention}", "ok", "Haven"),
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
        return await _respond(inter, simple("Ошибка", "Предложение не найдено", "error"), ephemeral=True)

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
            "Голос принят",
            f"За: {len(sg['votes_up'])} | Против: {len(sg['votes_down'])}",
            "ok",
        ),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /embed (simple text panel builder)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="say",
    description="Отправить сообщение от имени бота",
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
        components=simple("Haven", text, color),
        flags=MessageFlags(is_components_v2=True),
    )
    await _respond(
        inter,
        simple("Отправлено", f"Сообщение отправлено в {target.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@bot.slash_command(
    name="announce",
    description="Отправить объявление",
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
        simple("Отправлено", f"Объявление отправлено в {target.mention}", "ok", "Haven"),
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


@customcmd_group.sub_command(name="add", description="Добавить пользовательскую команду")
async def customcmd_add(
    inter: disnake.ApplicationCommandInteraction,
    trigger: str = commands.Param(description="Триггер (слово или фраза)"),
    response: str = commands.Param(description="Ответ бота"),
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
        simple("Команда добавлена", f"Триггер: `{trigger}`\nОтвет: {truncate(response, 200)}", "ok", "Haven"),
        ephemeral=True,
    )


@customcmd_group.sub_command(name="remove", description="Удалить пользовательскую команду")
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
        await _respond(inter, simple("Команда удалена", f"Триггер `{trigger}` удален", "ok", "Haven"), ephemeral=True)
    else:
        await _respond(inter, simple("Ошибка", f"Команда `{trigger}` не найдена", "error"), ephemeral=True)


@customcmd_group.sub_command(name="list", description="Список пользовательских команд")
async def customcmd_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    ccmds = _load("customcmds.json", {})
    gk = _guild_key(inter.guild)
    guild_cmds = ccmds.get(gk, {})
    if not guild_cmds:
        return await _respond(inter, simple("Команды", "Нет пользовательских команд", "info"), ephemeral=True)
    items = [f"`{t}` -- {truncate(d['response'], 60)} ({d.get('uses', 0)} исп.)" for t, d in guild_cmds.items()]
    await _respond(
        inter,
        list_card("Пользовательские команды", items, "info", f"Всего: {len(guild_cmds)}"),
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


@ar_group.sub_command(name="add", description="Добавить авто-ответ")
async def ar_add(
    inter: disnake.ApplicationCommandInteraction,
    trigger: str = commands.Param(description="Слово-триггер"),
    response: str = commands.Param(description="Ответ"),
    exact: bool = commands.Param(default=False, description="Точное совпадение (не содержание)"),
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
    mode = "точное совпадение" if exact else "содержание"
    await _respond(
        inter,
        simple("Авто-ответ добавлен", f"Триггер: `{trigger}` ({mode})\nОтвет: {truncate(response, 200)}", "ok", "Haven"),
        ephemeral=True,
    )


@ar_group.sub_command(name="remove", description="Удалить авто-ответ по номеру")
async def ar_remove(
    inter: disnake.ApplicationCommandInteraction,
    number: int = commands.Param(ge=1),
) -> None:
    assert inter.guild is not None
    ar, flush = save("autoresponders")
    gk = _guild_key(inter.guild)
    items = ar.get(gk, [])
    if number > len(items):
        return await _respond(inter, simple("Ошибка", f"Авто-ответ #{number} не найден", "error"), ephemeral=True)
    removed = items.pop(number - 1)
    flush()
    await _respond(
        inter,
        simple("Авто-ответ удален", f"Удален: `{removed['trigger']}`", "ok", "Haven"),
        ephemeral=True,
    )


@ar_group.sub_command(name="list", description="Список авто-ответов")
async def ar_list(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    ar = _load("autoresponders.json", {})
    gk = _guild_key(inter.guild)
    items = ar.get(gk, [])
    if not items:
        return await _respond(inter, simple("Авто-ответы", "Нет авто-ответов", "info"), ephemeral=True)
    lines = [
        f"**#{i+1}** `{a['trigger']}` -> {truncate(a['response'], 60)} ({'точное' if a.get('exact') else 'содержание'})"
        for i, a in enumerate(items)
    ]
    await _respond(inter, list_card("Авто-ответы", lines, "info", f"Всего: {len(items)}"), ephemeral=True)


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


@sticky_group.sub_command(name="set", description="Закрепить сообщение (будет пересоздаваться)")
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
        components=simple("Закрепленное сообщение", text, "info", "Haven"),
        flags=MessageFlags(is_components_v2=True),
    )
    stickies[gk][str(inter.channel.id)]["message_id"] = str(msg.id)
    _sticky_messages[inter.channel.id] = msg.id
    flush()

    await _respond(
        inter,
        simple("Sticky", f"Закрепленное сообщение установлено", "ok", "Haven"),
        ephemeral=True,
    )


@sticky_group.sub_command(name="remove", description="Убрать закрепленное сообщение")
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
    await _respond(inter, simple("Sticky", "Закрепленное сообщение убрано", "ok", "Haven"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /lockdown (server-wide lockdown)
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="lockdown",
    description="Заблокировать все текстовые каналы",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def lockdown_cmd(
    inter: disnake.ApplicationCommandInteraction,
    reason: str = "Экстренная блокировка",
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
            "Сервер заблокирован",
            [
                ("Заблокировано каналов", str(locked_count)),
                ("Причина", reason),
                ("Модератор", inter.author.mention),
            ],
            "error",
            "Haven | Используйте /unlockdown для снятия",
        ),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,
        card(
            "Lockdown",
            [
                ("Каналов", str(locked_count)),
                ("Причина", reason),
                ("Модератор", inter.author.mention),
            ],
            "error",
            "Haven",
        ),
    )


@bot.slash_command(
    name="unlockdown",
    description="Разблокировать все текстовые каналы",
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
        components=simple("Сервер разблокирован", f"Разблокировано каналов: {unlocked}", "ok", "Haven"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,
        simple("Unlockdown", f"**Каналов:** {unlocked}\n**Модератор:** {inter.author.mention}", "ok"),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /quarantine
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="quarantine", description="Настроить карантинную роль")
async def setup_quarantine(
    inter: disnake.ApplicationCommandInteraction,
    role: disnake.Role,
) -> None:
    set_cfg(inter.guild, "quarantine_role", str(role.id))
    await _respond(
        inter,
        simple("Настройка", f"Карантинная роль: {role.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@bot.slash_command(
    name="quarantine",
    description="Поставить участника на карантин",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(moderate_members=True),
)
async def quarantine_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    assert inter.guild is not None
    cfg = get_cfg(inter.guild)
    role_id = cfg.get("quarantine_role")
    if not role_id:
        return await _respond(
            inter,
            simple("Ошибка", "Карантинная роль не настроена. Используйте /setup quarantine", "error"),
            ephemeral=True,
        )
    role = inter.guild.get_role(int(role_id))
    if not role:
        return await _respond(inter, simple("Ошибка", "Роль не найдена", "error"), ephemeral=True)

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
        await member.remove_roles(*roles_to_remove, reason=f"Карантин: {reason}")
    await member.add_roles(role, reason=f"Карантин: {reason}")

    cid = add_case(inter.guild, member, "quarantine", inter.author, reason)
    c = card(
        "Карантин",
        [
            ("Участник", f"{member.mention} ({member.id})"),
            ("Причина", reason),
            ("Сохранено ролей", str(len(saved_roles))),
            ("Модератор", inter.author.mention),
            ("Кейс", f"#{cid}"),
        ],
        "error",
        "Haven",
    )
    await _respond(inter, c, ephemeral=True)
    await send_log(inter.guild, c)
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",
            f"Вы поставлены на карантин.\n\n**Причина:** {reason}",
            "error",
            f"Кейс #{cid}",
        ),
    )


@bot.slash_command(
    name="unquarantine",
    description="Снять карантин и восстановить роли",
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
            await member.remove_roles(role, reason="Снятие карантина")

    if data:
        roles_to_add = []
        for rid in data.get("saved_roles", []):
            r = inter.guild.get_role(rid)
            if r:
                roles_to_add.append(r)
        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Восстановление ролей после карантина")
        del quarantined[gk][str(member.id)]
        flush()
        restored = len(roles_to_add)
    else:
        restored = 0

    c = card(
        "Карантин снят",
        [
            ("Участник", f"{member.mention}"),
            ("Восстановлено ролей", str(restored)),
            ("Модератор", inter.author.mention),
        ],
        "ok",
        "Haven",
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


@voice_group.sub_command(name="kick", description="Отключить участника из голосового канала")
async def voice_kick(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    if not member.voice or not member.voice.channel:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    ch_name = member.voice.channel.name
    await member.move_to(None, reason=reason)
    await _respond(
        inter,
        simple("Голосовой кик", f"{member.mention} отключен из {ch_name}", "ok", "Haven"),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple("Голосовой кик", f"**Участник:** {member.mention}\n**Канал:** {ch_name}\n**Модератор:** {inter.author.mention}", "info"),
    )


@voice_group.sub_command(name="move", description="Переместить участника в другой голосовой канал")
async def voice_move(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    channel: disnake.VoiceChannel,
) -> None:
    if not member.voice or not member.voice.channel:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    old_ch = member.voice.channel.name
    await member.move_to(channel)
    await _respond(
        inter,
        simple("Перемещение", f"{member.mention}: {old_ch} -> {channel.name}", "ok", "Haven"),
        ephemeral=True,
    )


@voice_group.sub_command(name="moveall", description="Переместить всех из одного канала в другой")
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
        components=simple("Перемещение", f"Перемещено {moved} участников из {source.name} в {target.name}", "ok", "Haven"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@voice_group.sub_command(name="limit", description="Установить лимит участников в голосовом канале")
async def voice_limit(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.VoiceChannel,
    limit: int = commands.Param(ge=0, le=99, description="0 = без лимита"),
) -> None:
    await channel.edit(user_limit=limit)
    text = f"Лимит {channel.name}: {limit}" if limit else f"Лимит {channel.name} снят"
    await _respond(inter, simple("Голосовой канал", text, "ok", "Haven"), ephemeral=True)


@voice_group.sub_command(name="mute", description="Замутить участника в голосовом канале")
async def voice_mute(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    await member.edit(mute=True, reason=f"Voice mute by {inter.author}")
    await _respond(
        inter,
        simple("Голосовой мут", f"{member.mention} замучен в голосовом канале", "ok", "Haven"),
        ephemeral=True,
    )


@voice_group.sub_command(name="unmute", description="Размутить участника в голосовом канале")
async def voice_unmute(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    await member.edit(mute=False, reason=f"Voice unmute by {inter.author}")
    await _respond(
        inter,
        simple("Голосовой мут снят", f"{member.mention} размучен", "ok", "Haven"),
        ephemeral=True,
    )


@voice_group.sub_command(name="deafen", description="Заглушить участника в голосовом канале")
async def voice_deafen(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    await member.edit(deafen=True, reason=f"Voice deafen by {inter.author}")
    await _respond(inter, simple("Заглушка", f"{member.mention} заглушен", "ok", "Haven"), ephemeral=True)


@voice_group.sub_command(name="undeafen", description="Снять заглушку")
async def voice_undeafen(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
) -> None:
    if not member.voice:
        return await _respond(inter, simple("Ошибка", "Участник не в голосовом канале", "error"), ephemeral=True)
    await member.edit(deafen=False, reason=f"Voice undeafen by {inter.author}")
    await _respond(inter, simple("Заглушка снята", f"{member.mention} разглушен", "ok", "Haven"), ephemeral=True)


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


@thread_group.sub_command(name="create", description="Создать ветку")
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
        simple("Ветка создана", f"{thread.mention} ({('приватная' if private else 'публичная')})", "ok", "Haven"),
        ephemeral=True,
    )


@thread_group.sub_command(name="archive", description="Архивировать ветку")
async def thread_archive(
    inter: disnake.ApplicationCommandInteraction,
) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Ошибка", "Эта команда работает только в ветке", "error"), ephemeral=True)
    await inter.channel.edit(archived=True)
    await _respond(inter, simple("Ветка", "Ветка архивирована", "ok", "Haven"), ephemeral=True)


@thread_group.sub_command(name="lock", description="Заблокировать ветку")
async def thread_lock(inter: disnake.ApplicationCommandInteraction) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Ошибка", "Эта команда работает только в ветке", "error"), ephemeral=True)
    await inter.channel.edit(locked=True)
    await _respond(inter, simple("Ветка", "Ветка заблокирована", "ok", "Haven"), ephemeral=True)


@thread_group.sub_command(name="unlock", description="Разблокировать ветку")
async def thread_unlock(inter: disnake.ApplicationCommandInteraction) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Ошибка", "Эта команда работает только в ветке", "error"), ephemeral=True)
    await inter.channel.edit(locked=False)
    await _respond(inter, simple("Ветка", "Ветка разблокирована", "ok", "Haven"), ephemeral=True)


@thread_group.sub_command(name="rename", description="Переименовать ветку")
async def thread_rename(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
) -> None:
    if not isinstance(inter.channel, disnake.Thread):
        return await _respond(inter, simple("Ошибка", "Эта команда работает только в ветке", "error"), ephemeral=True)
    old = inter.channel.name
    await inter.channel.edit(name=name)
    await _respond(inter, simple("Ветка", f"Переименована: {old} -> {name}", "ok", "Haven"), ephemeral=True)


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


@channel_group.sub_command(name="clone", description="Клонировать канал")
async def channel_clone(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel | None = None,
) -> None:
    ch = channel or inter.channel
    assert isinstance(ch, disnake.TextChannel)
    cloned = await ch.clone(reason=f"Клонировано {inter.author}")
    await _respond(
        inter,
        simple("Канал клонирован", f"Создан {cloned.mention} (клон {ch.mention})", "ok", "Haven"),
        ephemeral=True,
    )


@channel_group.sub_command(name="rename", description="Переименовать канал")
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
        simple("Канал переименован", f"#{old} -> #{name}", "ok", "Haven"),
        ephemeral=True,
    )


@channel_group.sub_command(name="topic", description="Установить тему канала")
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
        simple("Тема установлена", f"#{ch.name}: {truncate(topic, 100)}", "ok", "Haven"),
        ephemeral=True,
    )


@channel_group.sub_command(name="create", description="Создать текстовый канал")
async def channel_create(
    inter: disnake.ApplicationCommandInteraction,
    name: str,
    category: disnake.CategoryChannel | None = None,
) -> None:
    assert inter.guild is not None
    ch = await inter.guild.create_text_channel(name=name, category=category)
    await _respond(
        inter,
        simple("Канал создан", f"{ch.mention}", "ok", "Haven"),
        ephemeral=True,
    )


@channel_group.sub_command(name="delete", description="Удалить канал")
async def channel_delete(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    reason: str = "Не указана",
) -> None:
    name = channel.name
    await channel.delete(reason=reason)
    await _respond(
        inter,
        simple("Канал удален", f"#{name}", "ok", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /massnick
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="massnick",
    description="Массовое изменение ников",
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
        return await _respond(inter, simple("Ошибка", "Укажите ник для установки", "error"), ephemeral=True)
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
    verb = "установлен" if action == "set" else "сброшен"
    text = f"Ник {verb} у {changed} участников"
    if errors:
        text += f"\nОшибок: {errors}"
    await inter.followup.send(
        components=simple("Massnick", text, "ok", "Haven"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /permissions
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="permissions",
    description="Проверить права участника в канале",
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
        "view_channel": "Видеть канал",
        "send_messages": "Отправлять сообщения",
        "manage_messages": "Управлять сообщениями",
        "manage_channels": "Управлять каналом",
        "embed_links": "Встраивать ссылки",
        "attach_files": "Прикреплять файлы",
        "add_reactions": "Добавлять реакции",
        "use_external_emojis": "Внешние эмодзи",
        "mention_everyone": "Упоминать everyone",
        "read_message_history": "Читать историю",
        "manage_threads": "Управлять ветками",
        "create_public_threads": "Создавать ветки",
        "send_messages_in_threads": "Писать в ветках",
    }

    for perm, label in perm_labels.items():
        if getattr(perms, perm, False):
            allowed.append(label)
        else:
            denied.append(label)

    fields: list[tuple[str, str]] = [
        ("Участник", f"{member.mention}"),
        ("Канал", ch.mention),
    ]
    if allowed:
        fields.append(("Разрешено", ", ".join(allowed)))
    if denied:
        fields.append(("Запрещено", ", ".join(denied)))

    await _respond(inter, card("Права", fields, "info", "Haven"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /rolehierarchy
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="rolehierarchy",
    description="Показать иерархию ролей",
    contexts=GUILD_ONLY,
)
async def rolehierarchy_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    roles = sorted(inter.guild.roles, key=lambda r: r.position, reverse=True)
    items: list[str] = []
    for i, role in enumerate(roles[:30]):
        count = len(role.members)
        items.append(f"**[{role.position}]** {role.mention} -- {count} уч.")
    footer = f"Всего: {len(inter.guild.roles)} ролей"
    if len(roles) > 30:
        footer += " (показаны первые 30)"
    await _respond(inter, list_card("Иерархия ролей", items, "info", footer), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /boosts
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="boosts",
    description="Информация о бустах сервера",
    contexts=GUILD_ONLY,
)
async def boosts_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert inter.guild is not None
    boosters = [m for m in inter.guild.members if m.premium_since]
    boosters.sort(key=lambda m: m.premium_since or datetime.min.replace(tzinfo=timezone.utc))

    fields: list[tuple[str, str]] = [
        ("Уровень буста", str(inter.guild.premium_tier)),
        ("Количество бустов", str(inter.guild.premium_subscription_count or 0)),
        ("Бустеров", str(len(boosters))),
    ]

    if boosters:
        booster_list = "\n".join(
            f"{m.mention} -- с {disnake.utils.format_dt(m.premium_since, 'R')}"
            for m in boosters[:10]
        )
        fields.append(("Бустеры", booster_list))

    await _respond(inter, card("Бусты сервера", fields, "info", "Haven"), ephemeral=True)


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


@schedule_group.sub_command(name="message", description="Запланировать сообщение")
async def schedule_message(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
    delay: str = commands.Param(description="Через сколько (10m / 2h / 1d)"),
    text: str = commands.Param(description="Текст сообщения"),
) -> None:
    dur = parse_dur(delay)
    if dur is None:
        return await _respond(inter, simple("Ошибка", "Неверный формат времени", "error"), ephemeral=True)
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
        simple("Запланировано", f"Сообщение будет отправлено в {channel.mention} через {fmt_dur(dur)}", "ok", "Haven"),
        ephemeral=True,
    )


@schedule_group.sub_command(name="list", description="Список запланированных сообщений")
async def schedule_list(inter: disnake.ApplicationCommandInteraction) -> None:
    scheduled = _load("scheduled.json", {})
    gk = _guild_key(inter.guild)
    items_data = scheduled.get(gk, [])
    now = now_ts()
    pending = [s for s in items_data if s["send_at"] > now]
    if not pending:
        return await _respond(inter, simple("Расписание", "Нет запланированных сообщений", "info"), ephemeral=True)
    items: list[str] = []
    for i, s in enumerate(pending, 1):
        remaining = s["send_at"] - now
        items.append(f"**#{i}** <#{s['channel_id']}> | через {fmt_dur(timedelta(seconds=remaining))} | {truncate(s['text'], 50)}")
    await _respond(inter, list_card("Запланированные сообщения", items, "info", f"Всего: {len(pending)}"), ephemeral=True)


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
                            components=simple("Haven", s["text"], "info"),
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
    description="Экспортировать настройки бота",
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
        simple("Бэкап", f"Настройки экспортированы ({len(export)} байт)", "ok", "Haven"),
        ephemeral=True,
        file=file,
    )


@bot.slash_command(
    name="restore",
    description="Импортировать настройки бота из файла",
    contexts=GUILD_ONLY,
    default_member_permissions=disnake.Permissions(administrator=True),
)
async def restore_cmd(
    inter: disnake.ApplicationCommandInteraction,
    file: disnake.Attachment,
) -> None:
    assert inter.guild is not None
    if not file.filename.endswith(".json"):
        return await _respond(inter, simple("Ошибка", "Файл должен быть .json", "error"), ephemeral=True)
    content = await file.read()
    try:
        backup_data = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return await _respond(inter, simple("Ошибка", "Неверный формат JSON", "error"), ephemeral=True)

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
        simple("Восстановлено", f"Импортировано {restored} категорий настроек", "ok", "Haven"),
        ephemeral=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  /milestone
# ═══════════════════════════════════════════════════════════════════════════


@setup_group.sub_command(name="milestones", description="Настроить вехи участников")
async def setup_milestones(
    inter: disnake.ApplicationCommandInteraction,
    channel: disnake.TextChannel,
) -> None:
    set_cfg(inter.guild, "milestone_channel", str(channel.id))
    await _respond(
        inter,
        simple("Настройка", f"Канал вех: {channel.mention}", "ok", "Haven"),
        ephemeral=True,
    )


_MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]


# ═══════════════════════════════════════════════════════════════════════════
#  /color
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="color",
    description="Информация о цвете (hex)",
    contexts=GUILD_ONLY,
)
async def color_cmd(
    inter: disnake.ApplicationCommandInteraction,
    hex_color: str = commands.Param(description="Hex цвет (например: #ff5733 или ff5733)"),
) -> None:
    clean = hex_color.lstrip("#")
    if len(clean) != 6:
        return await _respond(inter, simple("Ошибка", "Неверный формат (используйте 6 символов hex)", "error"), ephemeral=True)
    try:
        value = int(clean, 16)
    except ValueError:
        return await _respond(inter, simple("Ошибка", "Неверный hex-код", "error"), ephemeral=True)

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
            disnake.ui.TextDisplay(f"### Цвет: #{clean.upper()}"),
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
    description="Статистика участников сервера",
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
        ("Всего", str(total)),
        ("Люди", str(humans)),
        ("Боты", str(bots)),
        ("В сети", str(online)),
        ("Бустеры", str(boosters)),
    ]
    await _respond(inter, card("Участники", fields, "info", "Haven"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /firstmessage
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="firstmessage",
    description="Ссылка на первое сообщение в канале",
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
            simple("Первое сообщение", f"[Перейти к сообщению]({msg.jump_url})\n**Автор:** {msg.author}\n**Дата:** {fmt_ts(msg.created_at)}", "info", "Haven"),
            ephemeral=True,
        )
        return
    await _respond(inter, simple("Ошибка", "Сообщений не найдено", "error"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════
#  /topic
# ═══════════════════════════════════════════════════════════════════════════


@bot.slash_command(
    name="topic",
    description="Показать или установить тему канала",
    contexts=GUILD_ONLY,
)
async def topic_cmd(
    inter: disnake.ApplicationCommandInteraction,
    new_topic: str | None = None,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    if new_topic is not None:
        if not inter.author.guild_permissions.manage_channels:  # type: ignore[union-attr]
            return await _respond(inter, simple("Ошибка", "Недостаточно прав", "error"), ephemeral=True)
        await inter.channel.edit(topic=new_topic)
        await _respond(
            inter,
            simple("Тема установлена", truncate(new_topic, 200), "ok", "Haven"),
            ephemeral=True,
        )
    else:
        topic_text = inter.channel.topic or "Тема не установлена"
        await _respond(
            inter,
            simple(f"Тема #{inter.channel.name}", topic_text, "info", "Haven"),
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
                    components=simple("Закрепленное сообщение", sticky_data["text"], "info", "Haven"),
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
                components=simple("Haven", cmd_data["response"], "info"),
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
                    components=simple("Haven", entry["response"], "info"),
                    flags=MessageFlags(is_components_v2=True),
                )
            except Exception:
                pass
            break
        elif not entry.get("exact") and trigger in msg_lower:
            try:
                await message.channel.send(
                    components=simple("Haven", entry["response"], "info"),
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
                    components=simple("Счетчик", f"{message.author.mention}, нельзя считать два раза подряд!", "warn"),
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
                        "Счетчик",
                        f"{message.author.mention} ошибся! Правильное число было **{current + 1}**. Счет сброшен.",
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
                    reason="Авто-модерация: спам",
                )
                add_case(message.guild, message.author, "auto-mute", message.guild.me, "Спам (авто-модерация)", "5м")
                await message.channel.send(
                    components=simple(
                        "Авто-модерация",
                        f"{message.author.mention} получил тайм-аут за спам",
                        "warn",
                        "Haven",
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
                        "Авто-модерация",
                        f"{message.author.mention}, не используйте чрезмерное количество заглавных букв",
                        "warn",
                        "Haven",
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
                    "Авто-модерация",
                    f"{message.author.mention}, ссылки-приглашения запрещены",
                    "warn",
                    "Haven",
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
                    "Авто-модерация",
                    f"{message.author.mention}, ссылки запрещены",
                    "warn",
                    "Haven",
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
                        "Авто-модерация",
                        f"{message.author.mention}, слишком много упоминаний ({len(message.mentions)}/{max_mentions})",
                        "warn",
                        "Haven",
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
                await member.add_roles(role, reason="Авто-роль")
            except disnake.Forbidden:
                pass

    # Welcome DM
    welcome_dm = cfg.get("welcome_dm")
    if welcome_dm:
        text = welcome_dm.replace("{user}", member.mention).replace("{server}", member.guild.name)
        try:
            await member.send(
                components=simple(f"Haven -- {member.guild.name}", text, "ok"),
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
                "Подозрительный аккаунт",
                [
                    ("Участник", f"{member.mention} ({member.id})"),
                    ("Возраст аккаунта", fmt_dur(account_age)),
                ],
                "warn",
                "Haven | Возможный альт-аккаунт",
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
                "Анти-рейд",
                f"Обнаружен возможный рейд! {ANTI_RAID_THRESHOLD} входов за {ANTI_RAID_WINDOW} секунд.",
                "error",
                "Haven",
            ),
        )

    # Welcome message
    ch_id = cfg.get("welcome_channel")
    if ch_id:
        ch = member.guild.get_channel(int(ch_id))
        if ch and isinstance(ch, disnake.TextChannel):
            avatar_url = member.display_avatar.url
            comps = profile_card(
                "Добро пожаловать!",
                [
                    f"{member.mention} присоединился к серверу.",
                    f"**Участник #{member.guild.member_count}**",
                    f"**Аккаунт создан:** {disnake.utils.format_dt(member.created_at, 'R')} ({fmt_dur(account_age)} назад)",
                ],
                avatar_url,
                "ok",
                f"Haven | {member.guild.name}",
            )
            await ch.send(components=comps, flags=MessageFlags(is_components_v2=True))

    # Milestone check
    milestone_ch_id = cfg.get("milestone_channel")
    if milestone_ch_id and member.guild.member_count in _MILESTONES:
        ms_ch = member.guild.get_channel(int(milestone_ch_id))
        if ms_ch and isinstance(ms_ch, disnake.TextChannel):
            await ms_ch.send(
                components=simple(
                    "Веха достигнута!",
                    f"На сервере теперь **{member.guild.member_count}** участников!",
                    "ok",
                    "Haven",
                ),
                flags=MessageFlags(is_components_v2=True),
            )


# ═══════════════════════════════════════════════════════════════════════════
#  BOT READY + LAUNCH
# ═══════════════════════════════════════════════════════════════════════════

_bot_start_time = datetime.now(timezone.utc)


@bot.event
async def on_ready() -> None:
    print(f"Haven v{VERSION} is online as {bot.user} ({bot.user.id})")  # type: ignore[union-attr]
    print(f"Guilds: {len(bot.guilds)} | Commands: {len(list(bot.all_slash_commands))}")
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
            name=f"{len(bot.guilds)} серверов",
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
