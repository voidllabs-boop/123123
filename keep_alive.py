"""
Haven Keep-Alive & Web Dashboard
=================================
Полноценный веб-сервер на aiohttp для:
  - Keep-alive ping (UptimeRobot, BetterStack, Cron-job.org и т.д.)
  - Веб-панель управления ботом (без Discord — прямо через браузер)
  - REST API для чтения/записи конфигов, авто-модерации, варнов, кейсов и т.д.
  - Живые метрики бота (латентность, guilds, uptime, ...)
  - Webhook-endpoint (Discord, GitHub и т.д.)
  - Отправка сообщений в каналы прямо с панели

Запуск: импортируй keep_alive и вызови keep_alive.start(bot) до bot.run()
        или используй asyncio — оба варианта поддерживаются.

Окружение:
  DASHBOARD_SECRET  — секрет для входа в панель (обязателен)
  DASHBOARD_PORT    — порт (по умолчанию 8080)
  DASHBOARD_HOST    — хост (по умолчанию 0.0.0.0)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

log = logging.getLogger("haven.dashboard")

# ───────────────────────────────────────────────
#  Конфигурация
# ───────────────────────────────────────────────

DASHBOARD_PORT   = int(os.environ.get("DASHBOARD_PORT", 8080))
DASHBOARD_HOST   = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "changeme")
WH_SECRET        = os.environ.get("WEBHOOK_SECRET", "")  # для GitHub webhooks

# Сколько активных сессий хранить
MAX_SESSIONS = 64
_sessions: dict[str, dict] = {}  # token -> {guild_id, exp, username}

# Ссылка на бот (заполняется в start())
_bot: Any = None

_start_time = time.time()

# ═══════════════════════════════════════════════════════════════════════════
#  SESSION HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _new_session(guild_id: str, username: str) -> str:
    if len(_sessions) >= MAX_SESSIONS:
        oldest = min(_sessions, key=lambda t: _sessions[t]["exp"])
        _sessions.pop(oldest, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "guild_id": guild_id,
        "username": username,
        "exp": time.time() + 86400 * 7,  # 7 дней
        "created": datetime.now(timezone.utc).isoformat(),
    }
    return token


def _check_session(request: web.Request) -> dict | None:
    token = request.cookies.get("haven_session") or request.headers.get("X-Haven-Token")
    if not token:
        return None
    sess = _sessions.get(token)
    if not sess or sess["exp"] < time.time():
        _sessions.pop(token, None)
        return None
    return sess


def _require_session(handler):
    async def wrapper(request: web.Request):
        sess = _check_session(request)
        if sess is None:
            if request.path.startswith("/api/"):
                raise web.HTTPUnauthorized(reason="Not authenticated")
            raise web.HTTPFound("/login")
        request["session"] = sess
        return await handler(request)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS для работы с данными бота
# ═══════════════════════════════════════════════════════════════════════════

def _get_guild(guild_id: str):
    if _bot is None:
        return None
    try:
        return _bot.get_guild(int(guild_id))
    except Exception:
        return None


def _bot_uptime() -> str:
    delta = int(time.time() - _start_time)
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    parts.append(f"{s}с")
    return " ".join(parts)


def _guild_info(guild) -> dict:
    if guild is None:
        return {}
    return {
        "id":           str(guild.id),
        "name":         guild.name,
        "icon":         str(guild.icon.url) if guild.icon else None,
        "member_count": guild.member_count,
        "channel_count": len(guild.channels),
        "role_count":   len(guild.roles),
        "boost_level":  guild.premium_tier,
        "boosts":       guild.premium_subscription_count,
        "owner_id":     str(guild.owner_id),
        "created_at":   guild.created_at.isoformat(),
    }


def _get_store():
    """Получить _store из main.py если доступен."""
    try:
        import main as m
        return m._store, m._save, m._load
    except Exception:
        return {}, None, None


# ═══════════════════════════════════════════════════════════════════════════
#  HTML ШАБЛОНЫ
# ═══════════════════════════════════════════════════════════════════════════

_HTML_BASE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Haven Dashboard</title>
<style>
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;
  --accent:#5865f2;--accent2:#4752c4;--ok:#57f287;--warn:#fee75c;
  --error:#ed4245;--text:#e6edf3;--text2:#8b949e;--radius:8px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Layout */
.layout{display:flex;min-height:100vh}
.sidebar{width:240px;background:var(--surface);border-right:1px solid var(--border);
  padding:16px;display:flex;flex-direction:column;gap:4px;flex-shrink:0;position:fixed;
  top:0;left:0;bottom:0;overflow-y:auto;z-index:100}
.main{flex:1;margin-left:240px;padding:24px;max-width:1200px}

/* Sidebar */
.logo{font-size:22px;font-weight:700;color:var(--accent);padding:8px 0 20px;
  letter-spacing:.5px;border-bottom:1px solid var(--border);margin-bottom:12px}
.logo span{font-size:11px;font-weight:400;color:var(--text2);display:block;margin-top:2px}
.nav-section{font-size:11px;text-transform:uppercase;color:var(--text2);
  letter-spacing:1px;padding:12px 8px 4px}
.nav-link{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:var(--radius);
  color:var(--text2);font-size:14px;transition:.15s;cursor:pointer;border:none;
  background:none;width:100%;text-align:left}
.nav-link:hover,.nav-link.active{background:var(--surface2);color:var(--text)}
.nav-link.active{color:var(--accent)}
.nav-link svg{flex-shrink:0;opacity:.8}

/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:20px}
.card-title{font-size:16px;font-weight:600;margin-bottom:16px;display:flex;
  align-items:center;gap:8px}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}

/* Stat cards */
.stat{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px}
.stat-label{font-size:12px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:28px;font-weight:700;margin-top:4px}
.stat-sub{font-size:12px;color:var(--text2);margin-top:2px}

/* Forms */
.form-group{margin-bottom:16px}
label{display:block;font-size:13px;color:var(--text2);margin-bottom:6px;font-weight:500}
input,select,textarea{width:100%;padding:8px 12px;background:var(--surface2);
  border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;
  transition:.15s;outline:none;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(88,101,242,.15)}
textarea{resize:vertical;min-height:80px}
select option{background:var(--surface2)}

.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:6px;
  font-size:14px;font-weight:500;cursor:pointer;border:none;transition:.15s;
  font-family:inherit}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent2)}
.btn-danger{background:var(--error);color:#fff}
.btn-danger:hover{background:#c93235}
.btn-ghost{background:transparent;color:var(--text2);border:1px solid var(--border)}
.btn-ghost:hover{background:var(--surface2);color:var(--text)}
.btn-sm{padding:5px 10px;font-size:12px}
.btn-success{background:var(--ok);color:#000}

/* Table */
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;padding:8px 12px;font-size:12px;text-transform:uppercase;
  letter-spacing:.5px;color:var(--text2);border-bottom:1px solid var(--border)}
td{padding:10px 12px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surface2)}

/* Badges */
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600}
.badge-ok{background:rgba(87,242,135,.15);color:var(--ok)}
.badge-warn{background:rgba(254,231,92,.15);color:var(--warn)}
.badge-error{background:rgba(237,66,69,.15);color:var(--error)}
.badge-info{background:rgba(88,101,242,.15);color:var(--accent)}

/* Toggle */
.toggle{position:relative;display:inline-block;width:40px;height:22px}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--border);border-radius:22px;
  cursor:pointer;transition:.3s}
.slider:before{content:'';position:absolute;width:16px;height:16px;
  left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
input:checked + .slider{background:var(--accent)}
input:checked + .slider:before{transform:translateX(18px)}

/* Alerts */
.alert{padding:12px 16px;border-radius:6px;font-size:14px;margin-bottom:16px;
  display:flex;align-items:center;gap:8px}
.alert-ok{background:rgba(87,242,135,.1);border:1px solid rgba(87,242,135,.3);color:var(--ok)}
.alert-error{background:rgba(237,66,69,.1);border:1px solid rgba(237,66,69,.3);color:var(--error)}
.alert-warn{background:rgba(254,231,92,.1);border:1px solid rgba(254,231,92,.3);color:var(--warn)}
.alert-info{background:rgba(88,101,242,.1);border:1px solid rgba(88,101,242,.3);color:var(--accent)}

/* Tabs */
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:20px}
.tab{padding:8px 16px;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;
  color:var(--text2);transition:.15s;background:none;border-top:none;
  border-left:none;border-right:none;font-family:inherit}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{display:none}
.tab-content.active{display:block}

/* Misc */
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.text-sm{font-size:13px}
.text-muted{color:var(--text2)}
.flex{display:flex;gap:8px;align-items:center}
.flex-wrap{flex-wrap:wrap}
.mt-12{margin-top:12px}
.avatar{width:32px;height:32px;border-radius:50%;background:var(--surface2)}
.guild-icon{width:48px;height:48px;border-radius:12px;background:var(--surface2)}
.code{font-family:monospace;font-size:12px;background:var(--surface2);
  padding:2px 6px;border-radius:4px;color:var(--accent)}
.spinner{border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;width:20px;height:20px;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.online-dot{width:8px;height:8px;border-radius:50%;background:var(--ok);
  display:inline-block}

/* Login */
.login-wrap{display:flex;justify-content:center;align-items:center;min-height:100vh}
.login-card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:40px;width:100%;max-width:360px}
.login-logo{font-size:28px;font-weight:700;color:var(--accent);text-align:center;
  margin-bottom:8px}
.login-sub{text-align:center;color:var(--text2);font-size:14px;margin-bottom:28px}

/* Toast */
#toast-container{position:fixed;top:20px;right:20px;z-index:9999;
  display:flex;flex-direction:column;gap:8px}
.toast{padding:12px 16px;border-radius:8px;font-size:14px;min-width:240px;
  animation:slide-in .3s ease;backdrop-filter:blur(8px)}
.toast-ok{background:rgba(87,242,135,.9);color:#000}
.toast-error{background:rgba(237,66,69,.9);color:#fff}
.toast-info{background:rgba(88,101,242,.9);color:#fff}
@keyframes slide-in{from{transform:translateX(100%);opacity:0}to{transform:none;opacity:1}}

/* Responsive */
@media(max-width:768px){
  .sidebar{transform:translateX(-100%);transition:.3s}
  .sidebar.open{transform:none}
  .main{margin-left:0}
  .card-grid{grid-template-columns:1fr}
}

/* Log viewer */
#log-box{background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;padding:12px;font-family:monospace;font-size:12px;
  max-height:400px;overflow-y:auto;line-height:1.6}
.log-entry{padding:2px 0;border-bottom:1px solid var(--border)}
.log-info{color:#79c0ff}
.log-warn{color:var(--warn)}
.log-error{color:var(--error)}
.log-ok{color:var(--ok)}

/* Channel send */
.channel-send{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:end}
</style>
</head>
<body>
<div id="toast-container"></div>
{BODY}
<script>
// ── Toast ───────────────────────────────────────────────────
function toast(msg, type='info'){
  const el=document.createElement('div');
  el.className=`toast toast-${type}`;
  el.textContent=msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(()=>el.remove(),4000);
}

// ── Tabs ────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab=>{
  tab.addEventListener('click',()=>{
    const group=tab.dataset.group;
    document.querySelectorAll(`.tab[data-group="${group}"]`).forEach(t=>t.classList.remove('active'));
    document.querySelectorAll(`.tab-content[data-group="${group}"]`).forEach(c=>c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.target)?.classList.add('active');
  });
});

// ── API helpers ─────────────────────────────────────────────
async function api(method, path, body=null){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body) opts.body=JSON.stringify(body);
  const r=await fetch(path,opts);
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error||`HTTP ${r.status}`);
  return data;
}

async function doGet(path){ return api('GET',path); }
async function doPost(path,body){ return api('POST',path,body); }
async function doPatch(path,body){ return api('PATCH',path,body); }
async function doDelete(path){ return api('DELETE',path); }

// ── Confirm ─────────────────────────────────────────────────
async function confirmAction(msg){
  return new Promise(res=>{
    if(window.confirm(msg)) res(true); else res(false);
  });
}

// ── Nav highlight ────────────────────────────────────────────
document.querySelectorAll('.nav-link[href]').forEach(l=>{
  if(l.href===location.href) l.classList.add('active');
});

// ── Mobile sidebar ───────────────────────────────────────────
const hamburger=document.getElementById('hamburger');
const sidebar=document.querySelector('.sidebar');
hamburger?.addEventListener('click',()=>sidebar?.classList.toggle('open'));
</script>
</body></html>"""

