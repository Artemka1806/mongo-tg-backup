FROM python:3.11-slim

# timezone
ENV TZ=Etc/UTC

# install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        mongodb-database-tools \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# working directory
WORKDIR /app

# install python dependencies first (better docker cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy project files
COPY main.py .
COPY login.py .

# create directories
RUN mkdir -p /app/backups /app/sessions

# volumes
VOLUME ["/app/backups", "/app/sessions"]

# run app
CMD ["python", "-u", "main.py"]