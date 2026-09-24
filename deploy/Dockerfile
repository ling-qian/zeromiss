# BizBot AI 店长 - 生产部署镜像
# 构建后在服务器常驻运行（沙箱 20 分钟超时的限制不适用于客户服务器）
FROM python:3.13-slim

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据（SQLite/知识库/游标）与日志持久化
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1

CMD ["python3", "app.py", "--host", "0.0.0.0", "--port", "8080"]
