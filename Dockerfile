# ---------- builder: must match the distroless Python (3.13 on Debian 13) ----------
FROM python:3.13-slim-trixie AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

COPY requirements.txt .
RUN pip install --no-deps --target /app/deps -r requirements.txt

COPY controller.py /app/controller.py
# precompile: the runtime FS is read-only and PYTHONDONTWRITEBYTECODE=1
RUN python -m compileall -q /app

# ---------- runtime: distroless Python 3.13, nonroot ----------
FROM gcr.io/distroless/python3-debian13:nonroot
ENV PYTHONPATH=/app/deps \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /app /app
WORKDIR /app
USER 65532:65532
ENTRYPOINT ["/usr/bin/python3", "/app/controller.py"]
