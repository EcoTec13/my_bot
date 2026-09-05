"""
Конфигурация бота MAX. Значения берутся из переменных окружения
(на Bothost — из панели), при локальном запуске — из .env через python-dotenv.
"""

import os
from datetime import timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# MAX_TOKEN — основной; BOT_TOKEN — для совместимости с панелями хостинга.
MAX_TOKEN = os.getenv("MAX_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_MAX_IDS = os.getenv("ADMIN_MAX_IDS")

if not MAX_TOKEN:
    raise RuntimeError("MAX_TOKEN (или BOT_TOKEN) не задан в окружении")
if not ADMIN_MAX_IDS:
    raise RuntimeError("ADMIN_MAX_IDS не задан в окружении")

ADMIN_IDS = [int(x.strip()) for x in ADMIN_MAX_IDS.split(",") if x.strip()]
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_MAX_IDS должен содержать хотя бы один ID")

API_BASE_URL = "https://platform-api2.max.ru"
MSK = timezone(timedelta(hours=3))

# Информация о салоне (раздел «Контакты»)
SALON_PHONE = os.getenv("SALON_PHONE", "+79272769035")
SALON_ADDRESS = os.getenv("SALON_ADDRESS", "г. Саранск, ул. Полежаева, д. 117")
SALON_WORK_HOURS = os.getenv("SALON_WORK_HOURS", "ежедневно с 09:00 до 20:00")
SALON_LAT = float(os.getenv("SALON_LAT", "54.190030"))
SALON_LON = float(os.getenv("SALON_LON", "45.173284"))
SALON_MAP_URL = os.getenv(
    "SALON_MAP_URL", "https://yandex.ru/maps/?pt=45.173284,54.190030&z=17"
)
SALON_PROFILE_URL = os.getenv("SALON_PROFILE_URL", "https://yandex.ru/profile/1707544358")

# Файл с графиком работы мастеров (импорт через админ-панель).
SCHEDULE_FILE_PATH = os.getenv(
    "SCHEDULE_FILE_PATH", "График_работы_мастеров_сентябрь_2026.xlsx"
)