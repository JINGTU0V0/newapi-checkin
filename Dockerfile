FROM python:3.11-slim

# 时区对齐（签到码/账本按本地日期分界）
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY checkin.py ./
RUN pip install --no-cache-dir requests

# 状态/账本落在挂载卷里，容器重建不丢当天进度
ENV CHECKIN_STATE=/app/data/.checkin_state.json \
    CHECKIN_LEDGER=/app/data/.checkin_ledger.json

# 配置以卷挂载：/app/sites.yaml /app/creds.json
# 定时条目写进 /etc/cron.d/checkin（见 entrypoint）
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
