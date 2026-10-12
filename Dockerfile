FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-commerce.txt ./
RUN python -m pip install --no-cache-dir -r requirements-commerce.txt
COPY app ./app
COPY mcp_server ./mcp_server
COPY frontend ./frontend
RUN mkdir -p /app/data
EXPOSE 8000 8004
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
