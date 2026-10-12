"""Entry point for uvicorn: `uvicorn echo.web.main:app --host 127.0.0.1 --port 8000`."""

import logging

from echo.web.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
