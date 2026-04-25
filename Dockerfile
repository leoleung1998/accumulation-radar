FROM python:3.11-slim

RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Write cron schedules
RUN echo "0 10 * * * root cd /app && /usr/local/bin/python3 accumulation_radar.py pool >> /app/pool.log 2>&1" > /etc/cron.d/radar && \
    echo "30 * * * * root cd /app && /usr/local/bin/python3 accumulation_radar.py oi >> /app/oi.log 2>&1" >> /etc/cron.d/radar && \
    chmod 0644 /etc/cron.d/radar && \
    crontab /etc/cron.d/radar

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
