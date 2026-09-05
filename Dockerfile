FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Создаём папку для данных (постоянное хранилище Bothost)
RUN mkdir -p /app/data && chmod 777 /app/data

# Переменные окружения
ENV PYTHONUNBUFFERED=1
ENV DATABASE_URL=sqlite+aiosqlite:///app/data/bot.db
ENV PORT=8080

EXPOSE 8080

# Запуск бота
CMD ["python", "bot.py"]
