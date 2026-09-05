# Чат-бот салона красоты для MAX — деплой на Bothost

Адаптированная копия бота для развёртывания на хостинге
[Bothost](https://bothost.ru).

Отличия от локальной версии:

- SQLite БД хранится в постоянном томе `/app/data` (переменная `DATABASE_URL`);
- запускается HTTP-сервер health-проверок: слушает `0.0.0.0:PORT`
  (по умолчанию `8080`) и отвечает `200 {"status": "ok"}` на `GET /health`;
- переменные окружения задаются в панели Bothost, а не в `.env`
  (поддерживаются `MAX_TOKEN` и `BOT_TOKEN`).

## Быстрый старт (Docker)

Соберите образ из этой папки:

```bash
docker build -t bot_max_bothost .
```

Запустите контейнер:

```bash
docker run -p 8080:8080 \
  -e MAX_TOKEN=ваш_токен \
  -e ADMIN_MAX_IDS=111111111,222222222 \
  bot_max_bothost
```

Проверка здоровья:

```bash
curl http://localhost:8080/health
# {"status": "ok"}
```

## Деплой на Bothost

1. Создайте проект на [bothost.ru](https://bothost.ru).
2. Загрузите содержимое этой папки (или подключите Git-репозиторий).
3. В настройках проекта укажите:
   - **Главный файл**: `bot.py` (запуск `python bot.py`);
   - **Dockerfile**: включите сборку по кастомному `Dockerfile`
     из корня проекта (если Bothost строит образ автоматически —
     он подхватит `Dockerfile` сам).
4. В разделе **Переменные окружения** задайте:
   - `MAX_TOKEN` (или `BOT_TOKEN`) — токен чат-бота MAX
     (platform.business.max.ru → Чат-боты → Расширенные настройки);
   - `ADMIN_MAX_IDS` — ID администраторов MAX через запятую;
   - `DATABASE_URL` — можно не задавать: по умолчанию
     `sqlite+aiosqlite:///app/data/bot.db`;
   - `PORT` — передаётся Bothost автоматически (по умолчанию `8080`);
   - опционально, данные салона для раздела «Контакты»:
     `SALON_PHONE`, `SALON_ADDRESS`, `SALON_WORK_HOURS`,
     `SALON_LAT`, `SALON_LON`, `SALON_MAP_URL`, `SALON_PROFILE_URL`.
5. Убедитесь, что у проекта примонтирован постоянный том
   на `/app/data` — там хранится БД между деплоями
   (на Bothost это стандартное хранилище для `/app/data`).
6. Привяжите домен (опционально): если Bothost проксирует внешний
   домен на `PORT`, health-проверка будет доступна по адресу
   `https://ваш-домен/health`.
7. Задеплойте проект. В логах появится:
   `Health-сервер запущен на http://0.0.0.0:8080/health`.

## Локальный запуск без Docker

```bash
pip install -r requirements.txt
$env:MAX_TOKEN = "ваш_токен"          # или export MAX_TOKEN=... в Linux/macOS
$env:ADMIN_MAX_IDS = "111111111,222222222"
$env:DATABASE_URL = "sqlite+aiosqlite:///bot_local.db"
python bot.py
```

## Структура

```
bot.py                  # точка входа: Long Polling + health-сервер
config.py               # MAX_TOKEN/BOT_TOKEN, ADMIN_MAX_IDS, данные салона
database/               # SQLAlchemy модели и подключение (БД в /app/data)
handlers/common.py      # HTTP-клиент, FSM, отправка, диспетчеризация, логика
keyboards/builders.py   # построители кнопок и payload
states.py               # состояния FSM
Dockerfile              # сборка образа для Bothost
.dockerignore           # исключения для контекста сборки
```

## Важно

- Токены и секреты передаются только через переменные окружения
  панели Bothost — не кладите их в код и не загружайте `.env`.
- Папка `/app/data` должна сохраняться между деплоями, иначе
  база записей будет обнуляться при каждом релизе.
- Для production документация MAX рекомендует Webhook, а не Long Polling.
- Услуги и мастера удаляются мягко (`is_active = False`).
- Рабочее время: 9:00–19:30 (последний слот), шаг 30 минут, часовой пояс Москва.
