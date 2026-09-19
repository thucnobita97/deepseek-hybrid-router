# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY router/ ./router/
COPY config/ ./config/

EXPOSE 8001

CMD ["uvicorn", "router.main:app", "--host", "0.0.0.0", "--port", "8001"]
