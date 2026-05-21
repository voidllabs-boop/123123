# Haven

Модерационный Discord-бот на Python (disnake >= 2.11) с MongoDB и Discord Components V2.

Полнофункциональный бот для модерации, управления и автоматизации Discord-сервера.
Все сообщения -- через Components V2, без embeds и без эмодзи. Хранение данных -- MongoDB.

## Запуск

```bash
pip install -r requirements.txt
HAVEN_TOKEN=<your-bot-token> MONGO_URI=mongodb://localhost:27017 python main.py
```

**Переменные окружения:**
| Переменная | Описание | Умолчание |
|-----------|----------|-----------|
| `HAVEN_TOKEN` | Токен бота (обязательно) | -- |
| `MONGO_URI` | URI подключения к MongoDB | `mongodb://localhost:27017` |
| `MONGO_DB` | Имя базы данных | `haven` |

## Возможности (99 команд)

### Модерация
| Команда | Описание |
|---------|----------|
| `/mute` | Тайм-аут с интерактивным выбором длительности |
| `/unmute` | Снять тайм-аут |
| `/warn` | Предупреждение (+ авто-действия по порогам) |
| `/warnings` | Список предупреждений |
| `/delwarn` | Удалить конкретный варн |
| `/clearwarns` | Очистить все варны |
| `/kick` | Кикнуть участника |
| `/softban` | Бан + разбан (удаление сообщений) |
| `/ban` | Бан (с поддержкой временного) |
| `/unban` | Разбан по ID |
| `/massban` | Бан нескольких по ID |
| `/purge` | Удаление сообщений с фильтрами |
| `/purgeuser` | Удалить сообщения конкретного пользователя |
| `/slowmode` | Медленный режим |
| `/slowmodepresets` | Пресеты slowmode (calm/moderate/strict/lockdown) |
| `/lock` / `/unlock` | Заблокировать / разблокировать канал |
| `/lockdown` / `/unlockdown` | Блокировка всего сервера |
| `/nuke` | Пересоздать канал |
| `/quarantine` / `/unquarantine` | Карантин с сохранением ролей |
| `/massnick` | Массовое изменение ников |

### Система правил
| Команда | Описание |
|---------|----------|
| `/rules add/remove/edit/list/clear` | Управление правилами |
| `/rules display` | Красивая панель правил в канале |
| `/rules setrole` | Кнопка "Принять правила" с выдачей роли |

### Анти-нюк защита
| Команда | Описание |
|---------|----------|
| `/antinuke enable/disable/status` | Защита от нюка |
| `/antinuke trusted` | Доверенные пользователи |

### Авто-модерация
| Команда | Описание |
|---------|----------|
| `/automod toggle` | Включить/выключить модуль |
| `/automod status` | Статус модулей |
| `/warnthresholds set/remove/list` | Авто-действия по кол-ву варнов |
| `/whitelist add/remove/list` | Белый список доменов |

Модули: анти-спам, анти-рейд, анти-капс, анти-ссылки, анти-инвайты, анти-упоминания

### Голосовые каналы
`/voice kick/move/moveall/limit/mute/unmute/deafen/undeafen`

### Управление каналами и ветками
| Команда | Описание |
|---------|----------|
| `/channel clone/rename/topic/create/delete` | Каналы |
| `/thread create/archive/lock/unlock/rename` | Ветки |

### Тикет-система
- Панель с 6 категориями
- 4 уровня приоритета
- Claim, транскрипт, архив, удаление

### Модмейл
| Команда | Описание |
|---------|----------|
| `/modmail` | Анонимное обращение к модераторам |
| `/modmailreply` | Ответ через ЛС |

### Самоназначаемые роли
`/selfroles create/addrole/send` -- панели ролей с dropdown

