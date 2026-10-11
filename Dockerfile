# Runtime image for the console and the OpenAPI service.
# Multi-stage keeps build tooling out of the image: the wheel is built once, then installed alone.
FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="EnterpriseFlow" \
      org.opencontainers.image.description="Enterprise workflow agent orchestration with bounded planning, human approval and transactional commit" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENTERPRISEFLOW_DB=/app/runs/enterprise.sqlite \
    ENTERPRISEFLOW_MODE=fixture

COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt \
    && python -m pip install --no-cache-dir fastapi 'uvicorn[standard]' \
    && useradd --system --create-home --uid 10001 enterpriseflow \
    && mkdir -p /app/runs && chown -R enterpriseflow /app

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

USER enterpriseflow
EXPOSE 7861 8000
VOLUME ["/app/runs"]

# Default: the console on 7861. Override the command to run serve-api on 8000 instead.
CMD ["python", "-m", "enterprise_flow", "serve", "--host", "0.0.0.0", "--port", "7861", "--mode", "fixture"]
