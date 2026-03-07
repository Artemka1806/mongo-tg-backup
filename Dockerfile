FROM python:3.11-slim

ENV TZ=Etc/UTC

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        gnupg \
        ca-certificates \
        tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    wget -qO - https://pgp.mongodb.com/server-7.0.asc | \
        gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg && \
    echo "deb [ arch=amd64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian bullseye/mongodb-org/7.0 main" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list && \
    apt-get update && \
    apt-get install -y mongodb-database-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY login.py .

RUN mkdir -p /app/backups /app/sessions

VOLUME ["/app/backups", "/app/sessions"]

CMD ["python", "-u", "main.py"]