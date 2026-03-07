FROM python:3.11-slim

ENV TZ=Etc/UTC

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
        tzdata \
        libkrb5-3 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && wget -q https://fastdl.mongodb.org/tools/db/mongodb-database-tools-debian12-x86_64-100.9.4.tgz \
    && tar -xzf mongodb-database-tools-debian12-x86_64-100.9.4.tgz \
    && mv mongodb-database-tools-*/bin/* /usr/local/bin/ \
    && rm -rf mongodb-database-tools* \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY login.py .

RUN mkdir -p /app/backups /app/sessions

VOLUME ["/app/backups", "/app/sessions"]

CMD ["python", "-u", "main.py"]