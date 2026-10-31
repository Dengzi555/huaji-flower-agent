# 容器只打包运行代码与合成数据，不复制本机密钥、运行记录或 SQLite 数据库。
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY ecommerce_agent ./ecommerce_agent
COPY data ./data
# 使用普通用户执行，持久化任务和产物单独放在 /state 卷中。
RUN useradd --uid 10001 --create-home appuser && mkdir -p /state/runs /state/database && chown -R appuser:appuser /state
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "ecommerce_agent", "web", "--host", "0.0.0.0", "--state-dir", "/state/database", "--output", "/state/runs"]