_SIDEBAR = """
<div class="sidebar" id="sidebar">
  <div class="logo">⚓ Haven<span>Dashboard v2.0</span></div>

  <div class="nav-section">Обзор</div>
  <a href="/" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M3 13h1v7c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-7h1a1 1 0 0 0 .7-1.7l-9-9a1 1 0 0 0-1.4 0l-9 9A1 1 0 0 0 3 13z"/></svg>Главная</a>
  <a href="/stats" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 14H7v-2h5v2zm5-4H7v-2h10v2zm0-4H7V7h10v2z"/></svg>Статистика</a>
  <a href="/logs" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm-1 7V3.5L18.5 9H13z"/></svg>Логи</a>

  <div class="nav-section">Управление</div>
  <a href="/config" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>Конфигурация</a>
  <a href="/automod" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/></svg>Авто-модерация</a>
  <a href="/moderation" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2z"/></svg>Модерация</a>
  <a href="/warnings" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>Варны / Кейсы</a>
  <a href="/tickets" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4c-1.11 0-2 .89-2 2v12c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm-1 14H5c-.55 0-1-.45-1-1V7c0-.55.45-1 1-1h14c.55 0 1 .45 1 1v10c0 .55-.45 1-1 1z"/></svg>Тикеты</a>

  <div class="nav-section">Инструменты</div>
  <a href="/send" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>Отправить сообщение</a>
  <a href="/channels" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>Каналы</a>
  <a href="/roles" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>Роли</a>
  <a href="/members" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>Участники</a>
  <a href="/giveaways" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>Розыгрыши</a>

  <div class="nav-section">Система</div>
  <a href="/api-docs" class="nav-link"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M9.4 16.6L4.8 12l4.6-4.6L8 6l-6 6 6 6 1.4-1.4zm5.2 0l4.6-4.6-4.6-4.6L16 6l6 6-6 6-1.4-1.4z"/></svg>API Docs</a>
  <a href="/logout" class="nav-link" style="margin-top:auto;color:var(--error)"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>Выход</a>
  </div>
"""

# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: LOGIN
# ═══════════════════════════════════════════════════════════════════════════

