FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY echo ./echo
RUN pip install --no-cache-dir ".[postgres]"

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "echo.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
