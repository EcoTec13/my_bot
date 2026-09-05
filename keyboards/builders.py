"""
Построители inline-клавиатур MAX и payload-фабрики.

Формат клавиатуры MAX:
attachments: [
  {
    "type": "inline_keyboard",
    "payload": {
      "buttons": [
        [ {"type": "callback", "text": "Кнопка", "payload": "..."} ],
        ...
      ]
    }
  }
]

Payload кнопки — JSON-строка вида {"cmd": "...", ...аргументы}.
"""

import json
from typing import Any


def make_payload(cmd: str, **kwargs: Any) -> str:
    """Формирует JSON-строку payload для callback-кнопки."""
    return json.dumps({"cmd": cmd, **kwargs}, ensure_ascii=False)


def _btn(text: str, cmd: str, **kwargs: Any) -> dict:
    return {"type": "callback", "text": text, "payload": make_payload(cmd, **kwargs)}


def link_button(text: str, url: str) -> dict[str, Any]:
    """Кнопка-ссылка. MAX принимает только http/https ссылки."""
    return {"type": "link", "text": text, "url": url}


def geo_request_button(text: str) -> dict[str, Any]:
    """Кнопка запроса геолокации пользователя (ответ придёт location-вложением)."""
    return {"type": "request_geo_location", "text": text}


def inline_keyboard(rows: list[list[dict]]) -> list[dict]:
    """Возвращает список attachments с одной inline-клавиатурой."""
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def _pager(page: int, total_pages: int, cmd: str) -> list[dict]:
    """Кнопки навигации «Назад/Вперёд» для многостраничных списков."""
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ Назад", cmd, page=page - 1))
    if page < total_pages - 1:
        nav.append(_btn("Вперёд ➡️", cmd, page=page + 1))
    return nav


# --- Главное меню / профиль ---

def build_main_menu(is_admin: bool = False) -> list[dict]:
    rows = [
        [_btn("📝 Оставить заявку", "menu_services")],
        [_btn("👤 Профиль", "profile")],
        [_btn("📍 Контакты", "contacts")],
    ]
    if is_admin:
        rows.append([_btn("🛠 Админ-панель", "admin_menu")])
    return inline_keyboard(rows)


def build_contacts_keyboard() -> list[dict]:
    """Клавиатура раздела «Контакты»: профиль на Яндексе, геолокация, назад."""
    from config import SALON_PROFILE_URL

    rows = [
        [link_button("📱 Профиль на Яндекс", SALON_PROFILE_URL)],
        [geo_request_button("📍 Поделиться геолокацией")],
        [_btn("🔙 Назад", "menu")],
    ]
    return inline_keyboard(rows)


def build_start_button() -> list[dict]:
    """Клавиатура с кнопкой «Начать» для неопознанных команд."""
    return inline_keyboard([[_btn("▶️ Начать", "menu")]])


def build_profile_menu(has_phone: bool = False, has_active_bookings: bool = False) -> list[dict]:
    # Имя не редактируется и не хранится (минимизация персональных данных).
    rows = [
        [_btn("✏️ Изменить телефон", "profile_edit_phone")],
        # Кнопка «поделиться контактом»: MAX сам запрашивает номер из профиля.
        [{"type": "request_contact", "text": "📱 Поделиться номером"}],
    ]
    if has_active_bookings:
        rows.append([_btn("📋 Мои записи", "booking_history")])
    else:
        rows.append([_btn("📋 История записей", "booking_history")])
    if has_phone:
        rows.append([_btn("🗑 Удалить телефон", "profile_delete_phone")])
    rows.append([_btn("🏠 В меню", "menu")])
    return inline_keyboard(rows)


def build_profile_cancel() -> list[dict]:
    """Клавиатура отмены для шагов редактирования профиля."""
    return inline_keyboard([[_btn("🔙 Отмена", "profile_cancel")]])


def build_profile_delete_confirm() -> list[dict]:
    """Подтверждение удаления номера телефона."""
    rows = [
        [
            _btn("✅ Да, удалить", "profile_delete_phone_confirm"),
            _btn("🔙 Отмена", "profile_cancel"),
        ],
    ]
    return inline_keyboard(rows)


# --- История записей пользователя ---

