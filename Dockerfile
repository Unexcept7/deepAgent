FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Neo4j 驱动按需安装；镜像默认包含它，未配置 Neo4j 时不会发起连接。
RUN pip install --no-cache-dir -r requirements-optional.txt

COPY app ./app
COPY docs ./docs
COPY .env.example ./.env.example

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
