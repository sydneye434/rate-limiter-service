# Rate limiter service – production image. Developed by Sydney Edwards.
FROM python:3.12-alpine3.20

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/appuser/.local/bin:${PATH}"

WORKDIR /app

# Create unprivileged user
RUN addgroup -S appuser && adduser -S appuser -G appuser

COPY requirements.txt .

# Keep image minimal: no OS build toolchain, install wheels only where possible
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