# Подписи фильтров в истории записей.
FILTER_LABELS = {
    "all": "Все",
    "new": "🆕 Новые",
    "confirmed": "✅ Подтверждённые",
    "declined": "❌ Отклонённые",
    "done": "🏁 Завершённые",
}

def build_booking_history_list(
    bookings: list[dict],
    current: str,
    statuses: list[str],
    page: int = 0,
    total_pages: int = 1,
) -> list[dict]:
    """Список записей пользователя с фильтрами и пагинацией.

    bookings — список {"id": int, "label": str}; current — активный фильтр.
    Не более 5-7 записей на страницу, чтобы не превысить лимит рядов MAX.
    """
    rows = []
    filter_row = []
    for st in statuses:
        label = FILTER_LABELS.get(st, st)
        label = f"[{label}]" if st == current else label
        filter_row.append(_btn(label, "booking_history_filter", status=st))
    # Разбиваем фильтры на ряды по 3 кнопки.
    for i in range(0, len(filter_row), 3):
        rows.append(filter_row[i : i + 3])
    for b in bookings:
        rows.append([_btn(b["label"], "booking_history_detail", booking_id=b["id"])])
    # Навигация по страницам (с сохранением фильтра).
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(
                _btn("⬅️ Назад", "booking_history_page", status=current, page=page - 1)
            )
        if page < total_pages - 1:
            nav.append(
                _btn("Вперёд ➡️", "booking_history_page", status=current, page=page + 1)
            )
        if nav:
            rows.append(nav)
    rows.append([_btn("🔙 Назад в профиль", "profile")])
    return inline_keyboard(rows)


def build_booking_history_detail() -> list[dict]:
    """Клавиатура карточки записи в истории."""
    rows = [
        [_btn("🔙 К списку записей", "booking_history")],
        [_btn("👤 В профиль", "profile")],
    ]
    return inline_keyboard(rows)


# --- Услуги ---

def build_services_list(services: list[dict], page: int, total_pages: int) -> list[dict]:
    rows = []
    for s in services:
        rows.append(
            [_btn(f"{s['name']} — {s['price']} ₽", "service_select", service_id=s["id"])]
        )
    nav = _pager(page, total_pages, "booking_services_page")
    if nav:
        rows.append(nav)
    rows.append([_btn("🏠 В меню", "menu")])
    return inline_keyboard(rows)


# --- Мастера (для записи) ---

def build_masters_list(masters: list[dict], show_any: bool = True) -> list[dict]:
    """Список мастеров одной страницей: «Любой свободный» сверху, затем мастера."""
    rows = []
    if show_any:
        rows.append([_btn("🤝 Любой свободный", "master_select", master_id="any")])
    for m in masters:
        label = m["name"]
        if m.get("specialization"):
            label += f" — {m['specialization']}"
        rows.append([_btn(label, "master_select", master_id=m["id"])])
    rows.append([_btn("🔙 Отмена", "booking_cancel")])
    return inline_keyboard(rows)


# --- Дата / время ---

def build_date_buttons(dates: list[dict]) -> list[dict]:
    rows = []
    for i, d in enumerate(dates):
        if i % 2 == 0:
            rows.append([])
        rows[-1].append(_btn(d["label"], "date_select", date=d["value"]))
    rows.append([_btn("🔙 Отмена", "booking_cancel")])
    return inline_keyboard(rows)


def build_hour_buttons(hours: list[str], page: int, total_pages: int) -> list[dict]:
    rows = []
    for i, h in enumerate(hours):
        if i % 3 == 0:
            rows.append([])
        rows[-1].append(_btn(f"{h}:00", "time_hour", hour=h))
    nav = _pager(page, total_pages, "time_hour_page")
    if nav:
        rows.append(nav)
    rows.append([_btn("🔙 Отмена", "booking_cancel")])
    return inline_keyboard(rows)


def build_minute_buttons(minutes: list[str]) -> list[dict]:
    rows = []
    for i, m in enumerate(minutes):
        if i % 2 == 0:
            rows.append([])
        rows[-1].append(_btn(f":{m}", "time_minute", minute=m))
    rows.append([_btn("🔙 Отмена", "booking_cancel")])
    return inline_keyboard(rows)


def build_booking_confirm() -> list[dict]:
    rows = [
        [
            _btn("✅ Подтвердить", "booking_confirm"),
            _btn("❌ Отмена", "booking_cancel"),
        ],
    ]
    return inline_keyboard(rows)


