# Базовый образ с CUDA для ускорения на видеокарте
FROM nvidia/cuda:12.1.0-devel-ubuntu22.04

LABEL org.opencontainers.image.title="Margin" \
      org.opencontainers.image.description="Handwritten text recognition service: llama.cpp + Qwen2.5-VL" \
      org.opencontainers.image.licenses="MIT"

# Отключаем интерактивные запросы при установке пакетов
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3-pip \
    python3-dev \
    git \
    cmake \
    build-essential \
    ninja-build \
    libopencv-dev \
    python3-opencv \
    wget \
    curl \
    ca-certificates \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN ln -sf /usr/bin/python3.11 /usr/bin/python

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /app

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Актуальный cmake (3.22 из дистрибутива не знает нужных CUDA_ARCHITECTURES)
RUN pip install --no-cache-dir cmake --upgrade

# Сборка llama.cpp с CUDA. Нужны llama-mtmd-cli и llama-server.
RUN git clone https://github.com/ggerganov/llama.cpp /tmp/llama.cpp && \
    cd /tmp/llama.cpp && \
    mkdir -p build && cd build && \
    cmake .. \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="60;70;75;80;86;89;90" \
        -DCMAKE_BUILD_TYPE=Release \
        -GNinja && \
    cmake --build . --config Release -j$(nproc) && \
    mkdir -p /app/llama.cpp/build/bin && \
    cp bin/* /app/llama.cpp/build/bin/ && \
    find . -name "*.so*" -exec cp {} /app/llama.cpp/build/bin/ \; && \
    cd / && rm -rf /tmp/llama.cpp

ENV LD_LIBRARY_PATH=/app/llama.cpp/build/bin:$LD_LIBRARY_PATH

# Каталоги рантайма создаются здесь, а не при первом запуске: контейнер
# работает под непривилегированным пользователем и создать каталог в /app сам
# уже не сможет, а без каталога логов сервис останется без журнала.
RUN mkdir -p /app/uploads /app/models /app/logs /app/logs/raw \
             /app/data /app/data/dictionaries /app/data/in /app/data/out && \
    chmod -R 755 /app

# Код приложения. src/ — сервис и пайплайн, tools/ — папочная обвязка,
# config/ — настройки и схемы, data/dictionaries — словари, docs/ —
# документация (нужна тому, кто зашёл в контейнер разбираться).
COPY src/ src/
COPY tools/ tools/
COPY config/ config/
COPY data/dictionaries/ data/dictionaries/
COPY docs/ docs/
COPY README.md ./

# Непривилегированный пользователь. uid 1000 совпадает с типичным первым
# пользователем хоста: смонтированные тома logs/ и data/ остаются доступными
# на запись без chmod 777.
RUN useradd -m -u 1000 -s /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

# Пути рантайма. Каждый переопределяется при запуске: журналы, словари и
# схемы монтируются с хоста, и код не должен знать про точки монтирования.
ENV LOG_DIR=/app/logs \
    DICT_DIR=/app/data/dictionaries \
    SCHEMA_DIR=/app/config/schemas \
    RESULTS_DB=/app/data/results.db

# Порт по умолчанию; фактический берётся из SERVER_PORT.
EXPOSE 5002

# Проба живости идёт на фактический порт: при SERVER_PORT != 5002 проба по
# захардкоженному порту всегда падала бы, и контейнер уходил в вечный рестарт.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f "http://localhost:${SERVER_PORT:-5002}/api/health" || exit 1

CMD ["python", "src/app.py"]
