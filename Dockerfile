FROM python:3.11-alpine

# Set build-time metadata
LABEL maintainer="NPM Docker Agent Contributors"
LABEL description="An automated bridge between Docker container labels and Nginx Proxy Manager."

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies
# We use a non-root user for better security
# Note: If using a local unix socket, the user 'agent' needs permission to read/write it.
# Often this means adding the user to the 'docker' group (GID 999 or similar).
RUN addgroup -g 999 docker && \
    addgroup -S agent && adduser -S agent -G agent && \
    addgroup agent docker && \
    pip install --no-cache-dir docker requests

# Copy application code
COPY --chown=agent:agent agent.py .

# Use non-root user
USER agent

# Dashboard Port
EXPOSE 8080

# Healthcheck to ensure the process is running
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import socket; s = socket.socket(); s.connect(('localhost', 8080))" || exit 1

CMD ["python", "agent.py"]
