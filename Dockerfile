FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    postgresql-client \
    zstd \
    && rm -rf /var/lib/apt/lists/*

# Install Docker using official get.docker.com script
RUN curl -fsSL https://get.docker.com | sh

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY tools/ ./tools/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Create necessary directories
RUN mkdir -p /archives

EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "app.main:app"]
