import asyncio
import json
import logging
import os
import re
import ssl
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import aiohttp
import certifi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from config import MAX_TOKEN, API_BASE_URL, ADMIN_IDS, MSK, SCHEDULE_FILE_PATH
from database import async_session, User, Service, Master, Booking, MasterSchedule
from keyboards import (
    build_admin_booking_view,
    build_admin_bookings_list,
    build_admin_master_edit,
    build_admin_masters_list,
    build_admin_menu,
    build_admin_service_edit,
    build_admin_services_list,
    build_booking_confirm,
    build_booking_history_detail,
    build_booking_history_list,
    build_contacts_keyboard,
    build_date_buttons,
    build_hour_buttons,
    build_main_menu,
    build_masters_list,
    build_minute_buttons,
    build_post_booking,
    build_profile_cancel,
    build_profile_delete_confirm,
    build_profile_menu,
    build_services_list,
    build_start_button,
)
import states

logger = logging.getLogger(__name__)

# Подписи статусов заявок (единственное место задания текстов).
STATUS_LABELS = {
    "new": "🆕 Новая",
    "confirmed": "✅ Подтверждена",
    "declined": "❌ Отклонена",
    "done": "🏁 Завершена",
}

# Возможные фильтры истории записей (для пользователя).
HISTORY_STATUSES = ["all", "new", "confirmed", "declined", "done"]

FSM_STATES: Dict[int, str] = {}
BOOKING_DATA: Dict[int, dict] = {}
ADMIN_CTX: Dict[int, dict] = {}

def set_state(user_id: int, state: str) -> None:
    FSM_STATES[user_id] = state

def get_state(user_id: int) -> Optional[str]:
    return FSM_STATES.get(user_id)

def delete_state(user_id: int) -> None:
    FSM_STATES.pop(user_id, None)

def get_booking_data(user_id: int) -> dict:
    return BOOKING_DATA.get(user_id, {})

def set_booking_data(user_id: int, data: dict) -> None:
    BOOKING_DATA[user_id] = data

def clear_booking_data(user_id: int) -> None:
    BOOKING_DATA.pop(user_id, None)

def get_admin_ctx(user_id: int) -> dict:
    return ADMIN_CTX.get(user_id, {})

def set_admin_ctx(user_id: int, data: dict) -> None:
    ADMIN_CTX[user_id] = data

def clear_admin_ctx(user_id: int) -> None:
    ADMIN_CTX.pop(user_id, None)

