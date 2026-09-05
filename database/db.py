"""
Асинхронное подключение к SQLite и базовая модель SQLAlchemy 2.0.

Для Bothost БД лежит в постоянном хранилище /app/data (переопределяется
переменной окружения DATABASE_URL).
"""

import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///app/data/bot.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def _ensure_data_dir() -> None:
    """Создаёт папку для файла БД, если её ещё нет."""
    path = DATABASE_URL.split("sqlite+aiosqlite:///", 1)[-1]
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

async def init_db():
    _ensure_data_dir()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкая миграция: добавляем новую колонку в уже существующую таблицу.
        row = await conn.execute(text("PRAGMA table_info(bookings)"))
        columns = {r[1] for r in row.fetchall()}
        if columns and "reminder_sent" not in columns:
            await conn.execute(
                text("ALTER TABLE bookings ADD COLUMN reminder_sent BOOLEAN NOT NULL DEFAULT 0")
            )
        await _dedupe_masters(conn)


async def _dedupe_masters(conn) -> None:
    """Объединяет дубликаты мастеров (по имени без учёта регистра).

    Заявки и расписание переносятся на «канонического» мастера
    (с заполненной специализацией, иначе — с минимальным id),
    дубликаты удаляются. Идемпотентно.
    """
    rows = (await conn.execute(text("SELECT id, name, specialization FROM masters"))).fetchall()
    groups: dict[str, list[tuple[int, str | None]]] = {}
    for mid, name, spec in rows:
        key = (name or "").strip().lower()
        if not key:
            continue
        groups.setdefault(key, []).append((mid, spec))

    for items in groups.values():
        if len(items) <= 1:
            continue
        items.sort(key=lambda t: (0 if (t[1] or "").strip() else 1, t[0]))
        canonical = items[0][0]
        duplicates = [t[0] for t in items[1:]]
        for dup in duplicates:
            await conn.execute(
                text("UPDATE bookings SET master_id = :canonical WHERE master_id = :dup"),
                {"canonical": canonical, "dup": dup},
            )
            await conn.execute(
                text("UPDATE master_schedules SET master_id = :canonical WHERE master_id = :dup"),
                {"canonical": canonical, "dup": dup},
            )
            await conn.execute(text("DELETE FROM masters WHERE id = :dup"), {"dup": dup})

    # Схлопываем возможные дубли расписания: рабочий день важнее нерабочего.
    await conn.execute(text(
        """
        UPDATE master_schedules SET is_working = 1
        WHERE EXISTS (
            SELECT 1 FROM master_schedules m2
            WHERE m2.master_id = master_schedules.master_id
              AND m2.work_date = master_schedules.work_date
              AND m2.is_working = 1
        )
        """
    ))
    await conn.execute(text(
        """
        DELETE FROM master_schedules
        WHERE id NOT IN (
            SELECT MIN(id) FROM master_schedules GROUP BY master_id, work_date
        )
        """
    ))


async def close_db():
    await engine.dispose()
