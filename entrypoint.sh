#!/bin/sh
# 生成 cron（默认每天 06:00 全量 + 10:30 增量补跑），启动 crond 常驻
CRON_SCHEDULE="${CRON_SCHEDULE:-0 6 * * *|30 10 * * *}"
i=0
: > /etc/cron.d/checkin
echo 'PATH=/usr/local/bin:/usr/bin:/bin' >> /etc/cron.d/checkin
echo "TZ=${TZ:-Asia/Shanghai}" >> /etc/cron.d/checkin
echo "$CRON_SCHEDULE" | tr '|' '\n' | while read -r expr; do
  [ -n "$expr" ] || continue
  echo "$expr root python3 /app/checkin.py --today --notify >> /dev/stdout 2>&1" >> /etc/cron.d/checkin
done
chmod 0644 /etc/cron.d/checkin
echo "▶ newapi-checkin 定时任务："
cat /etc/cron.d/checkin
exec crond -f
