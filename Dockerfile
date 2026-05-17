FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc-dev \
    libffi-dev \
    libjpeg-dev \
    zlib1g-dev \
    libtiff6 \
    libopenjp2-7 \
    libwebp-dev \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --no-deps \
    --extra-index-url https://www.piwheels.org/simple \
    fastapi==0.115.0 \
    uvicorn==0.29.0 \
    httpx==0.27.0 \
    python-multipart==0.0.9 \
    jinja2==3.1.4 \
    reportlab==4.2.0 \
    python-dateutil==2.9.0 \
    itsdangerous==2.2.0 \
    starlette==0.37.2 \
    python-dotenv==1.0.1 \
    pydantic==2.7.0 \
    pydantic-core==2.18.1 \
    annotated-types==0.7.0 \
    typing-extensions==4.11.0 \
    anyio==4.3.0 \
    sniffio==1.3.1 \
    h11==0.14.0 \
    httpcore==1.0.5 \
    certifi==2024.2.2 \
    idna==3.7 \
    click==8.1.7 \
    MarkupSafe==2.1.5 \
    pillow==10.3.0 \
    chardet==5.2.0 \
    six==1.16.0

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--loop", "asyncio"]
