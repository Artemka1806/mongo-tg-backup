FROM python:3.11-slim

# Set the timezone to UTC
ENV TZ=Etc/UTC

# Встановлення mongodb-database-tools та tzdata
RUN apt-get update && \
    apt-get install -y wget gnupg tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    wget -qO - https://www.mongodb.org/static/pgp/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" | tee /etc/apt/sources.list.d/mongodb-org-7.0.list && \
    apt-get update && \
    apt-get install -y mongodb-database-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Створення робочої директорії
WORKDIR /app

# Копіювання файлів
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY login.py .

# Створення директорії для бекапів
RUN mkdir -p /app/backups

# Створення директорії для session файлів Pyrogram
RUN mkdir -p /app/sessions

VOLUME ["/app/backups", "/app/sessions"]

# Запуск додатку
CMD ["python", "-u", "main.py"]