async def max_api_request(method: str, path: str, **kwargs) -> Optional[Dict]:
    url = f"{API_BASE_URL}{path}"
    headers = {
        "Authorization": MAX_TOKEN,
        "Content-Type": "application/json",
    }
    if "headers" in kwargs:
        headers.update(kwargs.pop("headers"))
    try:
        # Исправление SSL-ошибки ("certificate verify failed"): используем
        # корневые сертификаты из certifi вместо системного хранилища.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url, headers=headers, ssl=ssl_context, **kwargs
            ) as resp:
                if resp.status == 401:
                    logger.error("Ошибка авторизации: неверный токен")
                    return None
                if resp.status == 429:
                    logger.warning("Превышен лимит запросов (30 rps)")
                    return None
                if resp.status == 503:
                    logger.warning("Сервис MAX недоступен")
                    return None
                if resp.status >= 400:
                    logger.error(f"Ошибка HTTP {resp.status}: {await resp.text()}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.exception(f"Ошибка при запросе к MAX API: {e}")
        return None

async def send_message(user_id: int, text: str, attachments: Optional[List] = None) -> bool:
    if not text and not attachments:
        return True
    payload = {"text": text}
    if attachments:
        payload["attachments"] = attachments
    result = await max_api_request("POST", f"/messages?user_id={user_id}", json=payload)
    return result is not None

async def send_message_with_keyboard(user_id: int, text: str, keyboard: List[Dict]) -> bool:
    return await send_message(user_id, text, keyboard)

async def get_updates(marker: Optional[int] = None) -> Optional[Dict]:
    params = {
        "timeout": 30,
        "types": "message_created,message_callback,bot_started",
    }
    if marker is not None:
        params["marker"] = marker
    result = await max_api_request("GET", "/updates", params=params)
    return result

async def get_user_or_create(max_id: int) -> User:
    """Возвращает пользователя по max_id, создавая запись при первом обращении.

    В соответствии с принципом минимизации персональных данных сохраняем
    только max_id и (по желанию пользователя) номер телефона — без имени.
    """
    async with async_session() as session:
        stmt = select(User).where(User.max_id == max_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if user is None:
            user = User(max_id=max_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user

def _extract_user_id(update: Dict) -> Optional[int]:
    """Достаёт user_id из update с учётом типа обновления MAX Bot API.

    Форматы MAX Bot API:
      - message_callback: update.callback.user.user_id
      - message_created:  update.message.sender.user_id
      - bot_started:      update.user.user_id
    """
    update_type = update.get("update_type")

    if update_type == "message_callback":
        user = (update.get("callback") or {}).get("user") or {}
        uid = user.get("user_id")
    elif update_type == "message_created":
        sender = (update.get("message") or {}).get("sender") or {}
        uid = sender.get("user_id")
    else:
        user = update.get("user") or {}
        uid = user.get("user_id")

    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def _normalize_phone(raw: str) -> Optional[str]:
    """Приводит номер к виду +7XXXXXXXXXX; иначе возвращает None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def _extract_phone_from_contact(body: Dict) -> Optional[str]:
    """Достаёт телефон из вложения-контакта (message.body.attachments)."""
    for att in (body.get("attachments") or []):
        if att.get("type") != "contact":
            continue
        payload = att.get("payload") or {}
        vcf = payload.get("vcf_info") or ""
        for line in vcf.replace("\r\n", "\n").split("\n"):
            if line.strip().upper().startswith("TEL") and ":" in line:
                phone = _normalize_phone(line.split(":", 1)[1].strip())
                if phone:
                    return phone
        for key in ("phone", "phone_number", "mobile"):
            phone = _normalize_phone(str(payload.get(key) or ""))
            if phone:
                return phone
        max_info = payload.get("max_info") or {}
        for key in ("phone", "phone_number", "mobile"):
            phone = _normalize_phone(str(max_info.get(key) or ""))
            if phone:
                return phone
    return None


async def dispatch_update(update: Dict) -> None:
    update_type = update.get("update_type")
    logger.debug("Полный update: %s", update)

    user_id = _extract_user_id(update)
    if user_id is None:
        logger.warning("Обновление без user_id, пропускаем: %s", update_type)
        return

    try:
        await _dispatch_update(update, update_type, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка обработки обновления: %s", exc)
        try:
            await send_message_with_keyboard(
                user_id,
                "⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите «▶️ Начать».",
                build_start_button(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить сообщение об ошибке пользователю %s", user_id)


async def _dispatch_update(update: Dict, update_type: str, user_id: int) -> None:
    if update_type == "bot_started":
        # Имя пользователя не сохраняем — только факт старта диалога (минимизация ПДн).
        await get_user_or_create(user_id)
        async with async_session() as session:
            stmt = (
                select(User)
                .options(selectinload(User.bookings))
                .where(User.max_id == user_id)
            )
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
        # Новый пользователь: нет телефона и ни одной записи.
        is_new = bool(user) and not user.phone and not (user.bookings or [])
        if is_new:
            await show_welcome(user_id)
        else:
            await show_main_menu(user_id)
        return

    if update_type == "message_callback":
        callback = update.get("callback") or {}
        payload_str = callback.get("payload")
        logger.debug("Сырой payload из update: %s", payload_str)
        if not payload_str:
            logger.warning("Callback без payload, игнорируем")
            return
        try:
            payload = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Невалидный payload: %s", payload_str)
            return
        await handle_callback(user_id, payload)
        return

    if update_type == "message_created":
        message = update.get("message") or {}
        msg_body = message.get("body") or {}

        # Проверяем наличие геолокации во вложениях (кнопка «Поделиться геолокацией»).
        attachments = list(msg_body.get("attachments") or [])
        attachments += list(message.get("attachments") or [])
        for att in attachments:
            if att.get("type") != "location":
                continue
            payload = att.get("payload") or {}
            # В MAX координаты лежат на верхнем уровне вложения; payload — запасной вариант.
            raw_lat = att.get("latitude", payload.get("lat", payload.get("latitude")))
            raw_lon = att.get("longitude", payload.get("lon", payload.get("longitude")))
            try:
                lat = float(raw_lat)
                lon = float(raw_lon)
            except (TypeError, ValueError):
                lat = None
                lon = None
            if lat is not None and lon is not None:
                await handle_user_location(user_id, lat, lon)
                return

        phone = _extract_phone_from_contact(msg_body)
        if phone:
            await save_phone_from_profile(user_id, phone)
            return
        text = (msg_body.get("text") or "").strip()
        if not text:
            return
        await handle_message(user_id, text)
        return

    logger.info("Неизвестный тип обновления: %s", update_type)

async def show_welcome(user_id: int) -> None:
    """Приветствие для нового пользователя (первый запуск бота)."""
    text = (
        "👋 Привет! Я — бот салона красоты.\n\n"
        "Я помогу вам:\n"
        "• записаться на услугу (💇 стрижка, окрашивание, уходовые процедуры и другое);\n"
        "• посмотреть и управлять своими записями;\n"
        "• узнать контакты салона.\n\n"
        "Нажмите кнопку ниже, чтобы начать."
    )
    await send_message_with_keyboard(user_id, text, build_start_button())

async def show_main_menu(user_id: int) -> None:
    delete_state(user_id)
    clear_booking_data(user_id)
    clear_admin_ctx(user_id)
    is_admin = user_id in ADMIN_IDS
    keyboard = build_main_menu(is_admin)
    await send_message_with_keyboard(
        user_id,
        "🏠 Главное меню.\nВыберите раздел:",
        keyboard
    )

async def show_contacts(user_id: int) -> None:
    """Показывает контакты салона: адрес, телефон, режим работы и карту с меткой."""
    from config import SALON_ADDRESS, SALON_PHONE, SALON_WORK_HOURS, SALON_LAT, SALON_LON
    text = (
        "📞 **Контакты салона**\n\n"
        f"📍 Адрес: {SALON_ADDRESS}\n"
        f"📞 Телефон: {SALON_PHONE}\n"
        f"🕒 Режим работы: {SALON_WORK_HOURS}\n\n"
        "Откройте профиль салона на Яндексе или поделитесь своим местоположением, "
        "чтобы мы показали, как до нас добраться."
    )
    keyboard = build_contacts_keyboard()
    await send_message_with_keyboard(user_id, text, keyboard)
    # Показываем метку салона на карте внутри чата (LocationAttachment).
    await send_location(user_id, SALON_LAT, SALON_LON)

async def send_location(user_id: int, lat: float, lon: float) -> bool:
    """Отправляет LocationAttachment с меткой по координатам (lat/lon — float).

    Формат MAX: координаты передаются на верхнем уровне вложения
    ({"type": "location", "latitude": ..., "longitude": ...}).
    """
    attachment = {
        "type": "location",
        "latitude": float(lat),
        "longitude": float(lon),
    }
    return await send_message(user_id, "📍 Наш салон находится здесь:", [attachment])

async def handle_user_location(user_id: int, lat: float, lon: float) -> None:
    """Отвечает на присланную пользователем геолокацию ссылкой на маршрут до салона."""
    from config import SALON_LAT, SALON_LON
    map_link = (
        f"https://yandex.ru/maps/?rtext=~{lat},{lon}~{SALON_LAT},{SALON_LON}"
        f"&ruri=~{SALON_LAT},{SALON_LON}"
    )
    await send_message(user_id, f"Спасибо! Вот маршрут до нашего салона:\n{map_link}")

async def handle_message(user_id: int, text: str) -> None:
    state = get_state(user_id)
    if state:
        await handle_fsm_state(user_id, text, state)
        return

    text_lower = text.lower()
    if text_lower in ("начать", "старт", "menu", "главное меню", "🏠 в меню"):
        await show_main_menu(user_id)
        return
    if text_lower in ("💇 услуги", "услуги", "📝 оставить заявку", "оставить заявку"):
        await show_services(user_id)
        return
    if text_lower in ("👤 профиль", "профиль"):
        await show_profile(user_id)
        return
    if text_lower in ("📍 контакты", "контакты", "контакты салона"):
        await show_contacts(user_id)
        return
    if text_lower in ("🛠 админ-панель", "админ-панель"):
        if user_id not in ADMIN_IDS:
            await send_message(user_id, "❌ У вас нет доступа к админ-панели.")
            return
        await show_admin_panel(user_id)
        return
    await send_message_with_keyboard(
        user_id,
        "❓ Команда не распознана. Нажмите кнопку «Начать».",
        build_start_button(),
    )

async def handle_fsm_state(user_id: int, text: str, state: str) -> None:
    if state.startswith("PROFILE_"):
        await handle_profile_fsm(user_id, text, state)
    elif state.startswith("BOOKING_"):
        await handle_booking_fsm(user_id, text, state)
    elif state.startswith("ADMIN_"):
        await handle_admin_fsm(user_id, text, state)
    else:
        delete_state(user_id)
        await send_message(user_id, "Состояние сброшено. Используйте меню.")

async def handle_callback(user_id: int, payload: Dict) -> None:
    logger.debug(f"Распарсенный payload: {payload}")
    cmd = payload.get("cmd")
    logger.debug(f"Команда: {cmd}")
    if not cmd:
        logger.warning(f"В payload нет 'cmd': {payload}")
        await send_message(user_id, "Ошибка: команда не распознана. Попробуйте снова.")
        return

    if cmd == "menu":
        await show_main_menu(user_id)
        return
    if cmd == "contacts":
        await show_contacts(user_id)
        return
    if cmd == "profile":
        await show_profile(user_id)
        return
    if cmd == "booking_cancel":
        delete_state(user_id)
        clear_booking_data(user_id)
        await send_message(user_id, "Запись отменена. Возврат в меню.")
        await show_main_menu(user_id)
        return
    if cmd == "noop":
        return

    if cmd == "profile_edit_phone":
        set_state(user_id, states.PROFILE_WAIT_PHONE)
        await send_message_with_keyboard(
            user_id,
            "Введите номер телефона в формате +7XXXXXXXXXX или 8XXXXXXXXXX:",
            build_profile_cancel(),
        )
        return
    if cmd == "profile_cancel":
        delete_state(user_id)
        await show_profile(user_id)
        return
    if cmd == "profile_delete_phone":
        await send_message_with_keyboard(
            user_id,
            "🗑 Удалить номер телефона из профиля?",
            build_profile_delete_confirm(),
        )
        return
    if cmd == "profile_delete_phone_confirm":
        async with async_session() as session:
            stmt = select(User).where(User.max_id == user_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.phone = None
                await session.commit()
        await send_message(user_id, "✅ Телефон удалён.")
        await show_profile(user_id)
        return

    if cmd == "menu_services":
        await show_services(user_id)
        return
    if cmd == "booking_services_page":
        page = payload.get("page", 0)
        await show_services(user_id, page)
        return
    if cmd == "service_select":
        service_id = payload.get("service_id")
        if service_id is None:
            return
        user = await get_user_or_create(user_id)
        if not user.phone:
            await send_message(
                user_id,
                "⚠️ Для записи нужно указать номер телефона.\n"
                "Телефон можно передать автоматически кнопкой «📱 Поделиться номером».",
            )
            await show_profile(user_id)
            return
        data = get_booking_data(user_id)
        data["service_id"] = service_id
        set_booking_data(user_id, data)
        await show_masters_for_booking(user_id)
        return

    if cmd == "master_select":
        master_id = payload.get("master_id")
        if master_id is None:
            return
        data = get_booking_data(user_id)
        if master_id == "any":
            data["master_id"] = None
        else:
            data["master_id"] = int(master_id)
        set_booking_data(user_id, data)
        await show_date_selection(user_id)
        return

    if cmd == "date_select":
        date_str = payload.get("date")
        if not date_str:
            return
        data = get_booking_data(user_id)
        data["desired_date"] = date_str
        set_booking_data(user_id, data)
        await show_hour_selection(user_id)
        return

    if cmd == "time_hour":
        hour = payload.get("hour")
        if hour is None:
            return
        data = get_booking_data(user_id)
        data["desired_hour"] = hour
        set_booking_data(user_id, data)
        await show_minute_selection(user_id)
        return
    if cmd == "time_hour_page":
        page = payload.get("page", 0)
        await show_hour_selection(user_id, page)
        return

    if cmd == "time_minute":
        minute = payload.get("minute")
        if minute is None:
            return
        data = get_booking_data(user_id)
        data["desired_minute"] = minute
        set_booking_data(user_id, data)
        await show_booking_confirm(user_id)
        return

    if cmd == "booking_confirm":
        await confirm_booking(user_id)
        return

    if cmd == "booking_history":
        await show_booking_history(user_id)
        return
    if cmd == "booking_history_filter":
        status = payload.get("status") or "all"
        await show_booking_history(user_id, status)
        return
    if cmd == "booking_history_page":
        status = payload.get("status") or "all"
        page = payload.get("page", 0)
        await show_booking_history(user_id, status, page)
        return
    if cmd == "booking_history_detail":
        booking_id = payload.get("booking_id")
        if booking_id is None:
            return
        await show_booking_detail(user_id, booking_id)
        return

    if cmd == "admin_menu":
        await show_admin_panel(user_id)
        return
    if cmd == "admin_upload_schedule":
        if user_id not in ADMIN_IDS:
            await send_message(user_id, "❌ У вас нет доступа.")
            return
        await upload_schedule(user_id)
        return
    if cmd == "admin_services":
        await show_admin_services(user_id)
        return
    if cmd == "admin_service_add":
        set_state(user_id, states.ADMIN_SVC_ADD_NAME)
        await send_message(user_id, "Введите название услуги:")
        return
    if cmd == "admin_service_edit":
        service_id = payload.get("service_id")
        if service_id is None:
            return
        await show_admin_service_edit(user_id, service_id)
        return
    if cmd == "admin_service_edit_field":
        service_id = payload.get("service_id")
        field = payload.get("field")
        if service_id is None or not field:
            return
        ctx = get_admin_ctx(user_id)
        ctx["service_id"] = service_id
        ctx["field"] = field
        set_admin_ctx(user_id, ctx)
        state_map = {
            "name": states.ADMIN_SVC_EDIT_NAME,
            "description": states.ADMIN_SVC_EDIT_DESC,
            "price": states.ADMIN_SVC_EDIT_PRICE,
            "duration": states.ADMIN_SVC_EDIT_DURATION,
        }
        set_state(user_id, state_map[field])
        prompts = {
            "name": "Введите новое название:",
            "description": "Введите новое описание (или '-' для пустого):",
            "price": "Введите новую цену (строка, например 'от 450'):",
            "duration": "Введите новую длительность (минуты, или '-' для пустого):",
        }
        await send_message(user_id, prompts[field])
        return
    if cmd == "admin_service_toggle":
        service_id = payload.get("service_id")
        if service_id is None:
            return
        await toggle_service(user_id, service_id)
        return

    if cmd == "admin_masters":
        await show_admin_masters(user_id)
        return
    if cmd == "admin_master_add":
        set_state(user_id, states.ADMIN_MASTER_ADD_NAME)
        await send_message(user_id, "Введите имя мастера:")
        return
    if cmd == "admin_master_edit":
        master_id = payload.get("master_id")
        if master_id is None:
            return
        await show_admin_master_edit(user_id, master_id)
        return
    if cmd == "admin_master_edit_field":
        master_id = payload.get("master_id")
        field = payload.get("field")
        if master_id is None or not field:
            return
        ctx = get_admin_ctx(user_id)
        ctx["master_id"] = master_id
        ctx["field"] = field
        set_admin_ctx(user_id, ctx)
        state_map = {
            "name": states.ADMIN_MASTER_EDIT_NAME,
            "specialization": states.ADMIN_MASTER_EDIT_SPEC,
        }
        set_state(user_id, state_map[field])
        prompts = {
            "name": "Введите новое имя:",
            "specialization": "Введите новую специализацию (или '-' для пустого):",
        }
        await send_message(user_id, prompts[field])
        return
    if cmd == "admin_master_toggle":
        master_id = payload.get("master_id")
        if master_id is None:
            return
        await toggle_master(user_id, master_id)
        return

    if cmd == "admin_bookings":
        await show_admin_bookings(user_id)
        return
    if cmd == "admin_bookings_filter":
        status = payload.get("status")
        if status:
            await show_admin_bookings(user_id, status)
        return
    if cmd == "admin_booking_view":
        booking_id = payload.get("booking_id")
        if booking_id is None:
            return
        await show_admin_booking_view(user_id, booking_id)
        return
    if cmd == "admin_booking_status":
        booking_id = payload.get("booking_id")
        status = payload.get("status")
        if booking_id is None or not status:
            return
        await change_booking_status(user_id, booking_id, status)
        return

    logger.warning(f"Неизвестная команда callback: {cmd}")

def _short_date(value: str) -> str:
    """Приводит дату YYYY-MM-DD к виду ДД.ММ."""
    if len(value) == 10:
        return f"{value[8:10]}.{value[5:7]}"
    return value


async def show_profile(user_id: int) -> None:
    user = await get_user_or_create(user_id)
    # Активные записи пользователя: новые и подтверждённые.
    async with async_session() as session:
        stmt = (
            select(Booking)
            .options(selectinload(Booking.service))
            .where(
                Booking.user_id == user.id,
                Booking.status.in_(["new", "confirmed"]),
            )
            .order_by(Booking.desired_date, Booking.desired_time)
        )
        res = await session.execute(stmt)
        active_bookings = list(res.scalars().all())

    text = f"👤 Профиль\n\nТелефон: {user.phone or 'не указан'}"
    if active_bookings:
        lines = []
        for i, b in enumerate(active_bookings[:5], 1):
            label = STATUS_LABELS.get(b.status, b.status)
            service_name = b.service.name if b.service else "Услуга"
            lines.append(
                f"{i}. {service_name} — {_short_date(b.desired_date)}, "
                f"{b.desired_time} ({label})"
            )
        if len(active_bookings) > 5:
            lines.append(f"…и ещё {len(active_bookings) - 5}")
        text += "\n\n📋 Активные записи:\n" + "\n".join(lines)
    else:
        text += "\n\n📋 Активных записей нет."

    keyboard = build_profile_menu(
        has_phone=bool(user.phone),
        has_active_bookings=bool(active_bookings),
    )
    await send_message_with_keyboard(user_id, text, keyboard)


HISTORY_PAGE_SIZE = 5  # записей на страницу истории (лимит MAX: ≤30 рядов кнопок)


async def show_booking_history(user_id: int, filter_status: str = "all", page: int = 0) -> None:
    """Показывает список записей пользователя с фильтрацией и пагинацией."""
    user = await get_user_or_create(user_id)
    stmt = (
        select(Booking)
        .options(selectinload(Booking.service))
        .where(Booking.user_id == user.id)
    )
    if filter_status != "all":
        stmt = stmt.where(Booking.status == filter_status)
    stmt = stmt.order_by(Booking.desired_date, Booking.desired_time)

    async with async_session() as session:
        res = await session.execute(stmt)
        all_bookings = list(res.scalars().all())

    if filter_status == "all":
        header = "📋 Все записи"
    else:
        header = f"📋 Записи: {STATUS_LABELS.get(filter_status, filter_status)}"

    total = len(all_bookings)
    total_pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE if total else 1
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    start = page * HISTORY_PAGE_SIZE
    page_bookings = all_bookings[start : start + HISTORY_PAGE_SIZE]

    if not all_bookings:
        text = header + "\n\nУ вас пока нет записей."
        keyboard = build_booking_history_list([], filter_status, HISTORY_STATUSES, 0, 1)
        await send_message_with_keyboard(user_id, text, keyboard)
        return

    booking_rows = []
    lines = []
    for b in page_bookings:
        service_name = b.service.name if b.service else "Услуга"
        label = STATUS_LABELS.get(b.status, b.status)
        row_label = f"#{b.id}: {service_name} — {b.desired_date} {b.desired_time} ({label})"
        booking_rows.append({"id": b.id, "label": row_label})
        lines.append(row_label)

    text = header + f"\n(страница {page + 1} из {total_pages})\n\n" + "\n".join(lines)
    keyboard = build_booking_history_list(
        booking_rows, filter_status, HISTORY_STATUSES, page, total_pages
    )
    await send_message_with_keyboard(user_id, text, keyboard)


async def show_booking_detail(user_id: int, booking_id: int) -> None:
    """Карточка записи из истории пользователя (просмотр без изменения статуса)."""
    async with async_session() as session:
        booking = await session.get(Booking, booking_id)
        if not booking:
            await send_message(user_id, "Запись не найдена.")
            return
        await session.refresh(booking, attribute_names=["user", "service", "master"])
        if booking.user.max_id != user_id:
            await send_message(user_id, "Запись не найдена.")
            return
        service = booking.service
        master = booking.master
        text = (
            f"📋 Запись #{booking.id}\n"
            f"Услуга: {service.name} — {service.price} ₽\n"
            f"Мастер: {master.name if master else 'Любой свободный'}\n"
            f"Дата: {booking.desired_date}\n"
            f"Время: {booking.desired_time}\n"
            f"Статус: {STATUS_LABELS.get(booking.status, booking.status)}"
        )
    keyboard = build_booking_history_detail()
    await send_message_with_keyboard(user_id, text, keyboard)


async def save_phone_from_profile(user_id: int, phone: str) -> None:
    """Сохраняет номер из переданного контакта (имя не сохраняем)."""
    async with async_session() as session:
        stmt = select(User).where(User.max_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if user is None:
            user = User(max_id=user_id)
            session.add(user)
        user.phone = phone
        await session.commit()
    delete_state(user_id)
    await send_message(user_id, f"✅ Телефон сохранён: {phone}")
    await show_profile(user_id)

async def handle_profile_fsm(user_id: int, text: str, state: str) -> None:
    if state == states.PROFILE_WAIT_PHONE:
        pattern = re.compile(r"^(\+7|7|8)\d{10}$")
        if not pattern.match(text):
            await send_message(user_id, "Неверный формат. Введите номер как +7XXXXXXXXXX или 8XXXXXXXXXX:")
            return
        if text.startswith("8"):
            phone = "+7" + text[1:]
        elif text.startswith("7"):
            phone = "+7" + text[1:]
        else:
            phone = text
        async with async_session() as session:
            stmt = select(User).where(User.max_id == user_id)
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user:
                user.phone = phone
                await session.commit()
        delete_state(user_id)
        await send_message(user_id, "✅ Телефон обновлён.")
        await show_profile(user_id)
        return

async def show_services(user_id: int, page: int = 0) -> None:
    async with async_session() as session:
        stmt = select(Service).where(Service.is_active == True).order_by(Service.id)
        res = await session.execute(stmt)
        services = res.scalars().all()
    total = len(services)
    page_size = 4
    total_pages = (total + page_size - 1) // page_size if total else 1
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    start = page * page_size
    end = min(start + page_size, total)
    page_services = services[start:end]
    service_dicts = [{"id": s.id, "name": s.name, "price": s.price} for s in page_services]
    keyboard = build_services_list(service_dicts, page, total_pages)
    if total == 0:
        text = "Услуги отсутствуют."
    else:
        lines = [f"• {s.name} — {s.price} ₽" for s in services]
        text = (
            "📝 Оставьте заявку\n\n"
            "Выберите услугу, на которую хотите записаться:\n"
            + "\n".join(lines)
        )
    await send_message_with_keyboard(user_id, text, keyboard)

async def show_masters_for_booking(user_id: int) -> None:
    """Показывает всех активных мастеров одной страницей, по алфавиту.

    «Любой свободный» добавляется первым в build_masters_list.
    Дубликаты имён (записи-дубли в БД) схлопываются: остаётся
    первый мастер с именем (минимальный id = канонический).
    """
    async with async_session() as session:
        stmt = (
            select(Master)
            .where(Master.is_active == True)  # noqa: E712
            .order_by(Master.name, Master.id)
        )
        res = await session.execute(stmt)
        masters = res.scalars().all()
    seen: set[str] = set()
    unique_masters = []
    for m in masters:
        key = m.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique_masters.append(m)
    master_dicts = [
        {"id": m.id, "name": m.name, "specialization": m.specialization}
        for m in unique_masters
    ]
    # Дополнительная сортировка без учёта регистра.
    master_dicts.sort(key=lambda x: x["name"].lower())
    keyboard = build_masters_list(master_dicts, show_any=True)
    set_state(user_id, states.BOOKING_WAIT_MASTER)
    await send_message_with_keyboard(user_id, "Выберите мастера:", keyboard)

async def show_date_selection(user_id: int) -> None:
    """Даты для записи: только рабочие дни выбранного мастера (или любые с рабочим мастером)."""
    data = get_booking_data(user_id)
    master_id = data.get("master_id")
    now = datetime.now(MSK)
    today = now.date()
    # Если рабочий день (до 20:00) ещё идёт, сегодня можно предлагать.
    start_date = today if now.hour < 20 else today + timedelta(days=1)

    async with async_session() as session:
        stmt = (
            select(MasterSchedule.work_date)
            .where(
                MasterSchedule.is_working == True,  # noqa: E712
                MasterSchedule.work_date >= start_date,
            )
            .distinct()
            .order_by(MasterSchedule.work_date)
            .limit(7)
        )
        if master_id is not None:
            stmt = stmt.where(MasterSchedule.master_id == master_id)
        res = await session.execute(stmt)
        work_dates = list(res.scalars().all())

    if not work_dates:
        await send_message(
            user_id,
            "😔 На ближайшее время нет рабочих дней"
            + (" у этого мастера" if master_id is not None else "")
            + ". Выберите другого мастера.",
        )
        await show_masters_for_booking(user_id)
        return

    weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    dates = []
    for d in work_dates:
        if d == today:
            label = d.strftime("%d.%m") + " (сегодня)"
        else:
            label = d.strftime("%d.%m") + f" ({weekdays[d.weekday()]})"
        dates.append({"label": label, "value": d.strftime("%Y-%m-%d")})
    keyboard = build_date_buttons(dates)
    set_state(user_id, states.BOOKING_WAIT_DATE)
    await send_message_with_keyboard(user_id, "Выберите дату:", keyboard)

def _slot_minutes() -> list[str]:
    """Все 30-минутные слоты рабочего дня: 09:00, 09:30, ..., 19:30."""
    return [f"{h:02d}:{m}" for h in range(9, 20) for m in ("00", "30")]


async def _free_slots_for(master_id: Optional[int], date_str: str) -> set[str]:
    """Свободные 30-минутные слоты на дату (занято = new/confirmed заявки).

    Для «Любой свободный» (master_id is None) слот свободен, если
    свободен хотя бы у одного активного мастера.
    """
    base = set(_slot_minutes())
    async with async_session() as session:
        if master_id is not None:
            stmt = select(Booking).where(
                Booking.master_id == master_id,
                Booking.desired_date == date_str,
                Booking.status.in_(["new", "confirmed"]),
            )
            res = await session.execute(stmt)
            busy = {b.desired_time for b in res.scalars().all()}
            return base - busy
        masters = (
            await session.execute(select(Master).where(Master.is_active == True))  # noqa: E712
        ).scalars().all()
        master_ids = [m.id for m in masters]
        if not master_ids:
            return set()
        stmt = select(Booking).where(
            Booking.master_id.in_(master_ids),
            Booking.desired_date == date_str,
            Booking.status.in_(["new", "confirmed"]),
        )
        res = await session.execute(stmt)
        busy_by_master: Dict[int, set] = {}
        for b in res.scalars().all():
            busy_by_master.setdefault(b.master_id, set()).add(b.desired_time)
        free: set[str] = set()
        for m in masters:
            busy = busy_by_master.get(m.id, set())
            for slot in base:
                if slot not in busy:
                    free.add(slot)
        return free


async def show_hour_selection(user_id: int, page: int = 0) -> None:
    data = get_booking_data(user_id)
    date_str = data.get("desired_date")
    master_id = data.get("master_id")
    if not date_str:
        await send_message(user_id, "Ошибка: дата не выбрана. Начните запись заново.")
        await show_main_menu(user_id)
        return

    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")

    free_slots = await _free_slots_for(master_id, date_str)

    if date_str == today:
        # Не предлагаем слоты раньше, чем через час (округление до 30 минут).
        min_time = now + timedelta(hours=1)
        if min_time.minute > 30:
            min_time = min_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        elif min_time.minute > 0:
            min_time = min_time.replace(minute=30, second=0, microsecond=0)
        else:
            min_time = min_time.replace(minute=0, second=0, microsecond=0)
        cutoff = min_time.strftime("%H:%M")
        free_slots = {s for s in free_slots if s >= cutoff}

    if not free_slots:
        await send_message(
            user_id,
            "😔 На выбранную дату нет свободных слотов. Выберите другую дату.",
        )
        await show_date_selection(user_id)
        return

    available_hours = sorted({int(s[:2]) for s in free_slots})
    hours = [f"{h:02d}" for h in available_hours]

    total = len(hours)
    page_size = 6
    total_pages = (total + page_size - 1) // page_size if total else 1
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    start = page * page_size
    end = min(start + page_size, total)
    page_hours = hours[start:end]

    keyboard = build_hour_buttons(page_hours, page, total_pages)
    set_state(user_id, states.BOOKING_WAIT_HOUR)
    await send_message_with_keyboard(user_id, "Выберите час:", keyboard)

async def show_minute_selection(user_id: int) -> None:
    data = get_booking_data(user_id)
    date_str = data.get("desired_date")
    hour_str = data.get("desired_hour")
    master_id = data.get("master_id")
    if not date_str or not hour_str:
        await send_message(user_id, "Ошибка: дата или час не выбраны. Начните запись заново.")
        await show_main_menu(user_id)
        return

    # Показываем только незанятые минуты выбранного часа.
    free_slots = await _free_slots_for(master_id, date_str)
    minutes = [
        s[3:] for s in sorted(free_slots) if s.startswith(hour_str + ":")
    ]
    if not minutes:
        await send_message(user_id, "😔 На этот час нет свободных слотов. Выберите другой час.")
        await show_hour_selection(user_id)
        return

    keyboard = build_minute_buttons(minutes)
    set_state(user_id, states.BOOKING_WAIT_MINUTE)
    await send_message_with_keyboard(user_id, "Выберите минуты:", keyboard)

async def show_booking_confirm(user_id: int) -> None:
    data = get_booking_data(user_id)
    service_id = data.get("service_id")
    master_id = data.get("master_id")
    desired_date = data.get("desired_date")
    desired_hour = data.get("desired_hour")
    desired_minute = data.get("desired_minute")
    if not all([service_id, desired_date, desired_hour, desired_minute]):
        await send_message(user_id, "Ошибка: не все данные для записи заполнены. Начните заново.")
        await show_main_menu(user_id)
        return

    async with async_session() as session:
        service = await session.get(Service, service_id)
        if master_id:
            master = await session.get(Master, master_id)
            master_name = master.name if master else "Неизвестный мастер"
        else:
            master_name = "Любой свободный"
        user = await get_user_or_create(user_id)
        text = f"✅ Подтверждение записи\n\nУслуга: {service.name} — {service.price} ₽\nМастер: {master_name}\nДата: {desired_date}\nВремя: {desired_hour}:{desired_minute}"
    keyboard = build_booking_confirm()
    set_state(user_id, states.BOOKING_WAIT_CONFIRM)
    await send_message_with_keyboard(user_id, text, keyboard)

async def confirm_booking(user_id: int) -> None:
    data = get_booking_data(user_id)
    service_id = data.get("service_id")
    master_id = data.get("master_id")
    desired_date = data.get("desired_date")
    desired_hour = data.get("desired_hour")
    desired_minute = data.get("desired_minute")
    if not all([service_id, desired_date, desired_hour, desired_minute]):
        await send_message(user_id, "Ошибка: не все данные для записи заполнены. Начните заново.")
        await show_main_menu(user_id)
        return

    async with async_session() as session:
        user = await get_user_or_create(user_id)
        booking = Booking(
            user_id=user.id,
            service_id=service_id,
            master_id=master_id,
            desired_date=desired_date,
            desired_time=f"{desired_hour}:{desired_minute}",
            status="new",
        )
        session.add(booking)
        await session.commit()
        await session.refresh(booking)

        service = await session.get(Service, service_id)
        master = None
        if master_id:
            master = await session.get(Master, master_id)

        admin_text = (
            f"📋 Новая заявка #{booking.id}\n"
            f"Клиент: {user.phone or 'телефон не указан'}\n"
            f"Услуга: {service.name} — {service.price} ₽\n"
            f"Мастер: {master.name if master else 'Любой свободный'}\n"
            f"Дата: {desired_date}\n"
            f"Время: {desired_hour}:{desired_minute}"
        )
        for admin_id in ADMIN_IDS:
            await send_message(admin_id, admin_text)

    delete_state(user_id)
    clear_booking_data(user_id)
    keyboard = build_post_booking()
    await send_message_with_keyboard(
        user_id,
        "✅ Заявка принята!\nМастер свяжется с вами для подтверждения записи.",
        keyboard,
    )

async def send_reminder(booking_id: int) -> None:
    """Отправляет клиенту напоминание о подтверждённой записи."""
    async with async_session() as session:
        booking = await session.get(Booking, booking_id)
        if not booking:
            logger.warning("Напоминание: заявка #%s не найдена", booking_id)
            return
        await session.refresh(booking, attribute_names=["user", "service", "master"])
        user = booking.user
        service = booking.service
        master = booking.master
        text = (
            "⏰ Напоминание о записи!\n\n"
            f"Услуга: {service.name} — {service.price} ₽\n"
            f"Мастер: {master.name if master else 'Любой свободный'}\n"
            f"Дата: {booking.desired_date}\n"
            f"Время: {booking.desired_time}\n\n"
            "Ждём вас!"
        )
        await send_message(user.max_id, text)
        logger.info("Отправлено напоминание для заявки #%s", booking.id)

async def reminder_loop() -> None:
    """Фоновая задача: раз в минуту шлёт напоминания за час до визита.

    Напоминание отправляется один раз для подтверждённых заявок
    (reminder_sent=True), в окне [now+55мин, now+65мин].
    """
    logger.info("Фоновая задача напоминаний запущена")
    while True:
        try:
            now = datetime.now(MSK)
            start_window = now + timedelta(minutes=55)
            end_window = now + timedelta(minutes=65)
            async with async_session() as session:
                stmt = select(Booking).where(
                    Booking.status == "confirmed",
                    Booking.reminder_sent == False,  # noqa: E712
                )
                res = await session.execute(stmt)
                for booking in res.scalars().all():
                    try:
                        booking_dt = datetime.strptime(
                            f"{booking.desired_date} {booking.desired_time}",
                            "%Y-%m-%d %H:%M",
                        ).replace(tzinfo=MSK)
                    except ValueError:
                        logger.warning(
                            "Напоминание: некорректные дата/время заявки #%s: %s %s",
                            booking.id, booking.desired_date, booking.desired_time,
                        )
                        continue
                    if start_window <= booking_dt <= end_window:
                        await send_reminder(booking.id)
                        booking.reminder_sent = True
                        await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка в reminder_loop: %s", exc)
        await asyncio.sleep(60)

async def show_admin_panel(user_id: int) -> None:
    if user_id not in ADMIN_IDS:
        await send_message(user_id, "❌ У вас нет доступа.")
        return
    delete_state(user_id)
    clear_admin_ctx(user_id)
    keyboard = build_admin_menu()
    await send_message_with_keyboard(user_id, "🛠 Админ-панель", keyboard)


async def upload_schedule(user_id: int, file_path: Optional[str] = None) -> None:
    """Загружает график работы мастеров из Excel-файла (.xlsx).

    По умолчанию используется SCHEDULE_FILE_PATH из конфига. Чтение и запись
    в БД выполняет handlers.admin.schedule.import_schedule_from_excel
    (на базе openpyxl); здесь — проверки, импорт библиотеки и обработка ошибок.
    """
    file_path = file_path or SCHEDULE_FILE_PATH
    try:
        import openpyxl  # noqa: F401 — нужна для чтения .xlsx
    except ImportError:
        await send_message(
            user_id,
            "❌ Библиотека openpyxl не установлена. Установите её: pip install openpyxl",
        )
        return
    if not os.path.exists(file_path):
        await send_message(
            user_id,
            f"❌ Файл графика не найден: {file_path}\n"
            "Положите .xlsx рядом с ботом или задайте SCHEDULE_FILE_PATH.",
        )
        return
    try:
        from handlers.admin.schedule import import_schedule_from_excel

        report = await import_schedule_from_excel(file_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка импорта графика: %s", exc)
        await send_message(user_id, f"❌ Ошибка импорта графика: {exc}")
        return
    text = (
        "✅ График загружен!\n"
        f"Мастеров в БД: {report['masters']}\n"
        f"Дат в файле: {report['dates']}\n"
        f"Рабочих дней (X): {report['working_days']}\n"
        f"Строк вставлено: {report['inserted']}"
    )
    if report.get("skipped_masters"):
        text += "\n⚠️ Не найдены в БД: " + ", ".join(report["skipped_masters"])
    await send_message(user_id, text)


async def show_admin_services(user_id: int) -> None:
    async with async_session() as session:
        stmt = select(Service).order_by(Service.id)
        res = await session.execute(stmt)
        services = res.scalars().all()
    service_dicts = [{"id": s.id, "name": s.name, "price": s.price, "is_active": s.is_active} for s in services]
    keyboard = build_admin_services_list(service_dicts)
    await send_message_with_keyboard(user_id, "📋 Услуги (админ):", keyboard)

async def show_admin_service_edit(user_id: int, service_id: int) -> None:
    async with async_session() as session:
        service = await session.get(Service, service_id)
        if not service:
            await send_message(user_id, "Услуга не найдена.")
            return
        service_dict = {"id": service.id, "name": service.name, "description": service.description, "price": service.price, "duration": service.duration_min, "is_active": service.is_active}
        text = (
            f"📝 Редактирование услуги #{service.id}\n"
            f"Название: {service.name}\n"
            f"Описание: {service.description or '-'}\n"
            f"Цена: {service.price}\n"
            f"Длительность: {service.duration_min or '-'} мин\n"
            f"Статус: {'🟢 Активна' if service.is_active else '🔴 Скрыта'}"
        )
        keyboard = build_admin_service_edit(service_dict)
        await send_message_with_keyboard(user_id, text, keyboard)

async def toggle_service(user_id: int, service_id: int) -> None:
    async with async_session() as session:
        service = await session.get(Service, service_id)
        if not service:
            await send_message(user_id, "Услуга не найдена.")
            return
        service.is_active = not service.is_active
        await session.commit()
    await send_message(user_id, f"✅ Услуга {'скрыта' if not service.is_active else 'показана'}.")
    await show_admin_services(user_id)

async def show_admin_masters(user_id: int) -> None:
    async with async_session() as session:
        stmt = select(Master).order_by(Master.id)
        res = await session.execute(stmt)
        masters = res.scalars().all()
    master_dicts = [{"id": m.id, "name": m.name, "specialization": m.specialization, "is_active": m.is_active} for m in masters]
    keyboard = build_admin_masters_list(master_dicts)
    await send_message_with_keyboard(user_id, "👨‍🎨 Мастера (админ):", keyboard)

async def show_admin_master_edit(user_id: int, master_id: int) -> None:
    async with async_session() as session:
        master = await session.get(Master, master_id)
        if not master:
            await send_message(user_id, "Мастер не найден.")
            return
        master_dict = {"id": master.id, "name": master.name, "specialization": master.specialization, "is_active": master.is_active}
        text = (
            f"📝 Редактирование мастера #{master.id}\n"
            f"Имя: {master.name}\n"
            f"Специализация: {master.specialization or '-'}\n"
            f"Статус: {'🟢 Активен' if master.is_active else '🔴 Скрыт'}"
        )
        keyboard = build_admin_master_edit(master_dict)
        await send_message_with_keyboard(user_id, text, keyboard)

async def toggle_master(user_id: int, master_id: int) -> None:
    async with async_session() as session:
        master = await session.get(Master, master_id)
        if not master:
            await send_message(user_id, "Мастер не найден.")
            return
        master.is_active = not master.is_active
        await session.commit()
    await send_message(user_id, f"✅ Мастер {'скрыт' if not master.is_active else 'показан'}.")
    await show_admin_masters(user_id)

async def show_admin_bookings(user_id: int, status_filter: str = "all") -> None:
    async with async_session() as session:
        query = (
            select(Booking)
            .options(
                selectinload(Booking.user),
                selectinload(Booking.service),
                selectinload(Booking.master),
            )
            .order_by(Booking.id.desc())
        )
        if status_filter != "all":
            query = query.where(Booking.status == status_filter)
        res = await session.execute(query)
        bookings = res.scalars().all()
        # Связанные объекты уже загружены через selectinload, ленивой загрузки нет.
        # Имя клиента не храним/не показываем — только телефон (минимизация ПДн).
        booking_dicts = []
        for b in bookings:
            client = b.user.phone or f"ID {b.user.max_id}"
            booking_dicts.append({
                "id": b.id,
                "user_name": client,
                "service_name": b.service.name,
                "status": b.status,
            })
    statuses = ["all", "new", "confirmed", "declined", "done"]
    keyboard = build_admin_bookings_list(booking_dicts, statuses, status_filter)
    await send_message_with_keyboard(user_id, "📅 Заявки:", keyboard)

async def show_admin_booking_view(user_id: int, booking_id: int) -> None:
    async with async_session() as session:
        booking = await session.get(Booking, booking_id)
        if not booking:
            await send_message(user_id, "Заявка не найдена.")
            return
        await session.refresh(booking, attribute_names=["user", "service", "master"])
        user = booking.user
        service = booking.service
        master = booking.master
        text = (
            f"📋 Заявка #{booking.id}\n"
            f"Клиент: {user.phone or 'телефон не указан'}\n"
            f"Услуга: {service.name} — {service.price} ₽\n"
            f"Мастер: {master.name if master else 'Любой свободный'}\n"
            f"Дата: {booking.desired_date}\n"
            f"Время: {booking.desired_time}\n"
            f"Статус: {booking.status}"
        )
        booking_dict = {
            "id": booking.id,
            "status": booking.status,
        }
        keyboard = build_admin_booking_view(booking_dict)
        await send_message_with_keyboard(user_id, text, keyboard)

async def change_booking_status(user_id: int, booking_id: int, new_status: str) -> None:
    async with async_session() as session:
        booking = await session.get(Booking, booking_id)
        if not booking:
            await send_message(user_id, "Заявка не найдена.")
            return
        booking.status = new_status
        await session.commit()
        await session.refresh(booking, attribute_names=["user", "service", "master"])
        user = booking.user
        service = booking.service
        label = STATUS_LABELS.get(new_status, new_status)
        service_name = service.name if service else "Услуга"
        await send_message(
            user.max_id,
            f"📞 Статус вашей заявки на «{service_name}» "
            f"({booking.desired_date}, {booking.desired_time}) изменён: {label}",
        )
        await send_message(user_id, f"✅ Статус заявки #{booking.id} изменён на {label}")
    await show_admin_bookings(user_id, "all")

async def handle_admin_fsm(user_id: int, text: str, state: str) -> None:
    if state == states.ADMIN_SVC_ADD_NAME:
        if len(text) < 2 or len(text) > 100:
            await send_message(user_id, "Название должно быть от 2 до 100 символов. Попробуйте снова:")
            return
        ctx = get_admin_ctx(user_id)
        ctx["name"] = text
        set_admin_ctx(user_id, ctx)
        set_state(user_id, states.ADMIN_SVC_ADD_DESC)
        await send_message(user_id, "Введите описание услуги (или '-' для пустого):")
        return

    if state == states.ADMIN_SVC_ADD_DESC:
        ctx = get_admin_ctx(user_id)
        ctx["description"] = text if text != "-" else None
        set_admin_ctx(user_id, ctx)
        set_state(user_id, states.ADMIN_SVC_ADD_PRICE)
        await send_message(user_id, "Введите цену (строка, например '450' или 'от 450'):")
        return

    if state == states.ADMIN_SVC_ADD_PRICE:
        if len(text) < 1 or len(text) > 50:
            await send_message(user_id, "Цена должна быть строкой до 50 символов. Попробуйте снова:")
            return
        ctx = get_admin_ctx(user_id)
        ctx["price"] = text
        set_admin_ctx(user_id, ctx)
        set_state(user_id, states.ADMIN_SVC_ADD_DURATION)
        await send_message(user_id, "Введите длительность в минутах (или '-' для пустого):")
        return

    if state == states.ADMIN_SVC_ADD_DURATION:
        ctx = get_admin_ctx(user_id)
        if text != "-":
            try:
                duration = int(text)
                if duration <= 0:
                    raise ValueError
                ctx["duration"] = duration
            except ValueError:
                await send_message(user_id, "Длительность должна быть положительным целым числом. Попробуйте снова:")
                return
        else:
            ctx["duration"] = None
        set_admin_ctx(user_id, ctx)
        async with async_session() as session:
            service = Service(
                name=ctx["name"],
                description=ctx.get("description"),
                price=ctx["price"],
                duration_min=ctx.get("duration"),
                is_active=True,
            )
            session.add(service)
            await session.commit()
        delete_state(user_id)
        clear_admin_ctx(user_id)
        await send_message(user_id, "✅ Услуга добавлена.")
        await show_admin_services(user_id)
        return

    if state in (states.ADMIN_SVC_EDIT_NAME, states.ADMIN_SVC_EDIT_DESC, states.ADMIN_SVC_EDIT_PRICE, states.ADMIN_SVC_EDIT_DURATION):
        ctx = get_admin_ctx(user_id)
        service_id = ctx.get("service_id")
        field = ctx.get("field")
        if not service_id or not field:
            delete_state(user_id)
            clear_admin_ctx(user_id)
            await send_message(user_id, "Ошибка контекста. Попробуйте снова.")
            await show_admin_services(user_id)
            return
        if field == "name":
            if len(text) < 2 or len(text) > 100:
                await send_message(user_id, "Имя должно быть от 2 до 100 символов. Попробуйте снова:")
                return
            value = text
        elif field == "description":
            value = text if text != "-" else None
        elif field == "price":
            if len(text) < 1 or len(text) > 50:
                await send_message(user_id, "Цена должна быть строкой до 50 символов. Попробуйте снова:")
                return
            value = text
        elif field == "duration":
            if text != "-":
                try:
                    duration = int(text)
                    if duration <= 0:
                        raise ValueError
                    value = duration
                except ValueError:
                    await send_message(user_id, "Длительность должна быть положительным целым числом. Попробуйте снова:")
                    return
            else:
                value = None
        else:
            delete_state(user_id)
            clear_admin_ctx(user_id)
            await send_message(user_id, "Неизвестное поле.")
            await show_admin_services(user_id)
            return
        async with async_session() as session:
            service = await session.get(Service, service_id)
            if not service:
                await send_message(user_id, "Услуга не найдена.")
                delete_state(user_id)
                clear_admin_ctx(user_id)
                await show_admin_services(user_id)
                return
            model_field = "duration_min" if field == "duration" else field
            setattr(service, model_field, value)
            await session.commit()
        delete_state(user_id)
        clear_admin_ctx(user_id)
        await send_message(user_id, f"✅ Поле '{field}' обновлено.")
        await show_admin_service_edit(user_id, service_id)
        return

    if state == states.ADMIN_MASTER_ADD_NAME:
        if len(text) < 2 or len(text) > 100:
            await send_message(user_id, "Имя должно быть от 2 до 100 символов. Попробуйте снова:")
            return
        ctx = get_admin_ctx(user_id)
        ctx["name"] = text
        set_admin_ctx(user_id, ctx)
        set_state(user_id, states.ADMIN_MASTER_ADD_SPEC)
        await send_message(user_id, "Введите специализацию (или '-' для пустого):")
        return

    if state == states.ADMIN_MASTER_ADD_SPEC:
        ctx = get_admin_ctx(user_id)
        specialization = text if text != "-" else None
        async with async_session() as session:
            master = Master(
                name=ctx["name"],
                specialization=specialization,
                is_active=True,
            )
            session.add(master)
            await session.commit()
        delete_state(user_id)
        clear_admin_ctx(user_id)
        await send_message(user_id, "✅ Мастер добавлен.")
        await show_admin_masters(user_id)
        return

    if state in (states.ADMIN_MASTER_EDIT_NAME, states.ADMIN_MASTER_EDIT_SPEC):
        ctx = get_admin_ctx(user_id)
        master_id = ctx.get("master_id")
        field = ctx.get("field")
        if not master_id or not field:
            delete_state(user_id)
            clear_admin_ctx(user_id)
            await send_message(user_id, "Ошибка контекста. Попробуйте снова.")
            await show_admin_masters(user_id)
            return
        if field == "name":
            if len(text) < 2 or len(text) > 100:
                await send_message(user_id, "Имя должно быть от 2 до 100 символов. Попробуйте снова:")
                return
            value = text
        elif field == "specialization":
            value = text if text != "-" else None
        else:
            delete_state(user_id)
            clear_admin_ctx(user_id)
            await send_message(user_id, "Неизвестное поле.")
            await show_admin_masters(user_id)
            return
        async with async_session() as session:
            master = await session.get(Master, master_id)
            if not master:
                await send_message(user_id, "Мастер не найден.")
                delete_state(user_id)
                clear_admin_ctx(user_id)
                await show_admin_masters(user_id)
                return
            setattr(master, field, value)
            await session.commit()
        delete_state(user_id)
        clear_admin_ctx(user_id)
        await send_message(user_id, f"✅ Поле '{field}' обновлено.")
        await show_admin_master_edit(user_id, master_id)
        return

    delete_state(user_id)
    clear_admin_ctx(user_id)
    await send_message(user_id, "Неизвестное состояние админки. Возврат в меню.")
    await show_admin_panel(user_id)

async def handle_booking_fsm(user_id: int, text: str, state: str) -> None:
    await send_message(user_id, "Пожалуйста, используйте кнопки для выбора.")
