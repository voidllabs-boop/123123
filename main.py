"""Haven -- Discord moderation bot (disnake >= 2.11, Components V2)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
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

GUILD_ONLY = InteractionContextTypes(guild=True, bot_dm=False, private_channel=False)

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
COLORS: dict[str, int] = {
    "plain": 0x2C2F33,
    "ok": 0x57F287,
    "warn": 0xFEE75C,
    "error": 0xED4245,
    "info": 0x5865F2,
}

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = disnake.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.InteractionBot(intents=intents)

DATA = Path("data")
DATA.mkdir(exist_ok=True)

TICKET_CATEGORY_NAME = "Tickets"

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

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


def save(key: str):
    """Return (data, flush) tuple for a given storage key."""
    path = f"{key}.json"
    data = _load(path, {})

    def flush() -> None:
        _save(path, data)

    return data, flush


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_DUR_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> int:
    return int(time.time())


def _check_hierarchy(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> bool:
    return inter.author.top_role > member.top_role  # type: ignore[union-attr]


def _guild_key(guild: disnake.Guild) -> str:
    return str(guild.id)


def _user_key(user: disnake.User | disnake.Member) -> str:
    return str(user.id)


# ---------------------------------------------------------------------------
# Case helpers
# ---------------------------------------------------------------------------

def _next_case_id(guild_id: str, user_id: str) -> int:
    cases, _ = save("cases")
    g = cases.get(guild_id, {})
    u = g.get(user_id, [])
    return len(u) + 1


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
            "reason": reason,
            "duration": duration,
            "ts": now_iso(),
        }
    )
    flush()
    return cid


# ---------------------------------------------------------------------------
# Components V2 helpers
# ---------------------------------------------------------------------------

def card(title: str, fields: list[tuple[str, str]], kind: str = "plain") -> list[disnake.ui.Container]:
    children: list[Any] = [disnake.ui.TextDisplay(f"**{title}**")]
    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    for key, value in fields:
        children.append(disnake.ui.TextDisplay(f"**{key}:** {value}"))
    return [
        disnake.ui.Container(
            *children,
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


def simple(title: str, body: str, kind: str = "plain") -> list[disnake.ui.Container]:
    return [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"**{title}**"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(body),
            accent_colour=Color(COLORS.get(kind, COLORS["plain"])),
        )
    ]


async def send_log(guild: disnake.Guild, components: list[Any]) -> None:
    cfg = _load("config.json", {})
    gk = _guild_key(guild)
    ch_id = cfg.get(gk, {}).get("log_channel")
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id))
    if ch and isinstance(ch, disnake.TextChannel):
        await ch.send(components=components, flags=MessageFlags(is_components_v2=True))


async def _try_dm(user: disnake.User | disnake.Member, components: list[Any]) -> None:
    try:
        await user.send(components=components, flags=MessageFlags(is_components_v2=True))
    except disnake.Forbidden:
        pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_cfg(guild: disnake.Guild) -> dict[str, Any]:
    cfg = _load("config.json", {})
    return cfg.get(_guild_key(guild), {})


def set_cfg(guild: disnake.Guild, key: str, value: Any) -> None:
    cfg = _load("config.json", {})
    gk = _guild_key(guild)
    cfg.setdefault(gk, {})[key] = value
    _save("config.json", cfg)


# ---------------------------------------------------------------------------
# /setup commands
# ---------------------------------------------------------------------------

@bot.slash_command(name="setup", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(administrator=True))
async def setup_group(inter: disnake.ApplicationCommandInteraction) -> None:
    pass


@setup_group.sub_command(name="logs", description="Kanал для логов модерации")
async def setup_logs(inter: disnake.ApplicationCommandInteraction, channel: disnake.TextChannel) -> None:
    set_cfg(inter.guild, "log_channel", str(channel.id))  # type: ignore[arg-type]
    await inter.response.send_message(
        components=simple("Настройка", f"Лог-канал установлен: {channel.mention}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@setup_group.sub_command(name="welcome", description="Kanал приветствий")
async def setup_welcome(inter: disnake.ApplicationCommandInteraction, channel: disnake.TextChannel) -> None:
    set_cfg(inter.guild, "welcome_channel", str(channel.id))  # type: ignore[arg-type]
    await inter.response.send_message(
        components=simple("Настройка", f"Канал приветствий: {channel.mention}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@setup_group.sub_command(name="leave", description="Kanал прощаний")
async def setup_leave(inter: disnake.ApplicationCommandInteraction, channel: disnake.TextChannel) -> None:
    set_cfg(inter.guild, "leave_channel", str(channel.id))  # type: ignore[arg-type]
    await inter.response.send_message(
        components=simple("Настройка", f"Канал прощаний: {channel.mention}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@setup_group.sub_command(name="tickets", description="Отправить панель тикетов в канал")
async def setup_tickets(inter: disnake.ApplicationCommandInteraction, channel: disnake.TextChannel) -> None:
    set_cfg(inter.guild, "ticket_panel_channel", str(channel.id))  # type: ignore[arg-type]

    panel = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("**Система тикетов**"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(
                "Выберите тип обращения ниже. Будет создан приватный канал для общения с модераторами."
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
    await inter.response.send_message(
        components=simple("Настройка", f"Панель тикетов отправлена в {channel.mention}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@setup_group.sub_command(name="info", description="Текущие настройки сервера")
async def setup_info(inter: disnake.ApplicationCommandInteraction) -> None:
    cfg = get_cfg(inter.guild)  # type: ignore[arg-type]
    fields: list[tuple[str, str]] = []
    for key, label in [
        ("log_channel", "Лог-канал"),
        ("welcome_channel", "Канал приветствий"),
        ("leave_channel", "Канал прощаний"),
        ("ticket_panel_channel", "Панель тикетов"),
    ]:
        cid = cfg.get(key)
        fields.append((label, f"<#{cid}>" if cid else "Не задан"))
    await inter.response.send_message(
        components=card("Настройки Haven", fields, "info"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /mute, /unmute
# ---------------------------------------------------------------------------

@bot.slash_command(name="mute", description="Тайм-аут участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(moderate_members=True))
async def mute_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
) -> None:
    if not _check_hierarchy(inter, member):
        return await inter.response.send_message(
            components=simple("Ошибка", "Вы не можете замутить этого участника (иерархия ролей)", "error"),
            flags=MessageFlags(is_components_v2=True),
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

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay(f"**Тайм-аут для {member.display_name}**"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay(f"**Причина:** {reason}"),
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
                disnake.ui.Button(label="28 дней", custom_id=f"mute_28d:{member.id}:{reason}", style=ButtonStyle.danger),
                disnake.ui.Button(label="Свое время", custom_id=f"mute_custom:{member.id}:{reason}", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Отмена", custom_id="mute_cancel", style=ButtonStyle.secondary),
            ),
            accent_colour=Color(COLORS["warn"]),
        )
    ]

    await inter.response.send_message(
        components=comps,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


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
        await inter.response.send_message(
            components=simple("Ошибка", "Участник не найден", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
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
    )

    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(guild, c)
    await _try_dm(
        member,
        simple(
            f"Haven -- {guild.name}",
            f"Вам выдан тайм-аут.\n**Длительность:** {fmt_dur(duration)}\n**Причина:** {reason}",
            "warn",
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
        await inter.response.send_message(
            components=simple("Отменено", "Тайм-аут отменен", "plain"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
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
        await inter.response.send_message(
            components=simple("Ошибка", f"Неверный формат: {raw}. Используйте: 25m / 2h / 7d / 90s", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
        return
    await _apply_mute(inter, member_id, reason, dur)


@bot.slash_command(name="unmute", description="Снять тайм-аут", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(moderate_members=True))
async def unmute_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> None:
    await member.timeout(duration=None, reason=f"Снято модератором {inter.author}")
    c = card(
        "Тайм-аут снят",
        [
            ("Участник", f"{member.mention} ({member.id})"),
            ("Модератор", inter.author.mention),
        ],
        "ok",
    )
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# /warn, /warnings, /clearwarns
# ---------------------------------------------------------------------------

@bot.slash_command(name="warn", description="Выдать предупреждение", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(moderate_members=True))
async def warn_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member, reason: str = "Не указана") -> None:
    if not _check_hierarchy(inter, member):
        return await inter.response.send_message(
            components=simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)  # type: ignore[arg-type]
    warnings.setdefault(gk, {}).setdefault(uk, [])
    warnings[gk][uk].append({"reason": reason, "mod": str(inter.author.id), "ts": now_iso()})
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
    )
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вам выдано предупреждение.\n**Причина:** {reason}\n**Всего варнов:** {warn_count}",
            "warn",
        ),
    )


@bot.slash_command(name="warnings", description="Список предупреждений участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(moderate_members=True))
async def warnings_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> None:
    warnings = _load("warnings.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)  # type: ignore[arg-type]
    warns = warnings.get(gk, {}).get(uk, [])
    if not warns:
        return await inter.response.send_message(
            components=simple("Предупреждения", f"У {member.mention} нет предупреждений", "info"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    children: list[Any] = [disnake.ui.TextDisplay(f"**Предупреждения {member.display_name}**")]
    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    for i, w in enumerate(warns, 1):
        children.append(disnake.ui.TextDisplay(f"**#{i}** | {w['reason']} | <@{w['mod']}> | {w['ts'][:10]}"))
    await inter.response.send_message(
        components=[disnake.ui.Container(*children, accent_colour=Color(COLORS["info"]))],
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@bot.slash_command(name="clearwarns", description="Очистить все варны участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(administrator=True))
async def clearwarns_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> None:
    warnings, flush = save("warnings")
    gk, uk = _guild_key(inter.guild), _user_key(member)  # type: ignore[arg-type]
    if gk in warnings and uk in warnings[gk]:
        warnings[gk][uk] = []
        flush()
    await inter.response.send_message(
        components=simple("Варны очищены", f"Все предупреждения {member.mention} удалены", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /kick
# ---------------------------------------------------------------------------

@bot.slash_command(name="kick", description="Кикнуть участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(kick_members=True))
async def kick_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member, reason: str = "Не указана") -> None:
    if not _check_hierarchy(inter, member):
        return await inter.response.send_message(
            components=simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    cid = add_case(inter.guild, member, "kick", inter.author, reason)  # type: ignore[arg-type]
    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вы были кикнуты.\n**Причина:** {reason}",
            "error",
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
    )
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# /ban, /unban + tempban loop
# ---------------------------------------------------------------------------

@bot.slash_command(name="ban", description="Забанить участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(ban_members=True))
async def ban_cmd(
    inter: disnake.ApplicationCommandInteraction,
    member: disnake.Member,
    reason: str = "Не указана",
    duration: str | None = None,
    delete_messages: int = 0,
) -> None:
    if not _check_hierarchy(inter, member):
        return await inter.response.send_message(
            components=simple("Ошибка", "Иерархия ролей не позволяет", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )

    dur_td: timedelta | None = None
    dur_str: str | None = None
    if duration:
        dur_td = parse_dur(duration)
        if dur_td is None:
            return await inter.response.send_message(
                components=simple("Ошибка", f"Неверный формат длительности: {duration}", "error"),
                flags=MessageFlags(is_components_v2=True),
                ephemeral=True,
            )
        dur_str = fmt_dur(dur_td)

    cid = add_case(inter.guild, member, "ban", inter.author, reason, dur_str)  # type: ignore[arg-type]

    await _try_dm(
        member,
        simple(
            f"Haven -- {inter.guild.name}",  # type: ignore[union-attr]
            f"Вы были забанены.\n**Причина:** {reason}"
            + (f"\n**Длительность:** {dur_str}" if dur_str else ""),
            "error",
        ),
    )

    delete_seconds = min(delete_messages * 86400, 604800) if delete_messages else 0
    await inter.guild.ban(member, reason=reason, delete_message_seconds=delete_seconds)  # type: ignore[union-attr]

    if dur_td is not None:
        tempbans, flush = save("tempbans")
        gk = _guild_key(inter.guild)  # type: ignore[arg-type]
        tempbans.setdefault(gk, {})[str(member.id)] = {
            "expires": now_ts() + int(dur_td.total_seconds()),
            "reason": reason,
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

    c = card("Бан", fields, "error")
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(name="unban", description="Разбанить пользователя по ID", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(ban_members=True))
async def unban_cmd(inter: disnake.ApplicationCommandInteraction, user_id: str, reason: str = "Не указана") -> None:
    try:
        user = await bot.fetch_user(int(user_id))
    except (ValueError, disnake.NotFound):
        return await inter.response.send_message(
            components=simple("Ошибка", "Пользователь не найден", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )

    await inter.guild.unban(user, reason=reason)  # type: ignore[union-attr]

    tempbans, flush = save("tempbans")
    gk = _guild_key(inter.guild)  # type: ignore[arg-type]
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
    )
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


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
                        simple("Авто-разбан", f"{user} ({user.id}) -- временный бан истек", "ok"),
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


# ---------------------------------------------------------------------------
# /purge
# ---------------------------------------------------------------------------

@bot.slash_command(name="purge", description="Удалить сообщения", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(manage_messages=True))
async def purge_cmd(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(ge=1, le=100),
    member: disnake.Member | None = None,
) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)

    def check(m: disnake.Message) -> bool:
        if member is not None:
            return m.author.id == member.id
        return True

    deleted = await inter.channel.purge(limit=amount, check=check)
    await inter.response.send_message(
        components=simple("Очистка", f"Удалено сообщений: {len(deleted)}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await send_log(
        inter.guild,  # type: ignore[arg-type]
        simple(
            "Очистка",
            f"**Канал:** {inter.channel.mention}\n**Удалено:** {len(deleted)}\n**Модератор:** {inter.author.mention}",
            "info",
        ),
    )


# ---------------------------------------------------------------------------
# /slowmode
# ---------------------------------------------------------------------------

@bot.slash_command(name="slowmode", description="Медленный режим", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(manage_channels=True))
async def slowmode_cmd(inter: disnake.ApplicationCommandInteraction, seconds: int = commands.Param(ge=0, le=21600)) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    await inter.channel.edit(slowmode_delay=seconds)
    text = f"Медленный режим: {seconds}с" if seconds else "Медленный режим отключен"
    await inter.response.send_message(
        components=simple("Slowmode", text, "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /lock, /unlock
# ---------------------------------------------------------------------------

@bot.slash_command(name="lock", description="Заблокировать канал", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(manage_channels=True))
async def lock_cmd(inter: disnake.ApplicationCommandInteraction, reason: str = "Не указана") -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = False
    await inter.channel.set_permissions(inter.guild.default_role, overwrite=overwrite, reason=reason)  # type: ignore[union-attr]
    c = simple("Канал заблокирован", f"**Причина:** {reason}", "warn")
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


@bot.slash_command(name="unlock", description="Разблокировать канал", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(manage_channels=True))
async def unlock_cmd(inter: disnake.ApplicationCommandInteraction) -> None:
    assert isinstance(inter.channel, disnake.TextChannel)
    overwrite = inter.channel.overwrites_for(inter.guild.default_role)  # type: ignore[union-attr]
    overwrite.send_messages = None
    await inter.channel.set_permissions(inter.guild.default_role, overwrite=overwrite)  # type: ignore[union-attr]
    c = simple("Канал разблокирован", "Отправка сообщений восстановлена", "ok")
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(inter.guild, c)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# /userinfo
# ---------------------------------------------------------------------------

@bot.slash_command(name="userinfo", description="Информация о пользователе", contexts=GUILD_ONLY)
async def userinfo_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member | None = None) -> None:
    member = member or inter.author  # type: ignore[assignment]
    assert isinstance(member, disnake.Member)
    warnings = _load("warnings.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)  # type: ignore[arg-type]
    warn_count = len(warnings.get(gk, {}).get(uk, []))

    created = disnake.utils.format_dt(member.created_at, "F")
    joined = disnake.utils.format_dt(member.joined_at, "F") if member.joined_at else "N/A"

    roles = ", ".join(r.mention for r in reversed(member.roles[1:])) if len(member.roles) > 1 else "Нет"

    timeout_text = "Нет"
    if member.current_timeout and member.current_timeout > datetime.now(timezone.utc):
        timeout_text = f"До {disnake.utils.format_dt(member.current_timeout, 'F')}"

    text_lines = [
        f"**Ник:** {member.display_name}",
        f"**ID:** {member.id}",
        f"**Создан:** {created}",
        f"**Присоединился:** {joined}",
        f"**Варны:** {warn_count}",
        f"**Тайм-аут:** {timeout_text}",
        f"**Роли:** {roles}",
    ]

    avatar_url = member.display_avatar.url

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay("\n".join(text_lines)),
                accessory=disnake.ui.Thumbnail(avatar_url),
            ),
            accent_colour=Color(COLORS["info"]),
        )
    ]
    await inter.response.send_message(
        components=comps,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------

@bot.slash_command(name="history", description="История кейсов участника", contexts=GUILD_ONLY, default_member_permissions=disnake.Permissions(moderate_members=True))
async def history_cmd(inter: disnake.ApplicationCommandInteraction, member: disnake.Member) -> None:
    cases = _load("cases.json", {})
    gk, uk = _guild_key(inter.guild), _user_key(member)  # type: ignore[arg-type]
    user_cases = cases.get(gk, {}).get(uk, [])
    if not user_cases:
        return await inter.response.send_message(
            components=simple("История", f"У {member.mention} нет кейсов", "info"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )

    last_10 = user_cases[-10:]
    children: list[Any] = [disnake.ui.TextDisplay(f"**История {member.display_name}**")]
    children.append(disnake.ui.Separator(spacing=SeparatorSpacing.small))
    for c in last_10:
        dur_part = f" | {c['duration']}" if c.get("duration") else ""
        children.append(
            disnake.ui.TextDisplay(
                f"**#{c['id']}** {c['action']} | {c['reason']} | <@{c['mod']}> | {c['ts'][:10]}{dur_part}"
            )
        )
    await inter.response.send_message(
        components=[disnake.ui.Container(*children, accent_colour=Color(COLORS["info"]))],
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Ticket system
# ---------------------------------------------------------------------------

_TICKET_LABELS: dict[str, str] = {
    "question": "Вопрос",
    "complaint": "Жалоба",
    "appeal": "Апелляция",
    "bug": "Баг",
    "partnership": "Партнерство",
    "other": "Другое",
}


async def _get_or_create_ticket_category(guild: disnake.Guild) -> disnake.CategoryChannel:
    for cat in guild.categories:
        if cat.name.lower() == TICKET_CATEGORY_NAME.lower():
            return cat
    return await guild.create_category(TICKET_CATEGORY_NAME)


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
        return await inter.response.send_message(
            components=simple("Тикет", f"У вас уже открыт тикет: <#{existing}>", "warn"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )

    category = await _get_or_create_ticket_category(guild)

    overwrites: dict[disnake.Role | disnake.Member, disnake.PermissionOverwrite] = {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        guild.me: disnake.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        author: disnake.PermissionOverwrite(view_channel=True, send_messages=True),  # type: ignore[index]
    }

    for role in guild.roles:
        if role.permissions.kick_members and not role.is_default():
            overwrites[role] = disnake.PermissionOverwrite(view_channel=True, send_messages=True)

    channel = await guild.create_text_channel(
        name=f"ticket-{author.name}",
        category=category,
        topic=f"{label} | {author} | Средний",
        overwrites=overwrites,
    )

    tickets.setdefault(gk, {})[uk] = str(channel.id)
    flush()

    avatar_url = author.display_avatar.url

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"**Тикет: {label}**\n**Автор:** {author.mention}\n**Приоритет:** Средний"
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
                disnake.ui.Button(label="Закрыть тикет", custom_id=f"ticket_close:{channel.id}", style=ButtonStyle.danger),
                disnake.ui.Button(label="Сохранить транскрипт", custom_id=f"ticket_transcript:{channel.id}", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Claim", custom_id=f"ticket_claim:{channel.id}", style=ButtonStyle.secondary),
            ),
            accent_colour=Color(COLORS["plain"]),
        )
    ]

    await channel.send(components=comps, flags=MessageFlags(is_components_v2=True))

    await inter.response.send_message(
        components=simple("Тикет создан", f"Ваш тикет: {channel.mention}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )

    await send_log(
        guild,
        simple("Тикет открыт", f"**Тип:** {label}\n**Автор:** {author.mention}\n**Канал:** {channel.mention}", "info"),
    )


@bot.listen("on_string_select")
async def ticket_priority_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_priority:"):
        return
    if not inter.author.guild_permissions.kick_members:  # type: ignore[union-attr]
        return await inter.response.send_message(
            components=simple("Ошибка", "Только модераторы могут менять приоритет", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    priority_map = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}
    priority = priority_map.get(inter.values[0], "Средний")
    channel = inter.channel
    if isinstance(channel, disnake.TextChannel) and channel.topic:
        parts = channel.topic.split(" | ")
        if len(parts) >= 3:
            parts[2] = priority
        new_topic = " | ".join(parts)
        await channel.edit(topic=new_topic)
    await inter.response.send_message(
        components=simple("Приоритет", f"Приоритет изменен: {priority}", "ok"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@bot.listen("on_user_select")
async def ticket_add_user_select(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_add_user:"):
        return
    if not inter.author.guild_permissions.kick_members:  # type: ignore[union-attr]
        return await inter.response.send_message(
            components=simple("Ошибка", "Только модераторы могут добавлять участников", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    channel = inter.channel
    if not isinstance(channel, disnake.TextChannel):
        return
    for user in inter.resolved_values:
        if isinstance(user, disnake.Member):
            await channel.set_permissions(user, view_channel=True, send_messages=True)
    mentions = ", ".join(u.mention for u in inter.resolved_values)
    await inter.response.send_message(
        components=simple("Участник добавлен", f"{mentions} добавлен(ы) в тикет", "ok"),
        flags=MessageFlags(is_components_v2=True),
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
    c = simple("Claim", f"Тикет взят модератором {inter.author.mention}", "info")
    await inter.response.send_message(
        components=c,
        flags=MessageFlags(is_components_v2=True),
    )
    await send_log(
        guild,
        simple("Claim", f"**Модератор:** {inter.author.mention}\n**Канал:** {inter.channel.mention}", "info"),  # type: ignore[union-attr]
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
    await inter.response.send_message(
        components=simple("Транскрипт", "Транскрипт сохранен", "ok"),
        flags=MessageFlags(is_components_v2=True),
        file=file,
        ephemeral=True,
    )


async def _build_transcript(channel: disnake.TextChannel) -> str:
    messages: list[disnake.Message] = []
    async for msg in channel.history(limit=500, oldest_first=True):
        messages.append(msg)
    lines: list[str] = []
    for msg in messages:
        ts = msg.created_at.strftime("%d.%m %H:%M")
        lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {msg.content}")
    return "\n".join(lines)


@bot.listen("on_button_click")
async def ticket_close_btn(inter: disnake.MessageInteraction) -> None:
    cid = inter.component.custom_id or ""
    if not cid.startswith("ticket_close:"):
        return

    channel_id = cid.split(":", 1)[1]

    comps = [
        disnake.ui.Container(
            disnake.ui.TextDisplay("**Закрытие тикета**"),
            disnake.ui.Separator(spacing=SeparatorSpacing.small),
            disnake.ui.TextDisplay("Выберите действие:"),
            disnake.ui.ActionRow(
                disnake.ui.Button(label="Удалить канал", custom_id=f"ticket_delete:{channel_id}", style=ButtonStyle.danger),
                disnake.ui.Button(label="Архивировать", custom_id=f"ticket_archive:{channel_id}", style=ButtonStyle.secondary),
                disnake.ui.Button(label="Отмена", custom_id="ticket_close_cancel", style=ButtonStyle.secondary),
            ),
            accent_colour=Color(COLORS["warn"]),
        )
    ]
    await inter.response.send_message(
        components=comps,
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


@bot.listen("on_button_click")
async def ticket_close_cancel_btn(inter: disnake.MessageInteraction) -> None:
    if (inter.component.custom_id or "") != "ticket_close_cancel":
        return
    await inter.response.send_message(
        components=simple("Отменено", "Закрытие тикета отменено", "plain"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )


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

    channel = guild.get_channel(int(channel_id))
    await send_log(
        guild,
        simple("Тикет удален", f"**Канал:** ticket (ID: {channel_id})\n**Модератор:** {inter.author.mention}", "error"),
    )
    await inter.response.send_message(
        components=simple("Удаление", "Канал будет удален через 2 секунды", "error"),
        flags=MessageFlags(is_components_v2=True),
        ephemeral=True,
    )
    await asyncio.sleep(2)
    if channel and isinstance(channel, disnake.TextChannel):
        await channel.delete(reason="Тикет удален")


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
                components=simple("Тикет архивирован", f"**Канал:** {channel.name}", "info"),
                flags=MessageFlags(is_components_v2=True),
                file=file_for_log,
            )

    if author_id:
        author = guild.get_member(int(author_id))
        if author:
            await channel.set_permissions(author, send_messages=False)
            file_for_dm = disnake.File(io.StringIO(transcript), filename=f"transcript-{channel.name}.txt")
            try:
                await author.send(
                    components=simple("Тикет архивирован", f"Ваш тикет в {guild.name} был архивирован", "info"),
                    flags=MessageFlags(is_components_v2=True),
                    file=file_for_dm,
                )
            except disnake.Forbidden:
                pass

    await channel.edit(name=f"archived-{channel.name}")

    await inter.response.send_message(
        components=simple("Архивировано", "Тикет архивирован", "ok"),
        flags=MessageFlags(is_components_v2=True),
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


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_member_join(member: disnake.Member) -> None:
    cfg = get_cfg(member.guild)
    ch_id = cfg.get("welcome_channel")
    if not ch_id:
        return
    ch = member.guild.get_channel(int(ch_id))
    if not ch or not isinstance(ch, disnake.TextChannel):
        return
    avatar_url = member.display_avatar.url
    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"**Добро пожаловать!**\n{member.mention} присоединился к серверу."
                ),
                accessory=disnake.ui.Thumbnail(avatar_url),
            ),
            accent_colour=Color(COLORS["ok"]),
        )
    ]
    await ch.send(components=comps, flags=MessageFlags(is_components_v2=True))


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

    comps = [
        disnake.ui.Container(
            disnake.ui.Section(
                disnake.ui.TextDisplay(
                    f"**Участник покинул сервер**\n{member} ({member.id})\n**Роли:** {roles}\n**Был на сервере:** {time_on_server}"
                ),
                accessory=disnake.ui.Thumbnail(avatar_url),
            ),
            accent_colour=Color(COLORS["error"]),
        )
    ]
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

    lines: list[str] = [f"**Изменение ролей:** {after.mention} ({after.id})"]
    if added:
        lines.append(f"**Добавлены:** {', '.join(r.name for r in added)}")
    if removed:
        lines.append(f"**Убраны:** {', '.join(r.name for r in removed)}")

    await send_log(after.guild, simple("Роли", "\n".join(lines), "info"))


@bot.event
async def on_message_delete(message: disnake.Message) -> None:
    if message.author.bot or not message.guild:
        return
    text = message.content[:1000] if message.content else "(пусто)"
    await send_log(
        message.guild,
        simple(
            "Сообщение удалено",
            f"**Автор:** {message.author.mention} ({message.author.id})\n"
            f"**Канал:** {message.channel.mention}\n"
            f"**Текст:** {text}",
            "error",
        ),
    )


@bot.event
async def on_message_edit(before: disnake.Message, after: disnake.Message) -> None:
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return
    old = before.content[:500] if before.content else "(пусто)"
    new = after.content[:500] if after.content else "(пусто)"
    await send_log(
        before.guild,
        simple(
            "Сообщение отредактировано",
            f"**Автор:** {before.author.mention} ({before.author.id})\n"
            f"**Канал:** {before.channel.mention}\n"
            f"**Было:** {old}\n"
            f"**Стало:** {new}",
            "warn",
        ),
    )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

@bot.event
async def on_slash_command_error(inter: disnake.ApplicationCommandInteraction, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await inter.response.send_message(
            components=simple("Доступ закрыт", "Недостаточно прав", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    elif isinstance(error, commands.BotMissingPermissions):
        await inter.response.send_message(
            components=simple("Ошибка бота", "У бота недостаточно прав", "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
    else:
        await inter.response.send_message(
            components=simple("Ошибка", str(error)[:400], "error"),
            flags=MessageFlags(is_components_v2=True),
            ephemeral=True,
        )
        raise error


# ---------------------------------------------------------------------------
# Bot ready + launch
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    print(f"Haven is online as {bot.user} ({bot.user.id})")  # type: ignore[union-attr]
    if not tempban_check.is_running():
        tempban_check.start()


def main() -> None:
    token = os.environ.get("HAVEN_TOKEN")
    if not token:
        print("HAVEN_TOKEN is not set")
        raise SystemExit(1)
    bot.run(token)


if __name__ == "__main__":
    main()
