# Haven

Модерационный Discord-бот на Python (disnake >= 2.11) с использованием Discord Components V2.

## Запуск

```bash
pip install -r requirements.txt
HAVEN_TOKEN=<your-bot-token> python main.py
```

## Команды

| Команда | Описание | Права |
|---------|----------|-------|
| `/setup logs` | Канал для логов модерации | administrator |
| `/setup welcome` | Канал приветствий | administrator |
| `/setup leave` | Канал прощаний | administrator |
| `/setup tickets` | Панель тикетов | administrator |
| `/setup info` | Текущие настройки | administrator |
| `/mute` | Тайм-аут участника | moderate_members |
| `/unmute` | Снять тайм-аут | moderate_members |
| `/warn` | Предупреждение | moderate_members |
| `/warnings` | Список предупреждений | moderate_members |
| `/clearwarns` | Очистить варны | administrator |
| `/kick` | Кикнуть участника | kick_members |
| `/ban` | Забанить участника | ban_members |
| `/unban` | Разбанить по ID | ban_members |
| `/purge` | Удалить сообщения | manage_messages |
| `/slowmode` | Медленный режим | manage_channels |
| `/lock` | Заблокировать канал | manage_channels |
| `/unlock` | Разблокировать канал | manage_channels |
| `/userinfo` | Информация о пользователе | - |
| `/history` | История кейсов | moderate_members |

## Хранилище

Данные хранятся в JSON-файлах в папке `data/`:

- `config.json` -- настройки серверов
- `warnings.json` -- предупреждения
- `tickets.json` -- открытые тикеты
- `tempbans.json` -- временные баны
- `cases.json` -- все кейсы модерации
