"""Импорт графика работы мастеров из Excel (.xlsx).

Поддерживаются два формата листов:
1) Матричный («График_сентябрь»):
   - строка с названием месяца (например, «Сентябрь»);
   - строка-заголовок с числами месяца (1..31) или датами;
   - первый столбец — имена мастеров;
   - «X» (регистронезависимо) — рабочий день, иначе — нерабочий.
2) Длинный («По датам»): колонки Мастер, Дата (дд.мм.гггг), День недели,
   Статус («работает» → рабочий день).

Для дат из файла старые строки расписания удаляются и вставляются новые.
"""

import logging
import re
from datetime import date, datetime

import openpyxl
from sqlalchemy import delete, select

from database import async_session, Master, MasterSchedule

logger = logging.getLogger(__name__)

# Русские названия месяцев (для строки-заголовка матричного листа).
_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}

_XLSX_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_date_value(value, default_year: int) -> date | None:
    """Дата из ячейки: datetime/date или строка 'дд.мм.гггг'."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _cell_text(value)
    m = _XLSX_DATE_RE.match(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _day_of_month(day: int, year: int, month: int = None) -> date | None:
    """Возвращает дату для номера дня в месяце/year (month=None — не задано)."""
    if month is None or not (1 <= day <= 31):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_from_title(value) -> int | None:
    text = _cell_text(value).lower()
    for name, num in _MONTHS.items():
        if name in text:
            return num
    return None


def _parse_matrix_sheet(ws, filename: str) -> dict[tuple[str, date], bool]:
    """Парсит матричный лист: заголовки-дни и строки мастеров с X."""
    result: dict[tuple[str, date], bool] = {}

    year: int | None = None
    ym = _YEAR_RE.search(filename)
    if ym:
        year = int(ym.group(0))

    header_row_number: int | None = None
    day_numbers: list[int] = []
    month: int | None = None

    # Первые 12 строк: ищем название месяца и строку-заголовок с днями.
    for r_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=min(ws.max_row, 12), values_only=True), start=1
    ):
        cells = list(row)
        if month is None:
            for c in cells:
                m = _month_from_title(c)
                if m:
                    month = m
                    break
        if header_row_number is not None:
            continue
        if not cells or _cell_text(cells[0]).lower() not in ("мастер", "мастера"):
            continue
        nums = []
        for c in cells[1:]:
            t = _cell_text(c)
            if t.isdigit() and 1 <= int(t) <= 31:
                nums.append(int(t))
        if nums:
            header_row_number = r_idx
            day_numbers = nums

    if not day_numbers:
        return result

    if month is None:
        month = _month_from_title(ws.title) or _month_from_title(ws["A1"].value)
    if year is None:
        year = datetime.now().year

    # Строки мастеров идут после строки-заголовка.
    start_row = header_row_number + 1
    for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row, values_only=True):
        cells = list(row)
        master_name = _cell_text(cells[0]) if cells else ""
        if not master_name:
            continue
        low = master_name.lower()
        # Строки-легенды («X — рабочий день», подписи) заканчивают блок мастеров.
        if low.startswith("x") or "график" in low:
            break
        for col_idx, day in enumerate(day_numbers):
            value = cells[1 + col_idx] if 1 + col_idx < len(cells) else None
            work_day = _day_of_month(day, year, month)
            if work_day is None:
                continue
            working = _cell_text(value).upper() == "X"
            # X приоритетнее, чем пустая ячейка (на случай пересечения листов).
            result[(master_name, work_day)] = result.get((master_name, work_day), False) or working
    return result


def _parse_long_sheet(ws) -> dict[tuple[str, date], bool]:
    """Парсит длинный лист «По датам»: Мастер / Дата / ... / Статус."""
    result: dict[tuple[str, date], bool] = {}
    header: dict[str, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 5), values_only=True):
        cells = [c for c in (row or [])]
        # Ищем строку с колонками Мастер / Дата / Статус.
        lowered = [_cell_text(c).lower() for c in cells]
        if "мастер" in lowered and "дата" in lowered:
            header = {name: i for i, name in enumerate(lowered) if name}
            break
    if not header:
        return result

    name_col = next((i for i, n in header.items() if n == "мастер"), 0)
    date_col = next((i for i, n in header.items() if n == "дата"), 1)
    status_col = header.get("статус", None)

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
        cells = list(row or [])
        if name_col >= len(cells):
            continue
        master_name = _cell_text(cells[name_col])
        if not master_name or "график" in master_name.lower():
            continue
        d = _parse_date_value(cells[date_col] if date_col < len(cells) else None, datetime.now().year)
        if d is None:
            continue
        working = False
        if status_col is not None and status_col < len(cells):
            working = _cell_text(cells[status_col]).lower() == "работает"
        # X в колонке статуса тоже считаем рабочим днём.
        working = working or _cell_text(cells[status_col]).upper() == "X"
        result[(master_name, d)] = result.get((master_name, d), False) or working
    return result


def parse_workbook(file_path: str) -> dict[tuple[str, date], bool]:
    """Читает книгу и возвращает {(имя мастера, дата): работает}.

    Матричные листы (X-сетка) приоритетнее: если найден хотя бы один,
    длинные листы («По датам») не учитываются — они дублируют те же данные
    именами-сокращениями.
    """
    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    matrix: dict[tuple[str, date], bool] = {}
    long: dict[tuple[str, date], bool] = {}
    for ws in wb.worksheets:
        sheet_data = _parse_matrix_sheet(ws, file_path)
        if sheet_data:
            for key, working in sheet_data.items():
                matrix[key] = matrix.get(key, False) or working
        else:
            sheet_data = _parse_long_sheet(ws)
            for key, working in sheet_data.items():
                long[key] = long.get(key, False) or working
    wb.close()
    return matrix if matrix else long


async def import_schedule_from_excel(file_path: str) -> dict:
    """Импортирует график из Excel в таблицу master_schedules.

    Возвращает отчёт: количество мастеров, дат, рабочих дней и
    список имён мастеров из файла, не найденных в БД.
    """
    parsed = parse_workbook(file_path)
    if not parsed:
        raise ValueError("В файле не найдено данных о графике (X-меток или строк «работает»).")

    async with async_session() as session:
        masters = (await session.execute(select(Master).order_by(Master.id))).scalars().all()
        # При дублях имён каноническим считаем мастера с минимальным id.
        master_by_name: dict[str, Master] = {}
        for m in masters:
            master_by_name.setdefault(m.name.strip().lower(), m)

        work_dates = sorted({d for _, d in parsed})
        working_days = sum(1 for v in parsed.values() if v)

        # Удаляем старые строки для дат из файла и вставляем новые.
        await session.execute(
            delete(MasterSchedule).where(MasterSchedule.work_date.in_(work_dates))
        )

        skipped: list[str] = []
        inserted = 0
        for (name, d), working in sorted(parsed.items()):
            master = master_by_name.get(name.strip().lower())
            if master is None:
                if name not in skipped:
                    skipped.append(name)
                continue
            session.add(
                MasterSchedule(master_id=master.id, work_date=d, is_working=working)
            )
            inserted += 1
        await session.commit()

    report = {
        "masters": len(master_by_name),
        "dates": len(work_dates),
        "working_days": working_days,
        "inserted": inserted,
        "skipped_masters": skipped,
    }
    logger.info("Импорт графика: %s", report)
    return report
