FROM python:3.12-slim

# Установка часовых поясов и системных пакетов
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копирование зависимостей и установка
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода приложения
COPY . .

# Переменные окружения по умолчанию
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Europe/Moscow

# Точка входа по умолчанию запускает оба процесса через run.py
CMD ["python", "run.py"]
