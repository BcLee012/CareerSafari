# CareerSafari 生产镜像
# 构建：docker build -t careersafari .
# 运行：docker run -p 8000:8000 -e ADMIN_TOKEN=xxx -v careersafari-data:/app/data careersafari
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CAREERSAFARI_DB=/app/data/careersafari.db

WORKDIR /app

# 先只拷贝依赖清单，让 pip 层能被缓存复用
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY prompts ./prompts
COPY collects ./collects
COPY load_collects.py backfill_deadlines.py ./

# 以非 root 用户运行；/app/data 作为 SQLite 持久化目录
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"

# 说明：
# - SQLite 单文件库，默认单 worker。要提升并发请先把存储换成 PostgreSQL，
#   否则多 worker 只会让写操作互相等锁，而且进程内限流会变成「每 worker 一份配额」。
# - --proxy-headers + --forwarded-allow-ips 让限流能拿到真实客户端 IP。
#   仅当本服务确实跑在可信反向代理（Nginx / Caddy / 云负载均衡）后面时才这样用；
#   直接暴露到公网时请删掉这两个参数，否则客户端可以伪造 X-Forwarded-For 绕过限流。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