def build_post_booking() -> list[dict]:
    """Клавиатура после успешной подачи заявки."""
    rows = [
        [_btn("🆕 Оставить ещё заявку", "menu_services")],
        [_btn("🏠 В меню", "menu")],
    ]
    return inline_keyboard(rows)


# --- Админ-панель ---

def build_admin_menu() -> list[dict]:
    rows = [
        [_btn("💇 Услуги", "admin_services")],
        [_btn("👨‍🎨 Мастера", "admin_masters")],
        [_btn("📅 Заявки", "admin_bookings")],
        [_btn("📅 Загрузить график", "admin_upload_schedule")],
        [_btn("🏠 В меню", "menu")],
    ]
    return inline_keyboard(rows)


def build_admin_services_list(services: list[dict]) -> list[dict]:
    rows = [[_btn("➕ Добавить услугу", "admin_service_add")]]
    for s in services:
        status = "🟢" if s["is_active"] else "🔴"
        rows.append(
            [_btn(f"{status} {s['name']} ({s['price']} ₽)", "admin_service_edit", service_id=s["id"])]
        )
    rows.append([_btn("🔙 Админ-меню", "admin_menu")])
    return inline_keyboard(rows)


def build_admin_service_edit(service: dict) -> list[dict]:
    sid = service["id"]
    rows = [
        [
            _btn("✏️ Название", "admin_service_edit_field", service_id=sid, field="name"),
            _btn("✏️ Описание", "admin_service_edit_field", service_id=sid, field="description"),
        ],
        [
            _btn("✏️ Цена", "admin_service_edit_field", service_id=sid, field="price"),
            _btn("✏️ Длительность", "admin_service_edit_field", service_id=sid, field="duration"),
        ],
        [
            _btn(
                "🙈 Скрыть" if service["is_active"] else "👁 Показать",
                "admin_service_toggle",
                service_id=sid,
            )
        ],
        [_btn("🔙 К услугам", "admin_services")],
    ]
    return inline_keyboard(rows)


def build_admin_masters_list(masters: list[dict]) -> list[dict]:
    rows = [[_btn("➕ Добавить мастера", "admin_master_add")]]
    for m in masters:
        status = "🟢" if m["is_active"] else "🔴"
        rows.append([_btn(f"{status} {m['name']}", "admin_master_edit", master_id=m["id"])])
    rows.append([_btn("🔙 Админ-меню", "admin_menu")])
    return inline_keyboard(rows)


def build_admin_master_edit(master: dict) -> list[dict]:
    mid = master["id"]
    rows = [
        [
            _btn("✏️ Имя", "admin_master_edit_field", master_id=mid, field="name"),
            _btn("✏️ Специализация", "admin_master_edit_field", master_id=mid, field="specialization"),
        ],
        [
            _btn(
                "🙈 Скрыть" if master["is_active"] else "👁 Показать",
                "admin_master_toggle",
                master_id=mid,
            )
        ],
        [_btn("🔙 К мастерам", "admin_masters")],
    ]
    return inline_keyboard(rows)


def build_admin_bookings_list(
    bookings: list[dict], statuses: list[str], current: str
) -> list[dict]:
    filter_row = []
    for st in statuses:
        label = f"[{st}]" if st == current else st
        filter_row.append(_btn(label, "admin_bookings_filter", status=st))
    rows = [filter_row]
    for b in bookings:
        label = f"#{b['id']} {b['user_name']} — {b['service_name']} ({b['status']})"
        rows.append([_btn(label, "admin_booking_view", booking_id=b["id"])])
    rows.append([_btn("🔙 Админ-меню", "admin_menu")])
    return inline_keyboard(rows)


def build_admin_booking_view(booking: dict) -> list[dict]:
    bid = booking["id"]
    rows = [
        [
            _btn("✅ Подтвердить", "admin_booking_status", booking_id=bid, status="confirmed"),
            _btn("❌ Отклонить", "admin_booking_status", booking_id=bid, status="declined"),
        ],
        [_btn("🏁 Завершить", "admin_booking_status", booking_id=bid, status="done")],
        [_btn("🔙 К заявкам", "admin_bookings")],
    ]
    return inline_keyboard(rows)
