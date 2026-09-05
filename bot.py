#!/usr/bin/env python3
"""
Чат-бот салона красоты для мессенджера MAX (Long Polling) + health-сервер.

Адаптация для хостинга Bothost:
- переменные окружения задаются в панели (MAX_TOKEN/BOT_TOKEN, ADMIN_MAX_IDS);
- SQLite БД лежит в постоянном хранилище /app/data;
- HTTP-сервер слушает порт PORT (по умолчанию 8080) и отвечает на GET /health.

Запуск:
    1. pip install -r requirements.txt
    2. задать переменные окружения
    3. python bot.py
"""

import asyncio
import logging
import os

from aiohttp import web

from database import close_db, init_db
from handlers.common import (
    dispatch_update,
    get_updates,
    max_api_request,
    reminder_loop,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("max_salon_bot")


async def validate_token() -> bool:
    """Проверяет токен запросом GET /me."""
    me = await max_api_request("GET", "/me")
    if me is None:
        logger.error("Токен невалиден или MAX API недоступен.")
        return False
    logger.info("Токен валиден, бот запущен: %s (@%s)", me.get("name"), me.get("username"))
    return True


async def long_polling() -> None:
    marker = None
    while True:
        try:
            data = await get_updates(marker)
            if data is None:
                logger.warning("Не удалось получить обновления, повтор через 3 сек.")
                await asyncio.sleep(3)
                continue

            marker = data.get("marker", marker)
            updates = data.get("updates", [])
            logger.debug("Получено обновлений: %d", len(updates))

            for update in updates:
                try:
                    await dispatch_update(update)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Ошибка обработки обновления: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка long polling: %s", exc)
            await asyncio.sleep(3)


async def run_health_server() -> None:
    """Запускает HTTP-сервер health-проверок для Bothost.

    Слушает 0.0.0.0:PORT (по умолчанию 8080), отвечает 200 на /health и /.
    """
    port = int(os.getenv("PORT", "8080"))

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-сервер запущен на http://0.0.0.0:%d/health", port)

    # Держим сервер в фоновой задаче, пока работает бот.
    while True:
        await asyncio.sleep(3600)


async def main() -> None:
    await init_db()
    logger.info("База данных инициализирована (%s).", os.getenv("DATABASE_URL", "app/data/bot.db"))

    # Health-сервер запускаем первым — Bothost проверяет порт независимо от токена.
    health_task = asyncio.create_task(run_health_server())

    if not await validate_token():
        logger.error("Останавливаюсь: токен невалиден.")
        return

    # Запуск фоновой задачи напоминаний о записи.
    reminder_task = asyncio.create_task(reminder_loop())
    logger.info("Фоновая задача напоминаний запущена")

    try:
        await long_polling()
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass
        await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