async def handle_login(request: web.Request) -> web.Response:
    if _check_session(request):
        raise web.HTTPFound("/")
    error = ""
    if request.method == "POST":
        data = await request.post()
        password = data.get("password", "")
        guild_id  = data.get("guild_id", "")
        username  = data.get("username", "Haven Admin")
        if hmac.compare_digest(password, DASHBOARD_SECRET):
            token = _new_session(guild_id, username)
            response = web.HTTPFound("/")
            response.set_cookie("haven_session", token, max_age=604800, httponly=True, samesite="Lax")
            raise response
        else:
            error = "Неверный пароль"

    # Build guild select
    guild_options = ""
    if _bot:
        for g in sorted(_bot.guilds, key=lambda x: x.name):
            guild_options += f'<option value="{g.id}">{g.name}</option>'

    body = f"""
<div class="login-wrap">
  <div class="login-card">
    <div class="login-logo">⚓ Haven</div>
    <div class="login-sub">Панель управления ботом</div>
    {'<div class="alert alert-error">'+error+'</div>' if error else ''}
    <form method="POST" action="/login">
      <div class="form-group">
        <label>Ваше имя (для логов)</label>
        <input name="username" value="Admin" required>
      </div>
      <div class="form-group">
        <label>Сервер</label>
        <select name="guild_id">
          <option value="">— Выберите сервер —</option>
          {guild_options}
        </select>
      </div>
      <div class="form-group">
        <label>Пароль (DASHBOARD_SECRET)</label>
        <input type="password" name="password" required autofocus>
      </div>
      <button type="submit" class="btn btn-primary" style="width:100%">Войти</button>
    </form>
  </div>
</div>"""
    html = _HTML_BASE.replace("{BODY}", body)
    return web.Response(text=html, content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: HOME / DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_home(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess["guild_id"])
    gi = _guild_info(guild) if guild else {}
    latency = round(_bot.latency * 1000, 1) if _bot else 0
    guild_count = len(_bot.guilds) if _bot else 0
    _store, _, _ = _get_store()
    warns_count = sum(len(v) for gw in _store.get("warnings.json", {}).values() for v in gw.values()) if _store else 0
    cases_count = sum(len(v) for gc in _store.get("cases.json", {}).values() for v in gc.values()) if _store else 0
    tickets_data = _store.get("tickets.json", {}) if _store else {}
    ticket_count = sum(len(v) for v in tickets_data.values())

    latency_color = "ok" if latency < 100 else "warn" if latency < 300 else "error"

    guild_banner = ""
    if gi:
        guild_banner = f"""
        <div class="card" style="margin-bottom:20px">
          <div class="flex" style="gap:16px">
            {'<img src="'+gi["icon"]+'" class="guild-icon">' if gi.get("icon") else '<div class="guild-icon"></div>'}
            <div>
              <div style="font-size:20px;font-weight:700">{gi["name"]}</div>
              <div class="text-muted text-sm">ID: <span class="code">{gi["id"]}</span> · Boost Level {gi["boost_level"]} · {gi["boosts"]} бустов</div>
            </div>
            <div style="margin-left:auto;text-align:right">
              <div class="badge badge-ok"><span class="online-dot" style="margin-right:4px"></span>Online</div>
              <div class="text-muted text-sm mt-12">{gi["member_count"]} участников</div>
            </div>
          </div>
        </div>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <div class="section-header">
      <h1 style="font-size:22px;font-weight:700">⚓ Haven Dashboard</h1>
      <div class="flex">
        <span class="badge badge-ok">v6.0.0</span>
        <span class="text-muted text-sm">Вошёл как: {sess["username"]}</span>
      </div>
    </div>

    {guild_banner}

    <div class="card-grid">
      <div class="stat">
        <div class="stat-label">Задержка</div>
        <div class="stat-value" style="color:var(--{latency_color})">{latency}ms</div>
        <div class="stat-sub">WebSocket пинг</div>
      </div>
      <div class="stat">
        <div class="stat-label">Серверов</div>
        <div class="stat-value">{guild_count}</div>
        <div class="stat-sub">Всего в Discord</div>
      </div>
      <div class="stat">
        <div class="stat-label">Uptime</div>
        <div class="stat-value" id="uptime-val">{_bot_uptime()}</div>
        <div class="stat-sub">С последнего рестарта</div>
      </div>
      <div class="stat">
        <div class="stat-label">Варны</div>
        <div class="stat-value">{warns_count}</div>
        <div class="stat-sub">Всего в базе</div>
      </div>
      <div class="stat">
        <div class="stat-label">Кейсы</div>
        <div class="stat-value">{cases_count}</div>
        <div class="stat-sub">Всего действий</div>
      </div>
      <div class="stat">
        <div class="stat-label">Тикеты</div>
        <div class="stat-value">{ticket_count}</div>
        <div class="stat-sub">Активных каналов</div>
      </div>
    </div>

    <div class="card" style="margin-top:20px">
      <div class="card-title">🔴 Живые метрики <div class="spinner" id="spinner" style="margin-left:8px"></div></div>
      <div id="live-metrics" class="card-grid"></div>
    </div>

    <div class="card">
      <div class="card-title">📊 Быстрые действия</div>
      <div class="flex flex-wrap">
        <button class="btn btn-primary" onclick="window.location='/send'">📨 Отправить сообщение</button>
        <button class="btn btn-ghost" onclick="window.location='/config'">⚙️ Настройки</button>
        <button class="btn btn-ghost" onclick="window.location='/automod'">🛡️ Авто-мод</button>
        <button class="btn btn-ghost" onclick="window.location='/warnings'">⚠️ Варны</button>
        <button class="btn btn-ghost" onclick="window.location='/logs'">📋 Логи</button>
        <button class="btn btn-danger btn-sm" onclick="restartBot()">🔄 Reload Data</button>
      </div>
    </div>
  </div>
</div>
<script>
async function restartBot(){{
  if(!await confirmAction('Перезагрузить данные бота?')) return;
  try{{
    await doPost('/api/bot/reload',{{}});
    toast('Данные перезагружены','ok');
  }}catch(e){{toast(e.message,'error')}}
}}

// Live metrics poll
async function pollMetrics(){{
  try{{
    const d=await doGet('/api/metrics');
    document.getElementById('spinner').style.display='none';
    const c=document.getElementById('live-metrics');
    c.innerHTML=`
      <div class="stat"><div class="stat-label">Latency</div>
        <div class="stat-value">${{d.latency_ms}}ms</div></div>
      <div class="stat"><div class="stat-label">Guilds</div>
        <div class="stat-value">${{d.guilds}}</div></div>
      <div class="stat"><div class="stat-label">Uptime</div>
        <div class="stat-value" style="font-size:18px">${{d.uptime}}</div></div>
      <div class="stat"><div class="stat-label">Commands</div>
        <div class="stat-value">${{d.commands}}</div></div>
    `;
  }}catch(e){{}}
}}
pollMetrics();
setInterval(pollMetrics, 5000);
</script>"""
    html = _HTML_BASE.replace("{BODY}", body)
    return web.Response(text=html, content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: CONFIG
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_config(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess["guild_id"])
    guild_id_str = sess.get("guild_id", "")

    _store, _, _ = _get_store()
    cfg = _store.get("config.json", {}).get(guild_id_str, {}) if _store else {}

    # Build channel options
    ch_options = '<option value="">— Не задан —</option>'
    role_options = '<option value="">— Не задан —</option>'
    if guild:
        for ch in sorted(guild.text_channels, key=lambda c: c.position):
            ch_options += f'<option value="{ch.id}">{ch.name}</option>'
        for r in sorted(guild.roles[1:], key=lambda r: -r.position):
            role_options += f'<option value="{r.id}">{r.name}</option>'

    def sel(key, options_html):
        current = str(cfg.get(key, ""))
        return options_html.replace(f'value="{current}"', f'value="{current}" selected')

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">⚙️ Конфигурация сервера</h1>
    <div id="save-alert"></div>

    <div class="card">
      <div class="card-title">📢 Каналы</div>
      <div class="card-grid">
        <div class="form-group">
          <label>Канал логов</label>
          <select id="log_channel">{sel("log_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал приветствий</label>
          <select id="welcome_channel">{sel("welcome_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал прощаний</label>
          <select id="leave_channel">{sel("leave_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал тикетов</label>
          <select id="ticket_panel_channel">{sel("ticket_panel_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал предложений</label>
          <select id="suggestions_channel">{sel("suggestions_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал Starboard</label>
          <select id="starboard_channel">{sel("starboard_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал ModMail</label>
          <select id="modmail_channel">{sel("modmail_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал верификации</label>
          <select id="verify_channel">{sel("verify_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал счётчика</label>
          <select id="counting_channel">{sel("counting_channel", ch_options)}</select>
        </div>
        <div class="form-group">
          <label>Канал милестоунов</label>
          <select id="milestone_channel">{sel("milestone_channel", ch_options)}</select>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">👥 Роли</div>
      <div class="card-grid">
        <div class="form-group">
          <label>Роль модератора</label>
          <select id="mod_role">{sel("mod_role", role_options)}</select>
        </div>
        <div class="form-group">
          <label>Роль мьюта</label>
          <select id="mute_role">{sel("mute_role", role_options)}</select>
        </div>
        <div class="form-group">
          <label>Авторолл при входе</label>
          <select id="autorole">{sel("autorole", role_options)}</select>
        </div>
        <div class="form-group">
          <label>Роль верификации</label>
          <select id="verify_role">{sel("verify_role", role_options)}</select>
        </div>
        <div class="form-group">
          <label>Роль карантина</label>
          <select id="quarantine_role">{sel("quarantine_role", role_options)}</select>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">💬 Сообщения</div>
      <div class="form-group">
        <label>DM при входе (welcome_dm)</label>
        <textarea id="welcome_dm" placeholder="Добро пожаловать на сервер!">{cfg.get("welcome_dm","")}</textarea>
      </div>
      <div class="form-group">
        <label>Starboard порог (звёзды)</label>
        <input type="number" id="starboard_threshold" value="{cfg.get("starboard_threshold",3)}" min="1" max="50">
      </div>
      <div class="form-group">
        <label>Starboard эмодзи</label>
        <input id="starboard_emoji" value="{cfg.get("starboard_emoji","⭐")}">
      </div>
    </div>

    <div class="flex" style="gap:12px">
      <button class="btn btn-primary" onclick="saveConfig()">💾 Сохранить всё</button>
      <button class="btn btn-ghost" onclick="loadConfig()">🔄 Перечитать</button>
    </div>
  </div>
</div>

<script>
const CONFIG_KEYS=[
  'log_channel','welcome_channel','leave_channel','ticket_panel_channel',
  'suggestions_channel','starboard_channel','modmail_channel','verify_channel',
  'counting_channel','milestone_channel','mod_role','mute_role','autorole',
  'verify_role','quarantine_role','welcome_dm','starboard_threshold','starboard_emoji'
];

async function saveConfig(){{
  const payload={{}};
  CONFIG_KEYS.forEach(k=>{{
    const el=document.getElementById(k);
    if(el) payload[k]=el.value||null;
  }});
  try{{
    await doPost('/api/config',payload);
    document.getElementById('save-alert').innerHTML='<div class="alert alert-ok">✅ Настройки сохранены</div>';
    toast('Настройки сохранены','ok');
    setTimeout(()=>document.getElementById('save-alert').innerHTML='',3000);
  }}catch(e){{toast(e.message,'error')}}
}}

async function loadConfig(){{
  try{{
    const d=await doGet('/api/config');
    CONFIG_KEYS.forEach(k=>{{
      const el=document.getElementById(k);
      if(el&&d[k]!=null) el.value=d[k];
    }});
    toast('Конфиг перечитан','info');
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: AUTOMOD
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_automod(request: web.Request) -> web.Response:
    sess = request["session"]
    guild_id_str = sess.get("guild_id", "")
    _store, _, _ = _get_store()
    cfg = _store.get("config.json", {}).get(guild_id_str, {}) if _store else {}
    automod = cfg.get("automod", {})

    def toggled(key):
        return "checked" if automod.get(key) else ""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">🛡️ Авто-модерация</h1>
    <div id="am-alert"></div>

    <div class="card">
      <div class="card-title">Настройки авто-мода</div>
      <table>
        <thead><tr><th>Функция</th><th>Описание</th><th>Вкл/Выкл</th></tr></thead>
        <tbody>
          {''.join(f"""
          <tr>
            <td style="font-weight:500">{name}</td>
            <td class="text-muted text-sm">{desc}</td>
            <td><label class="toggle">
              <input type="checkbox" id="{key}" {toggled(key)} onchange="saveAutomod()">
              <span class="slider"></span>
            </label></td>
          </tr>
          """ for key, name, desc in [
              ("anti_spam", "Анти-спам", f"Блок {5} сообщений за {5}с"),
              ("anti_raid", "Анти-рейд", f"Защита от {8}+ входов за {10}с"),
              ("caps_filter", "Фильтр капса", f"Блок сообщений >{70}% заглавных (мин. {10} символов)"),
              ("link_filter", "Фильтр ссылок", "Удалять сообщения с http/https ссылками"),
              ("invite_filter", "Фильтр инвайтов", "Удалять ссылки discord.gg"),
              ("mention_spam", "Анти-ментион спам", f"Блок>{5} упоминаний за раз"),
              ("word_filter", "Фильтр слов", "Кастомный чёрный список слов"),
          ])}
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">📝 Кастомные настройки</div>
      <div class="card-grid">
        <div class="form-group">
          <label>Макс. сообщений/5с (анти-спам)</label>
          <input type="number" id="spam_threshold" value="{automod.get('spam_threshold',5)}" min="1" max="50">
        </div>
        <div class="form-group">
          <label>Макс. входов/10с (анти-рейд)</label>
          <input type="number" id="raid_threshold" value="{automod.get('raid_threshold',8)}" min="1" max="100">
        </div>
        <div class="form-group">
          <label>Макс. упоминаний за сообщение</label>
          <input type="number" id="max_mentions" value="{automod.get('max_mentions',5)}" min="1" max="20">
        </div>
        <div class="form-group">
          <label>Порог капса (%)</label>
          <input type="number" id="caps_pct" value="{int(automod.get('caps_pct',0.7)*100)}" min="50" max="100">
        </div>
      </div>
      <div class="form-group">
        <label>Запрещённые слова (через запятую)</label>
        <textarea id="banned_words" placeholder="слово1, слово2, ...">{', '.join(automod.get('banned_words',[]))}</textarea>
      </div>
      <div class="form-group">
        <label>Разрешённые ссылки (whitelist, через запятую)</label>
        <textarea id="link_whitelist" placeholder="example.com, youtube.com">{', '.join(automod.get('link_whitelist',[]))}</textarea>
      </div>
      <button class="btn btn-primary" onclick="saveAutomodFull()">💾 Сохранить</button>
    </div>
  </div>
</div>

<script>
async function saveAutomod(){{
  const toggles=['anti_spam','anti_raid','caps_filter','link_filter','invite_filter','mention_spam','word_filter'];
  const payload={{}};
  toggles.forEach(k=>{{payload[k]=document.getElementById(k)?.checked||false;}});
  try{{
    await doPatch('/api/automod',payload);
    toast('Авто-мод обновлён','ok');
  }}catch(e){{toast(e.message,'error')}}
}}

async function saveAutomodFull(){{
  const toggles=['anti_spam','anti_raid','caps_filter','link_filter','invite_filter','mention_spam','word_filter'];
  const payload={{}};
  toggles.forEach(k=>{{payload[k]=document.getElementById(k)?.checked||false;}});
  payload.spam_threshold=parseInt(document.getElementById('spam_threshold').value)||5;
  payload.raid_threshold=parseInt(document.getElementById('raid_threshold').value)||8;
  payload.max_mentions=parseInt(document.getElementById('max_mentions').value)||5;
  payload.caps_pct=(parseInt(document.getElementById('caps_pct').value)||70)/100;
  payload.banned_words=document.getElementById('banned_words').value.split(',').map(s=>s.trim()).filter(Boolean);
  payload.link_whitelist=document.getElementById('link_whitelist').value.split(',').map(s=>s.trim()).filter(Boolean);
  try{{
    await doPatch('/api/automod',payload);
    toast('Авто-мод сохранён','ok');
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: MODERATION (варны, кейсы)
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_warnings(request: web.Request) -> web.Response:
    sess = request["session"]
    guild_id_str = sess.get("guild_id", "")
    _store, _, _ = _get_store()
    warns_raw = _store.get("warnings.json", {}).get(guild_id_str, {}) if _store else {}
    cases_raw = _store.get("cases.json", {}).get(guild_id_str, {}) if _store else {}

    guild = _get_guild(guild_id_str)

    def member_name(uid):
        if guild:
            m = guild.get_member(int(uid))
            if m:
                return f"{m.display_name}"
        return f"<{uid}>"

    # Warns table
    warn_rows = ""
    for uid, warn_list in warns_raw.items():
        name = member_name(uid)
        for i, w in enumerate(warn_list):
            warn_rows += f"""<tr>
              <td><span class="code">{uid}</span></td>
              <td>{name}</td>
              <td>{w.get("reason","-")}</td>
              <td class="text-muted text-sm">{w.get("mod","-")}</td>
              <td class="text-muted text-sm">{w.get("ts","")[:10] if w.get("ts") else "-"}</td>
              <td>
                <button class="btn btn-danger btn-sm" onclick="delWarn('{uid}',{i})">✕</button>
              </td>
            </tr>"""

    # Cases table
    case_rows = ""
    action_colors = {"ban":"error","kick":"warn","mute":"warn","warn":"warn","unban":"ok","unmute":"ok"}
    for uid, case_list in cases_raw.items():
        name = member_name(uid)
        for c in case_list:
            action = c.get("action","?")
            badge_type = action_colors.get(action.lower(),"info")
            case_rows += f"""<tr>
              <td class="text-muted">#{c.get("id","?")}</td>
              <td><span class="badge badge-{badge_type}">{action}</span></td>
              <td><span class="code">{uid}</span> {name}</td>
              <td class="text-muted text-sm">{c.get("mod","-")}</td>
              <td>{c.get("reason","-")}</td>
              <td class="text-muted text-sm">{c.get("duration","-")}</td>
              <td class="text-muted text-sm">{c.get("ts","")[:10] if c.get("ts") else "-"}</td>
            </tr>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">⚠️ Варны & Кейсы</h1>

    <div class="tabs">
      <button class="tab active" data-group="wc" data-target="tab-warns">Варны ({sum(len(v) for v in warns_raw.values())})</button>
      <button class="tab" data-group="wc" data-target="tab-cases">Кейсы ({sum(len(v) for v in cases_raw.values())})</button>
      <button class="tab" data-group="wc" data-target="tab-search">Поиск по участнику</button>
    </div>

    <div id="tab-warns" class="tab-content active" data-group="wc">
      <div class="card">
        <div class="section-header">
          <div class="card-title">Список варнов</div>
          <button class="btn btn-danger btn-sm" onclick="clearAllWarns()">🗑️ Очистить все</button>
        </div>
        <div style="overflow-x:auto">
          <table>
            <thead><tr><th>User ID</th><th>Участник</th><th>Причина</th><th>Модератор</th><th>Дата</th><th></th></tr></thead>
            <tbody>{warn_rows or '<tr><td colspan="6" class="text-muted" style="text-align:center;padding:20px">Варнов нет</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div id="tab-cases" class="tab-content" data-group="wc">
      <div class="card">
        <div class="card-title">История кейсов</div>
        <div style="overflow-x:auto">
          <table>
            <thead><tr><th>#</th><th>Тип</th><th>Участник</th><th>Модератор</th><th>Причина</th><th>Длит.</th><th>Дата</th></tr></thead>
            <tbody>{case_rows or '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">Кейсов нет</td></tr>'}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div id="tab-search" class="tab-content" data-group="wc">
      <div class="card">
        <div class="card-title">🔍 Поиск</div>
        <div class="flex">
          <input id="search-uid" placeholder="User ID или упоминание" style="flex:1">
          <button class="btn btn-primary" onclick="searchUser()">Найти</button>
        </div>
        <div id="search-result" class="mt-12"></div>
      </div>
    </div>
  </div>
</div>

<script>
async function delWarn(uid, idx){{
  if(!await confirmAction('Удалить этот варн?')) return;
  try{{
    await doDelete(`/api/warnings/${{uid}}/${{idx}}`);
    toast('Варн удалён','ok');
    setTimeout(()=>location.reload(),600);
  }}catch(e){{toast(e.message,'error')}}
}}

async function clearAllWarns(){{
  if(!await confirmAction('Очистить ВСЕ варны на сервере?')) return;
  try{{
    await doDelete('/api/warnings');
    toast('Все варны удалены','ok');
    setTimeout(()=>location.reload(),600);
  }}catch(e){{toast(e.message,'error')}}
}}

async function searchUser(){{
  const uid=document.getElementById('search-uid').value.replace(/[^0-9]/g,'');
  if(!uid){{toast('Введите User ID','error');return;}}
  try{{
    const d=await doGet(`/api/user/${{uid}}`);
    const box=document.getElementById('search-result');
    box.innerHTML=`
      <div class="card-grid">
        <div class="stat"><div class="stat-label">Варны</div>
          <div class="stat-value" style="color:var(--warn)">${{d.warns}}</div></div>
        <div class="stat"><div class="stat-label">Кейсы</div>
          <div class="stat-value">${{d.cases}}</div></div>
      </div>
      <div style="margin-top:12px">
        ${{d.warn_list.map((w,i)=>`<div class="log-entry">⚠️ ${{w.reason||'-'}} — ${{w.mod||'-'}} (${{(w.ts||'').slice(0,10)}})
          <button class="btn btn-danger btn-sm" onclick="delWarn('${{uid}}',${{i}})">✕</button></div>`).join('')}}
      </div>`;
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: SEND MESSAGE
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_send(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))

    ch_options = ""
    if guild:
        for ch in sorted(guild.text_channels, key=lambda c: c.position):
            cat = f"[{ch.category.name}] " if ch.category else ""
            ch_options += f'<option value="{ch.id}">{cat}#{ch.name}</option>'

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">📨 Отправить сообщение</h1>
    <div id="send-alert"></div>

    <div class="card">
      <div class="card-title">💬 Обычное сообщение</div>
      <div class="form-group">
        <label>Канал</label>
        <select id="send-channel">{ch_options}</select>
      </div>
      <div class="form-group">
        <label>Сообщение</label>
        <textarea id="send-content" placeholder="Текст сообщения..." style="min-height:120px"></textarea>
      </div>
      <div class="flex">
        <button class="btn btn-primary" onclick="sendMsg()">📤 Отправить</button>
        <button class="btn btn-ghost" onclick="document.getElementById('send-content').value=''">Очистить</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">📋 Embed сообщение</div>
      <div class="card-grid">
        <div class="form-group">
          <label>Заголовок</label>
          <input id="embed-title" placeholder="Заголовок embed">
        </div>
        <div class="form-group">
          <label>Цвет (hex)</label>
          <input id="embed-color" placeholder="#5865F2" value="#5865F2" type="color">
        </div>
      </div>
      <div class="form-group">
        <label>Описание</label>
        <textarea id="embed-desc" placeholder="Описание embed..."></textarea>
      </div>
      <div class="card-grid">
        <div class="form-group">
          <label>Footer</label>
          <input id="embed-footer" placeholder="Текст footer">
        </div>
        <div class="form-group">
          <label>Thumbnail URL</label>
          <input id="embed-thumbnail" placeholder="https://...">
        </div>
        <div class="form-group">
          <label>Image URL</label>
          <input id="embed-image" placeholder="https://...">
        </div>
        <div class="form-group">
          <label>Author</label>
          <input id="embed-author" placeholder="Имя автора">
        </div>
      </div>
      <div class="flex">
        <button class="btn btn-primary" onclick="sendEmbed()">📤 Отправить Embed</button>
        <button class="btn btn-ghost" onclick="previewEmbed()">👁️ Превью</button>
      </div>
      <div id="embed-preview" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <div class="card-title">📣 Announce (DM всем участникам)</div>
      <div class="alert alert-warn">⚠️ Это отправит DM всем участникам сервера. Используйте осторожно!</div>
      <div class="form-group">
        <label>Сообщение</label>
        <textarea id="dm-content" placeholder="Текст DM..."></textarea>
      </div>
      <div class="form-group">
        <label>Фильтр по роли (опционально)</label>
        <select id="dm-role">
          <option value="">— Все участники —</option>
          {''.join(f'<option value="{r.id}">{r.name}</option>' for r in sorted(guild.roles[1:], key=lambda r: -r.position)) if guild else ''}
        </select>
      </div>
      <button class="btn btn-danger" onclick="sendDMAll()">📣 Отправить DM</button>
    </div>
  </div>
</div>

<script>
async function sendMsg(){{
  const ch=document.getElementById('send-channel').value;
  const content=document.getElementById('send-content').value.trim();
  if(!content){{toast('Введите текст','error');return;}}
  try{{
    await doPost('/api/send',{{channel_id:ch,content}});
    toast('Сообщение отправлено!','ok');
    document.getElementById('send-content').value='';
  }}catch(e){{toast(e.message,'error')}}
}}

async function sendEmbed(){{
  const ch=document.getElementById('send-channel').value;
  const embed={{
    title:document.getElementById('embed-title').value||undefined,
    description:document.getElementById('embed-desc').value||undefined,
    color:parseInt(document.getElementById('embed-color').value.replace('#',''),16)||0x5865F2,
    footer:document.getElementById('embed-footer').value?{{text:document.getElementById('embed-footer').value}}:undefined,
    thumbnail:document.getElementById('embed-thumbnail').value?{{url:document.getElementById('embed-thumbnail').value}}:undefined,
    image:document.getElementById('embed-image').value?{{url:document.getElementById('embed-image').value}}:undefined,
    author:document.getElementById('embed-author').value?{{name:document.getElementById('embed-author').value}}:undefined,
  }};
  try{{
    await doPost('/api/send',{{channel_id:ch,embed}});
    toast('Embed отправлен!','ok');
  }}catch(e){{toast(e.message,'error')}}
}}

function previewEmbed(){{
  const title=document.getElementById('embed-title').value;
  const desc=document.getElementById('embed-desc').value;
  const color=document.getElementById('embed-color').value;
  const footer=document.getElementById('embed-footer').value;
  document.getElementById('embed-preview').innerHTML=`
    <div style="border-left:4px solid ${{color}};background:var(--surface2);
      padding:12px 16px;border-radius:0 6px 6px 0;max-width:500px">
      ${{title?`<div style="font-weight:700;margin-bottom:4px">${{title}}</div>`:''}}
      ${{desc?`<div style="font-size:14px;color:var(--text2)">${{desc.replace(/\\n/g,'<br>')}}</div>`:''}}
      ${{footer?`<div style="font-size:12px;color:var(--text2);margin-top:8px;border-top:1px solid var(--border);padding-top:8px">${{footer}}</div>`:''}}
    </div>`;
}}

async function sendDMAll(){{
  const content=document.getElementById('dm-content').value.trim();
  const role_id=document.getElementById('dm-role').value;
  if(!content){{toast('Введите текст','error');return;}}
  if(!await confirmAction(`Отправить DM${{role_id?' участникам с ролью':' ВСЕМ участникам'}}?`)) return;
  try{{
    const d=await doPost('/api/dm-all',{{content,role_id:role_id||null}});
    toast(`Отправлено: ${{d.sent}}, ошибок: ${{d.failed}}`,'ok');
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: CHANNELS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_channels(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))

    rows = ""
    if guild:
        for ch in sorted(guild.channels, key=lambda c: (str(type(c).__name__), c.position)):
            type_map = {"TextChannel": "text", "VoiceChannel": "voice", "CategoryChannel": "category",
                        "StageChannel": "stage", "ForumChannel": "forum", "NewsChannel": "news"}
            ch_type = type_map.get(type(ch).__name__, "?")
            members = getattr(ch, "member_count", None) or len(getattr(ch, "members", []))
            rows += f"""<tr>
              <td><span class="code">#{ch.name}</span></td>
              <td class="text-muted">{ch_type}</td>
              <td class="text-muted text-sm">{getattr(ch, 'category', None) and ch.category.name or '—'}</td>
              <td class="text-muted">{ch.id}</td>
              <td class="text-muted">{members}</td>
              <td>
                <button class="btn btn-ghost btn-sm" onclick="lockCh('{ch.id}','{ch.name}')">🔒</button>
                <button class="btn btn-ghost btn-sm" onclick="unlockCh('{ch.id}','{ch.name}')">🔓</button>
              </td>
            </tr>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">💬 Каналы</h1>
    <div class="card">
      <div class="section-header">
        <div class="card-title">Список каналов</div>
        <div class="flex">
          <input id="ch-search" placeholder="Поиск..." oninput="filterCh(this.value)" style="width:200px">
        </div>
      </div>
      <div style="overflow-x:auto">
        <table id="ch-table">
          <thead><tr><th>Название</th><th>Тип</th><th>Категория</th><th>ID</th><th>Участников</th><th>Действия</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
function filterCh(q){{
  q=q.toLowerCase();
  document.querySelectorAll('#ch-table tbody tr').forEach(r=>{{
    r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
async function lockCh(id,name){{
  if(!await confirmAction(`Заблокировать #${{name}}?`)) return;
  try{{await doPost('/api/channel/'+id+'/lock',{{}});toast('Канал заблокирован','ok');}}
  catch(e){{toast(e.message,'error')}}
}}
async function unlockCh(id,name){{
  if(!await confirmAction(`Разблокировать #${{name}}?`)) return;
  try{{await doPost('/api/channel/'+id+'/unlock',{{}});toast('Канал разблокирован','ok');}}
  catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: ROLES
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_roles(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))
    rows = ""
    if guild:
        for r in reversed(guild.roles[1:]):
            color_hex = f"#{r.color.value:06x}" if r.color.value else "#99aab5"
            perms = []
            if r.permissions.administrator: perms.append("Admin")
            if r.permissions.manage_guild: perms.append("Manage Server")
            if r.permissions.manage_messages: perms.append("Manage Messages")
            if r.permissions.ban_members: perms.append("Ban")
            if r.permissions.kick_members: perms.append("Kick")
            rows += f"""<tr>
              <td><span style="display:inline-block;width:12px;height:12px;
                border-radius:50%;background:{color_hex};margin-right:6px;vertical-align:middle"></span>
                {r.name}</td>
              <td class="text-muted">{r.id}</td>
              <td class="text-muted">{len(r.members)}</td>
              <td><span class="code">{color_hex}</span></td>
              <td class="text-sm">{', '.join(perms) if perms else '—'}</td>
              <td>
                {'<span class="badge badge-warn">Hoist</span>' if r.hoist else ''}
                {'<span class="badge badge-info">Mentionable</span>' if r.mentionable else ''}
              </td>
            </tr>"""
    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">👥 Роли</h1>
    <div class="card">
      <div class="section-header">
        <div class="card-title">Список ролей ({len(guild.roles)-1 if guild else 0})</div>
        <input id="role-search" placeholder="Поиск..." oninput="filterRoles(this.value)" style="width:200px">
      </div>
      <div style="overflow-x:auto">
        <table id="roles-table">
          <thead><tr><th>Название</th><th>ID</th><th>Участников</th><th>Цвет</th><th>Права</th><th>Флаги</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
function filterRoles(q){{
  q=q.toLowerCase();
  document.querySelectorAll('#roles-table tbody tr').forEach(r=>{{
    r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: MEMBERS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_members(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))
    _store, _, _ = _get_store()
    guild_id_str = sess.get("guild_id", "")
    warns_data = _store.get("warnings.json", {}).get(guild_id_str, {}) if _store else {}
    cases_data = _store.get("cases.json", {}).get(guild_id_str, {}) if _store else {}

    rows = ""
    if guild:
        members = sorted(guild.members, key=lambda m: m.joined_at or datetime.min.replace(tzinfo=timezone.utc))[:200]
        for m in members:
            uid = str(m.id)
            w_count = len(warns_data.get(uid, []))
            c_count = len(cases_data.get(uid, []))
            top_role = m.top_role.name if len(m.roles) > 1 else "—"
            joined = m.joined_at.strftime("%d.%m.%Y") if m.joined_at else "—"
            w_badge = f'<span class="badge badge-warn">{w_count} варн</span>' if w_count else ""
            rows += f"""<tr>
              <td>
                {'<img src="'+str(m.display_avatar.url)+'" class="avatar" style="vertical-align:middle;margin-right:6px">' if m.display_avatar else ''}
                {m.display_name}
                {'<span class="badge badge-info" style="margin-left:4px">BOT</span>' if m.bot else ''}
              </td>
              <td class="text-muted"><span class="code">{m.id}</span></td>
              <td class="text-muted text-sm">{top_role}</td>
              <td class="text-muted text-sm">{joined}</td>
              <td>{w_badge} {f'<span class="badge badge-error">{c_count} кейс</span>' if c_count else ''}</td>
              <td>
                <button class="btn btn-ghost btn-sm" onclick="viewMember('{m.id}')">👁️</button>
                <button class="btn btn-danger btn-sm" onclick="kickMember('{m.id}','{m.display_name}')">kick</button>
              </td>
            </tr>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">👥 Участники</h1>
    <div class="card">
      <div class="section-header">
        <div class="card-title">Участники (первые 200)</div>
        <input id="m-search" placeholder="Поиск по имени/ID..." oninput="filterMembers(this.value)" style="width:220px">
      </div>
      <div style="overflow-x:auto">
        <table id="m-table">
          <thead><tr><th>Участник</th><th>ID</th><th>Топ роль</th><th>Зашёл</th><th>История</th><th></th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
function filterMembers(q){{
  q=q.toLowerCase();
  document.querySelectorAll('#m-table tbody tr').forEach(r=>{{
    r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}
async function kickMember(uid,name){{
  const reason=prompt(`Причина кика ${name}:`,'Kicked via dashboard');
  if(!reason) return;
  try{{
    await doPost(`/api/member/${{uid}}/kick`,{{reason}});
    toast(`${name} кикнут`,'ok');
    setTimeout(()=>location.reload(),600);
  }}catch(e){{toast(e.message,'error')}}
}}
async function viewMember(uid){{
  try{{
    const d=await doGet(`/api/user/${{uid}}`);
    alert(JSON.stringify(d,null,2));
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: TICKETS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_tickets(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    guild_id_str = sess.get("guild_id", "")
    tickets = _store.get("tickets.json", {}).get(guild_id_str, {}) if _store else {}
    guild = _get_guild(guild_id_str)

    rows = ""
    for tid, tdata in tickets.items():
        status = tdata.get("status", "open")
        badge_type = "ok" if status == "open" else "error"
        priority = tdata.get("priority", "normal")
        pcolor = {"low":"info","normal":"ok","high":"warn","urgent":"error"}.get(priority,"info")
        channel_id = tdata.get("channel_id","")
        ch = guild.get_channel(int(channel_id)) if guild and channel_id else None
        ch_name = f"#{ch.name}" if ch else f"<{channel_id}>"
        rows += f"""<tr>
          <td class="code">{tid}</td>
          <td>{ch_name}</td>
          <td><span class="badge badge-{badge_type}">{status}</span></td>
          <td><span class="badge badge-{pcolor}">{priority}</span></td>
          <td class="text-muted text-sm">{tdata.get("author_id","—")}</td>
          <td class="text-muted text-sm">{tdata.get("claimed_by","—")}</td>
          <td class="text-muted text-sm">{(tdata.get("created",""))[:10]}</td>
          <td>
            <button class="btn btn-danger btn-sm" onclick="closeTicket('{tid}')">Закрыть</button>
          </td>
        </tr>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">🎫 Тикеты</h1>
    <div class="card">
      <div class="section-header">
        <div class="card-title">Активные тикеты ({len(tickets)})</div>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>ID</th><th>Канал</th><th>Статус</th><th>Приоритет</th><th>Автор</th><th>Куратор</th><th>Дата</th><th></th></tr></thead>
          <tbody>{rows or '<tr><td colspan="8" class="text-muted" style="text-align:center;padding:20px">Тикетов нет</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
async function closeTicket(tid){{
  if(!await confirmAction(`Закрыть тикет ${{tid}}?`)) return;
  try{{
    await doPost(`/api/ticket/${{tid}}/close`,{{}});
    toast('Тикет закрыт','ok');
    setTimeout(()=>location.reload(),600);
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: GIVEAWAYS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_giveaways(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    guild_id_str = sess.get("guild_id", "")
    giveaways = _store.get("giveaways.json", {}).get(guild_id_str, {}) if _store else {}
    guild = _get_guild(guild_id_str)

    rows = ""
    for gid, gdata in giveaways.items():
        ended = gdata.get("ended", False)
        badge = '<span class="badge badge-ok">Активен</span>' if not ended else '<span class="badge badge-error">Завершён</span>'
        entries = len(gdata.get("entries", []))
        rows += f"""<tr>
          <td class="code">{gid}</td>
          <td>{gdata.get("prize","—")}</td>
          <td class="text-muted">{gdata.get("winners_count",1)} победитель(ей)</td>
          <td>{entries} участников</td>
          <td class="text-muted text-sm">{(gdata.get("end_time",""))[:10]}</td>
          <td>{badge}</td>
          <td>
            {'<button class="btn btn-primary btn-sm" onclick="endGiveaway(\''+gid+'\')">Завершить</button>' if not ended else
             '<button class="btn btn-ghost btn-sm" onclick="rerollGiveaway(\''+gid+'\')">Reroll</button>'}
          </td>
        </tr>"""

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">🎉 Розыгрыши</h1>
    <div class="card">
      <div class="card-title">Список розыгрышей</div>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>ID</th><th>Приз</th><th>Победители</th><th>Участники</th><th>До</th><th>Статус</th><th></th></tr></thead>
          <tbody>{rows or '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">Нет розыгрышей</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
async function endGiveaway(gid){{
  if(!await confirmAction('Завершить розыгрыш досрочно?')) return;
  try{{
    await doPost(`/api/giveaway/${{gid}}/end`,{{}});
    toast('Розыгрыш завершён','ok');
    setTimeout(()=>location.reload(),600);
  }}catch(e){{toast(e.message,'error')}}
}}
async function rerollGiveaway(gid){{
  try{{
    await doPost(`/api/giveaway/${{gid}}/reroll`,{{}});
    toast('Reroll выполнен','ok');
  }}catch(e){{toast(e.message,'error')}}
}}
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: STATS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_stats(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))
    gi = _guild_info(guild) if guild else {}

    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">📊 Статистика</h1>

    <div class="card-grid" style="margin-bottom:20px">
      <div class="stat"><div class="stat-label">Участников</div>
        <div class="stat-value">{gi.get("member_count","—")}</div></div>
      <div class="stat"><div class="stat-label">Каналов</div>
        <div class="stat-value">{gi.get("channel_count","—")}</div></div>
      <div class="stat"><div class="stat-label">Ролей</div>
        <div class="stat-value">{gi.get("role_count","—")}</div></div>
      <div class="stat"><div class="stat-label">Boost Level</div>
        <div class="stat-value">{gi.get("boost_level","—")}</div></div>
    </div>

    <div class="card">
      <div class="card-title">📈 Live метрики бота</div>
      <div id="metrics-full" class="card-grid"></div>
    </div>

    <div class="card">
      <div class="card-title">🗄️ База данных</div>
      <div id="db-stats"></div>
    </div>
  </div>
</div>
<script>
async function loadStats(){{
  try{{
    const m=await doGet('/api/metrics');
    document.getElementById('metrics-full').innerHTML=`
      <div class="stat"><div class="stat-label">Latency</div><div class="stat-value">${{m.latency_ms}}ms</div></div>
      <div class="stat"><div class="stat-label">Guilds</div><div class="stat-value">${{m.guilds}}</div></div>
      <div class="stat"><div class="stat-label">Uptime</div><div class="stat-value" style="font-size:16px">${{m.uptime}}</div></div>
      <div class="stat"><div class="stat-label">Commands</div><div class="stat-value">${{m.commands}}</div></div>
      <div class="stat"><div class="stat-label">Storage</div><div class="stat-value" style="font-size:16px">${{m.storage}}</div></div>
    `;
    const d=await doGet('/api/db-stats');
    document.getElementById('db-stats').innerHTML='<div class="card-grid">'+
      Object.entries(d.collections).map(([k,v])=>
        `<div class="stat"><div class="stat-label">${{k}}</div><div class="stat-value">${{v}}</div><div class="stat-sub">записей</div></div>`
      ).join('')+'</div>';
  }}catch(e){{}}
}}
loadStats();
setInterval(loadStats,5000);
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: LOGS (live)
# ═══════════════════════════════════════════════════════════════════════════

_log_buffer: list[dict] = []
_MAX_LOGS = 500


class DashboardLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append({
            "ts":  datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": self.format(record),
        })
        if len(_log_buffer) > _MAX_LOGS:
            _log_buffer.pop(0)


@_require_session
async def handle_logs(request: web.Request) -> web.Response:
    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">📋 Логи бота</h1>
    <div class="card">
      <div class="section-header">
        <div class="card-title">Системные логи <div class="spinner" id="log-spinner" style="margin-left:8px"></div></div>
        <div class="flex">
          <select id="log-filter" onchange="filterLogs()">
            <option value="">Все уровни</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
            <option value="DEBUG">DEBUG</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('log-box').innerHTML=''">Очистить</button>
        </div>
      </div>
      <div id="log-box"></div>
    </div>
  </div>
</div>
<script>
let lastCount=0;
const levelColors={{INFO:'log-info',WARNING:'log-warn',ERROR:'log-error',DEBUG:'log-ok'}};

async function pollLogs(){{
  try{{
    const d=await doGet(`/api/logs?since=${{lastCount}}`);
    if(d.entries?.length){{
      const box=document.getElementById('log-box');
      const filter=document.getElementById('log-filter').value;
      d.entries.forEach(e=>{{
        if(filter&&e.level!==filter) return;
        const div=document.createElement('div');
        div.className=`log-entry ${{levelColors[e.level]||''}}`;
        div.textContent=`[${{e.ts}}] [${{e.level}}] ${{e.msg}}`;
        box.appendChild(div);
      }});
      lastCount=d.total;
      box.scrollTop=box.scrollHeight;
      document.getElementById('log-spinner').style.display='none';
    }}
  }}catch(e){{}}
}}
function filterLogs(){{ lastCount=0; document.getElementById('log-box').innerHTML=''; pollLogs(); }}
pollLogs();
setInterval(pollLogs,2000);
</script>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  PAGE: API DOCS
# ═══════════════════════════════════════════════════════════════════════════

@_require_session
async def handle_api_docs(request: web.Request) -> web.Response:
    endpoints = [
        ("GET",  "/ping",              "Проверка работоспособности (без авторизации)"),
        ("GET",  "/api/metrics",       "Живые метрики бота"),
        ("GET",  "/api/db-stats",      "Статистика хранилища"),
        ("GET",  "/api/config",        "Получить конфиг сервера"),
        ("POST", "/api/config",        "Сохранить конфиг сервера (JSON body)"),
        ("GET",  "/api/automod",       "Получить настройки авто-мода"),
        ("PATCH","/api/automod",       "Обновить настройки авто-мода"),
        ("GET",  "/api/warnings",      "Все варны сервера"),
        ("DELETE","/api/warnings",     "Удалить все варны сервера"),
        ("DELETE","/api/warnings/{uid}/{idx}", "Удалить конкретный варн"),
        ("GET",  "/api/cases",         "Все кейсы сервера"),
        ("GET",  "/api/user/{uid}",    "Информация о пользователе (варны, кейсы)"),
        ("POST", "/api/member/{uid}/kick", "Кикнуть участника"),
        ("POST", "/api/member/{uid}/ban",  "Забанить участника"),
        ("POST", "/api/send",          "Отправить сообщение в канал"),
        ("POST", "/api/dm-all",        "DM всем участникам (с фильтром по роли)"),
        ("POST", "/api/channel/{id}/lock",   "Заблокировать канал"),
        ("POST", "/api/channel/{id}/unlock", "Разблокировать канал"),
        ("GET",  "/api/logs",          "Последние логи бота"),
        ("POST", "/api/bot/reload",    "Перезагрузить данные"),
        ("POST", "/api/giveaway/{id}/end",    "Завершить розыгрыш"),
        ("POST", "/api/giveaway/{id}/reroll", "Reroll розыгрыша"),
        ("POST", "/api/ticket/{id}/close",    "Закрыть тикет"),
        ("POST", "/webhook/github",    "GitHub webhook (signature required)"),
    ]
    rows = "".join(
        f'<tr><td><span class="badge badge-{"ok" if m=="GET" else "warn" if m in ("PATCH","PUT") else "error" if m=="DELETE" else "info"}">{m}</span></td>'
        f'<td class="code">{p}</td><td class="text-muted text-sm">{d}</td></tr>'
        for m, p, d in endpoints
    )
    body = f"""
<div class="layout">
  {_SIDEBAR}
  <div class="main">
    <h1 style="font-size:22px;font-weight:700;margin-bottom:20px">📖 API Documentation</h1>
    <div class="alert alert-info">Все API-запросы требуют заголовок <span class="code">X-Haven-Token: &lt;session_token&gt;</span> или cookie <span class="code">haven_session</span>.</div>
    <div class="card">
      <table>
        <thead><tr><th>Метод</th><th>Путь</th><th>Описание</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
</div>"""
    return web.Response(text=_HTML_BASE.replace("{BODY}", body), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════
#  REST API
# ═══════════════════════════════════════════════════════════════════════════

def json_ok(data: Any = None) -> web.Response:
    return web.Response(
        text=json.dumps(data or {"ok": True}, ensure_ascii=False),
        content_type="application/json",
    )


def json_err(msg: str, status: int = 400) -> web.Response:
    return web.Response(
        text=json.dumps({"error": msg}),
        content_type="application/json",
        status=status,
    )


# ── /ping ──────────────────────────────────────────────────────────────────
async def api_ping(request: web.Request) -> web.Response:
    return json_ok({
        "status": "ok",
        "bot":    _bot.user.name if _bot and _bot.user else "not connected",
        "uptime": _bot_uptime(),
        "latency_ms": round(_bot.latency * 1000, 1) if _bot else -1,
        "ts":     datetime.now(timezone.utc).isoformat(),
    })


# ── /api/metrics ──────────────────────────────────────────────────────────
@_require_session
async def api_metrics(request: web.Request) -> web.Response:
    return json_ok({
        "latency_ms": round(_bot.latency * 1000, 1) if _bot else -1,
        "guilds":     len(_bot.guilds) if _bot else 0,
        "commands":   len(list(_bot.all_slash_commands)) if _bot else 0,
        "uptime":     _bot_uptime(),
        "storage":    "MongoDB" if (hasattr(_bot, "_mongo_ok") and _bot._mongo_ok) else "JSON",
    })


# ── /api/db-stats ─────────────────────────────────────────────────────────
@_require_session
async def api_db_stats(request: web.Request) -> web.Response:
    _store, _, _ = _get_store()
    cols = {}
    if _store:
        for k, v in _store.items():
            name = k.replace(".json", "")
            if isinstance(v, dict):
                cols[name] = sum(len(vv) if isinstance(vv, (list, dict)) else 1 for vv in v.values())
            else:
                cols[name] = len(v) if v else 0
    return json_ok({"collections": cols})


# ── /api/config ──────────────────────────────────────────────────────────
@_require_session
async def api_config_get(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    cfg = _store.get("config.json", {}).get(sess.get("guild_id", ""), {}) if _store else {}
    return json_ok(cfg)


@_require_session
async def api_config_post(request: web.Request) -> web.Response:
    sess = request["session"]
    guild_id_str = sess.get("guild_id", "")
    try:
        payload = await request.json()
    except Exception:
        return json_err("Invalid JSON")
    _store, _save_fn, _ = _get_store()
    if _save_fn is None:
        return json_err("Storage not available")
    cfg = _store.get("config.json", {})
    gcfg = cfg.setdefault(guild_id_str, {})
    for k, v in payload.items():
        if v is None:
            gcfg.pop(k, None)
        else:
            gcfg[k] = v
    _save_fn("config.json", cfg)
    log.info(f"[Dashboard] Config updated by {sess['username']}: {list(payload.keys())}")
    return json_ok()


# ── /api/automod ──────────────────────────────────────────────────────────
@_require_session
async def api_automod_get(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    cfg = _store.get("config.json", {}).get(sess.get("guild_id", ""), {}) if _store else {}
    return json_ok(cfg.get("automod", {}))


@_require_session
async def api_automod_patch(request: web.Request) -> web.Response:
    sess = request["session"]
    guild_id_str = sess.get("guild_id", "")
    try:
        payload = await request.json()
    except Exception:
        return json_err("Invalid JSON")
    _store, _save_fn, _ = _get_store()
    if _save_fn is None:
        return json_err("Storage not available")
    cfg = _store.get("config.json", {})
    gcfg = cfg.setdefault(guild_id_str, {})
    automod = gcfg.get("automod", {})
    automod.update(payload)
    gcfg["automod"] = automod
    _save_fn("config.json", cfg)
    log.info(f"[Dashboard] Automod updated by {sess['username']}")
    return json_ok()


# ── /api/warnings ─────────────────────────────────────────────────────────
@_require_session
async def api_warnings_get(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    warns = _store.get("warnings.json", {}).get(sess.get("guild_id", ""), {}) if _store else {}
    return json_ok(warns)


@_require_session
async def api_warnings_clear(request: web.Request) -> web.Response:
    sess = request["session"]
    guild_id_str = sess.get("guild_id", "")
    _store, _save_fn, _ = _get_store()
    if _save_fn is None:
        return json_err("Storage not available")
    warns = _store.get("warnings.json", {})
    warns[guild_id_str] = {}
    _save_fn("warnings.json", warns)
    log.warning(f"[Dashboard] ALL warnings cleared by {sess['username']}")
    return json_ok()


@_require_session
async def api_warning_delete(request: web.Request) -> web.Response:
    sess = request["session"]
    uid = request.match_info["uid"]
    try:
        idx = int(request.match_info["idx"])
    except ValueError:
        return json_err("Invalid index")
    guild_id_str = sess.get("guild_id", "")
    _store, _save_fn, _ = _get_store()
    if _save_fn is None:
        return json_err("Storage not available")
    warns = _store.get("warnings.json", {})
    gwarn = warns.get(guild_id_str, {}).get(uid, [])
    if idx < 0 or idx >= len(gwarn):
        return json_err("Index out of range")
    gwarn.pop(idx)
    if not gwarn:
        warns.get(guild_id_str, {}).pop(uid, None)
    _save_fn("warnings.json", warns)
    return json_ok()


# ── /api/cases ─────────────────────────────────────────────────────────────
@_require_session
async def api_cases_get(request: web.Request) -> web.Response:
    sess = request["session"]
    _store, _, _ = _get_store()
    cases = _store.get("cases.json", {}).get(sess.get("guild_id", ""), {}) if _store else {}
    return json_ok(cases)


# ── /api/user/{uid} ──────────────────────────────────────────────────────
@_require_session
async def api_user(request: web.Request) -> web.Response:
    sess = request["session"]
    uid = request.match_info["uid"]
    guild_id_str = sess.get("guild_id", "")
    _store, _, _ = _get_store()
    warns = _store.get("warnings.json", {}).get(guild_id_str, {}).get(uid, []) if _store else []
    cases = _store.get("cases.json", {}).get(guild_id_str, {}).get(uid, []) if _store else []
    notes = _store.get("notes.json", {}).get(guild_id_str, {}).get(uid, []) if _store else []
    guild = _get_guild(guild_id_str)
    member_info = {}
    if guild:
        try:
            m = guild.get_member(int(uid))
            if m:
                member_info = {
                    "name": str(m),
                    "display_name": m.display_name,
                    "avatar": str(m.display_avatar.url),
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                    "roles": [r.name for r in m.roles[1:]],
                    "bot": m.bot,
                }
        except Exception:
            pass
    return json_ok({
        "uid": uid,
        "member": member_info,
        "warns": len(warns),
        "cases": len(cases),
        "notes": len(notes),
        "warn_list": warns,
        "case_list": cases,
        "note_list": notes,
    })


# ── /api/member/{uid}/kick ───────────────────────────────────────────────
@_require_session
async def api_member_kick(request: web.Request) -> web.Response:
    sess = request["session"]
    uid = request.match_info["uid"]
    guild = _get_guild(sess.get("guild_id", ""))
    if guild is None:
        return json_err("Guild not found")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reason = payload.get("reason", f"Kicked via Haven Dashboard by {sess['username']}")
    try:
        member = guild.get_member(int(uid))
        if member is None:
            return json_err("Member not found")
        await member.kick(reason=reason)
        log.warning(f"[Dashboard] Kicked {member} by {sess['username']}: {reason}")
        return json_ok()
    except Exception as e:
        return json_err(str(e))


# ── /api/member/{uid}/ban ────────────────────────────────────────────────
@_require_session
async def api_member_ban(request: web.Request) -> web.Response:
    sess = request["session"]
    uid = request.match_info["uid"]
    guild = _get_guild(sess.get("guild_id", ""))
    if guild is None:
        return json_err("Guild not found")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reason = payload.get("reason", f"Banned via Haven Dashboard by {sess['username']}")
    delete_days = int(payload.get("delete_days", 0))
    try:
        await guild.ban(disnake.Object(id=int(uid)), reason=reason, delete_message_days=delete_days)
        log.warning(f"[Dashboard] Banned {uid} by {sess['username']}: {reason}")
        return json_ok()
    except Exception as e:
        return json_err(str(e))


# ── /api/send ────────────────────────────────────────────────────────────
@_require_session
async def api_send(request: web.Request) -> web.Response:
    sess = request["session"]
    try:
        payload = await request.json()
    except Exception:
        return json_err("Invalid JSON")
    channel_id = payload.get("channel_id")
    content = payload.get("content")
    embed_data = payload.get("embed")
    if not channel_id:
        return json_err("channel_id required")
    if not _bot:
        return json_err("Bot not connected")
    try:
        channel = _bot.get_channel(int(channel_id))
        if channel is None:
            return json_err("Channel not found")
        kwargs = {}
        if content:
            kwargs["content"] = content
        if embed_data:
            import disnake as _d
            e = _d.Embed(
                title=embed_data.get("title"),
                description=embed_data.get("description"),
                color=embed_data.get("color", 0x5865F2),
            )
            if embed_data.get("footer"):
                e.set_footer(text=embed_data["footer"]["text"])
            if embed_data.get("thumbnail"):
                e.set_thumbnail(url=embed_data["thumbnail"]["url"])
            if embed_data.get("image"):
                e.set_image(url=embed_data["image"]["url"])
            if embed_data.get("author"):
                e.set_author(name=embed_data["author"]["name"])
            kwargs["embed"] = e
        if not kwargs:
            return json_err("content or embed required")
        msg = await channel.send(**kwargs)
        log.info(f"[Dashboard] Message sent to #{channel.name} by {sess['username']}")
        return json_ok({"message_id": str(msg.id)})
    except Exception as e:
        return json_err(str(e))


# ── /api/dm-all ──────────────────────────────────────────────────────────
@_require_session
async def api_dm_all(request: web.Request) -> web.Response:
    sess = request["session"]
    guild = _get_guild(sess.get("guild_id", ""))
    if guild is None:
        return json_err("Guild not found")
    try:
        payload = await request.json()
    except Exception:
        return json_err("Invalid JSON")
    content = payload.get("content", "")
    role_id = payload.get("role_id")
    if not content:
        return json_err("content required")
    members = guild.members
    if role_id:
        try:
            role = guild.get_role(int(role_id))
            if role:
                members = [m for m in members if role in m.roles]
        except Exception:
            pass
    members = [m for m in members if not m.bot]
    sent = failed = 0
    for m in members:
        try:
            await m.send(content)
            sent += 1
        except Exception:
            failed += 1
    log.info(f"[Dashboard] DM-All by {sess['username']}: sent={sent}, failed={failed}")
    return json_ok({"sent": sent, "failed": failed})


# ── /api/channel/{id}/lock & unlock ──────────────────────────────────────
@_require_session
async def api_channel_lock(request: web.Request) -> web.Response:
    sess = request["session"]
    channel_id = request.match_info["id"]
    if not _bot:
        return json_err("Bot not connected")
    try:
        import disnake as _d
        channel = _bot.get_channel(int(channel_id))
        if channel is None:
            return json_err("Channel not found")
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite,
                                       reason=f"Locked via dashboard by {sess['username']}")
        return json_ok()
    except Exception as e:
        return json_err(str(e))


@_require_session
async def api_channel_unlock(request: web.Request) -> web.Response:
    sess = request["session"]
    channel_id = request.match_info["id"]
    if not _bot:
        return json_err("Bot not connected")
    try:
        channel = _bot.get_channel(int(channel_id))
        if channel is None:
            return json_err("Channel not found")
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite,
                                       reason=f"Unlocked via dashboard by {sess['username']}")
        return json_ok()
    except Exception as e:
        return json_err(str(e))


# ── /api/logs ─────────────────────────────────────────────────────────────
@_require_session
async def api_logs(request: web.Request) -> web.Response:
    since = int(request.query.get("since", 0))
    entries = _log_buffer[since:]
    return json_ok({"entries": entries, "total": len(_log_buffer)})


# ── /api/bot/reload ───────────────────────────────────────────────────────
@_require_session
async def api_bot_reload(request: web.Request) -> web.Response:
    try:
        import main as m
        await m._mongo_load_all()
        log.info(f"[Dashboard] Data reloaded by {request['session']['username']}")
        return json_ok()
    except Exception as e:
        return json_err(str(e))


# ── /api/giveaway/{id}/end & reroll ──────────────────────────────────────
@_require_session
async def api_giveaway_end(request: web.Request) -> web.Response:
    sess = request["session"]
    gid = request.match_info["id"]
    guild_id_str = sess.get("guild_id", "")
    try:
        import main as m
        guild = _get_guild(guild_id_str)
        if guild is None:
            return json_err("Guild not found")
        await m._end_giveaway(guild, gid)
        return json_ok()
    except Exception as e:
        return json_err(str(e))


@_require_session
async def api_giveaway_reroll(request: web.Request) -> web.Response:
    sess = request["session"]
    gid = request.match_info["id"]
    guild_id_str = sess.get("guild_id", "")
    _store, _save_fn, _ = _get_store()
    if _store is None:
        return json_err("Storage not available")
    import random
    giveaways = _store.get("giveaways.json", {})
    gdata = giveaways.get(guild_id_str, {}).get(gid)
    if not gdata:
        return json_err("Giveaway not found")
    entries = gdata.get("entries", [])
    if not entries:
        return json_err("No entries")
    winners_count = min(gdata.get("winners_count", 1), len(entries))
    winners = random.sample(entries, winners_count)
    return json_ok({"winners": winners})


# ── /api/ticket/{id}/close ────────────────────────────────────────────────
@_require_session
async def api_ticket_close(request: web.Request) -> web.Response:
    sess = request["session"]
    tid = request.match_info["id"]
    guild_id_str = sess.get("guild_id", "")
    _store, _save_fn, _ = _get_store()
    if _save_fn is None:
        return json_err("Storage not available")
    tickets = _store.get("tickets.json", {})
    gtix = tickets.get(guild_id_str, {})
    if tid not in gtix:
        return json_err("Ticket not found")
    gtix[tid]["status"] = "closed"
    _save_fn("tickets.json", tickets)
    log.info(f"[Dashboard] Ticket {tid} closed by {sess['username']}")
    return json_ok()


# ── /webhook/github ──────────────────────────────────────────────────────
async def handle_github_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    if WH_SECRET:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(WH_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return json_err("Invalid signature", 403)
    try:
        data = json.loads(body)
    except Exception:
        return json_err("Invalid JSON")
    event = request.headers.get("X-GitHub-Event", "?")
    log.info(f"[Webhook] GitHub event: {event} — {data.get('repository', {}).get('full_name', '?')}")
    return json_ok({"received": event})


# ── /logout ──────────────────────────────────────────────────────────────
async def handle_logout(request: web.Request) -> web.Response:
    token = request.cookies.get("haven_session")
    _sessions.pop(token, None)
    resp = web.HTTPFound("/login")
    resp.del_cookie("haven_session")
    raise resp


# ═══════════════════════════════════════════════════════════════════════════
#  BUILD APP
# ═══════════════════════════════════════════════════════════════════════════

def _build_app() -> web.Application:
    app = web.Application()

    # Install log handler
    root_log = logging.getLogger()
    handler = DashboardLogHandler()
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    root_log.addHandler(handler)

    app.router.add_get("/ping",         api_ping)
    app.router.add_get("/",             handle_home)
    app.router.add_get("/login",        handle_login)
    app.router.add_post("/login",       handle_login)
    app.router.add_get("/logout",       handle_logout)
    app.router.add_get("/config",       handle_config)
    app.router.add_get("/automod",      handle_automod)
    app.router.add_get("/moderation",   handle_warnings)
    app.router.add_get("/warnings",     handle_warnings)
    app.router.add_get("/send",         handle_send)
    app.router.add_get("/channels",     handle_channels)
    app.router.add_get("/roles",        handle_roles)
    app.router.add_get("/members",      handle_members)
    app.router.add_get("/tickets",      handle_tickets)
    app.router.add_get("/giveaways",    handle_giveaways)
    app.router.add_get("/stats",        handle_stats)
    app.router.add_get("/logs",         handle_logs)
    app.router.add_get("/api-docs",     handle_api_docs)

    # REST API
    app.router.add_get("/api/metrics",              api_metrics)
    app.router.add_get("/api/db-stats",             api_db_stats)
    app.router.add_get("/api/config",               api_config_get)
    app.router.add_post("/api/config",              api_config_post)
    app.router.add_get("/api/automod",              api_automod_get)
    app.router.add_patch("/api/automod",            api_automod_patch)
    app.router.add_get("/api/warnings",             api_warnings_get)
    app.router.add_delete("/api/warnings",          api_warnings_clear)
    app.router.add_delete("/api/warnings/{uid}/{idx}", api_warning_delete)
    app.router.add_get("/api/cases",                api_cases_get)
    app.router.add_get("/api/user/{uid}",           api_user)
    app.router.add_post("/api/member/{uid}/kick",   api_member_kick)
    app.router.add_post("/api/member/{uid}/ban",    api_member_ban)
    app.router.add_post("/api/send",                api_send)
    app.router.add_post("/api/dm-all",              api_dm_all)
    app.router.add_post("/api/channel/{id}/lock",   api_channel_lock)
    app.router.add_post("/api/channel/{id}/unlock", api_channel_unlock)
    app.router.add_get("/api/logs",                 api_logs)
    app.router.add_post("/api/bot/reload",          api_bot_reload)
    app.router.add_post("/api/giveaway/{id}/end",    api_giveaway_end)
    app.router.add_post("/api/giveaway/{id}/reroll", api_giveaway_reroll)
    app.router.add_post("/api/ticket/{id}/close",   api_ticket_close)
    app.router.add_post("/webhook/github",          handle_github_webhook)

    return app


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

_runner: web.AppRunner | None = None


async def _run_server() -> None:
    global _runner
    app = _build_app()
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    site = web.TCPSite(_runner, DASHBOARD_HOST, DASHBOARD_PORT)
    await site.start()
    log.info(f"Haven Dashboard started → http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    log.info(f"Keep-alive ping       → http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/ping")


async def stop() -> None:
    if _runner:
        await _runner.cleanup()


def start(bot_instance: Any) -> None:
    """
    Вызвать из main() до bot.run().
    Запускает сервер в фоне через asyncio.create_task().

    Пример использования в main.py:

        import keep_alive
        ...
        def main() -> None:
            import asyncio

            async def _run():
                keep_alive.start(bot)
                await asyncio.sleep(0.5)   # дать серверу стартовать
                await bot._async_setup_hook()  # если нужно

            token = os.environ.get("HAVEN_TOKEN")
            bot.loop.create_task(_run_server_task())
            bot.run(token)

    Или напрямую:

        import keep_alive
        keep_alive.register(bot)   # регистрирует бота без запуска

    Смотри setup() ниже для intеграции через on_ready.
    """
    global _bot
    _bot = bot_instance
    loop = asyncio.get_event_loop()
    loop.create_task(_run_server())


def register(bot_instance: Any) -> None:
    """Только регистрирует бота, не запускает сервер."""
    global _bot
    _bot = bot_instance


async def start_async(bot_instance: Any) -> None:
    """Async версия — await из корутины."""
    global _bot
    _bot = bot_instance
    await _run_server()


def setup_auto_start(bot_instance: Any) -> None:
    """
    Наиболее надёжный способ интеграции:
    Добавляет слушатель on_ready который стартует сервер когда бот готов.

    Пример:
        import keep_alive
        keep_alive.setup_auto_start(bot)
        bot.run(token)
    """
    global _bot
    _bot = bot_instance

    @bot_instance.listen("on_ready")
    async def _dashboard_on_ready():
        await _run_server()
        log.info("Haven Dashboard auto-started via on_ready hook")


# ═══════════════════════════════════════════════════════════════════════════
#  STANDALONE (python keep_alive.py для тестирования)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Starting Haven Dashboard in STANDALONE mode on port {DASHBOARD_PORT}")
    print(f"Password: {DASHBOARD_SECRET}")
    print("(Bot not connected — metrics will be empty)")

    async def _standalone():
        await _run_server()
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(_standalone())
    except KeyboardInterrupt:
        print("Stopped")