### Информация
| Команда | Описание |
|---------|----------|
| `/userinfo`, `/avatar`, `/banner` | Пользователь |
| `/serverinfo`, `/serversettings` | Сервер |
| `/roleinfo`, `/rolehierarchy`, `/rolecount` | Роли |
| `/channelinfo`, `/channelcount` | Каналы |
| `/history`, `/stats`, `/modleaderboard` | Модерация |
| `/invites`, `/inviteinfo`, `/createinvite` | Приглашения |
| `/boosts`, `/botlist`, `/membercount` | Статистика |
| `/oldestmembers`, `/newestmembers`, `/joinposition` | Участники |
| `/slowmodeinfo`, `/messagestats` | Аналитика |
| `/permissions`, `/color`, `/firstmessage`, `/topic` | Утилиты |
| `/whois`, `/emojiinfo` | Поиск |

### Инструменты
| Команда | Описание |
|---------|----------|
| `/note add/list/delete/clear` | Заметки модераторов |
| `/role add/remove/members` | Управление ролями |
| `/roleall` | Массовая выдача ролей |
| `/rolecolor` | Изменение цвета роли |
| `/reactionrole create/addrole/send` | Реакционные роли |
| `/afk` | AFK статус |
| `/remind` | Напоминания |
| `/giveaway start/end/reroll` | Розыгрыши |
| `/poll create` | Голосования |
| `/snipe`, `/editsnipe` | Удаленные/редактированные сообщения |
| `/suggest` | Система предложений |
| `/report`, `/reports`, `/resolve` | Жалобы |
| `/verify` | Верификация |
| `/counting` | Игра-счетчик |
| `/say`, `/announce` | Сообщения от бота |
| `/embed` | Кастомные сообщения |
| `/dm` | ЛС от бота |
| `/emojisteal` | Копирование эмодзи |
| `/sticky set/remove` | Закрепленные сообщения |
| `/schedule message/list` | Отложенные сообщения |
| `/customcmd add/remove/list` | Кастомные команды |
| `/autoresponder add/remove/list` | Авто-ответы |
| `/cleanup bots/links/images/contains/embeds` | Продвинутая очистка |
| `/backup`, `/restore` | Экспорт/импорт настроек |
| `/exportwarns`, `/exportcases` | Экспорт модерации |
| `/nick set/reset` | Управление ником |
| `/haven`, `/ping` | О боте |

### Логирование событий
- Вход / выход участников (+ вехи)
- Изменение ролей
- Удаление / редактирование сообщений
- Создание / удаление каналов и ролей
- Бан / разбан (+ анти-нюк)
- Голосовые каналы

## Хранилище (MongoDB)

Бот использует MongoDB для хранения всех данных. Коллекции:

| Коллекция | Содержимое |
|-----------|-----------|
| `config` | Настройки серверов |
| `warnings` | Предупреждения |
| `cases` | Кейсы модерации |
| `tickets` | Тикеты |
| `tempbans` | Временные баны |
| `rules` | Правила серверов |
| `warnthresholds` | Пороги варнов |
| `notes` | Заметки модераторов |
| `modmail` | Обращения модмейла |
| `selfroles` | Панели ролей |
| `customcmds` | Кастомные команды |
| `autoresponders` | Авто-ответы |
| `scheduled` | Отложенные сообщения |
| `quarantined` | Карантин |
| `antinuke_trusted` | Доверенные (анти-нюк) |
| `whitelist` | Белый список доменов |
| `reminders` | Напоминания |
| `giveaways` | Розыгрыши |
| `reports` | Жалобы |
| `suggestions` | Предложения |

## Технические детали

- Python 3.11+
- disnake >= 2.11
- MongoDB (motor >= 3.3)
- Discord Components V2 (без embeds)
- Все в одном файле `main.py` (~7800 строк)
- 99 slash-команд
- 4 фоновые задачи: темпбаны (20с), напоминания (15с), розыгрыши (30с), расписание (30с)
- Монохромная цветовая схема без эмодзи
- Интерфейс полностью на русском
- In-memory кеш + асинхронная запись в MongoDB
