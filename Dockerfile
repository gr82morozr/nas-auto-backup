FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg rsync file qpdf unzip && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir xlrd==2.0.2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY apps_common ./apps_common
COPY apps/auto-backup/auto-backup.py apps/auto-backup/control-service.py apps/auto-backup/supervisor.py ./
COPY apps/auto-backup/config.json apps/auto-backup/overview.html ./
COPY apps/auto-backup/backup_store.py apps/auto-backup/backup_worker.py apps/auto-backup/backup_report.py ./
RUN mkdir -p /tmp/temp /tmp/runtime /tmp/output /tmp/shared /persistent /temp/log
EXPOSE 8000
CMD ["python", "/app/apps_common/container_log_entrypoint.py", "--name", "auto-backup", "--", "python", "/app/supervisor.py"]
