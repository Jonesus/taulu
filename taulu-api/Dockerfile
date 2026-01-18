# Use a Python image with uv pre-installed
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Set environment variables
ENV UV_COMPILE_BYTECODE=1 
ENV UV_LINK_MODE=copy

# Set working directory
WORKDIR /app

# Install dependencies
# Copy only the files needed for dependency installation first to leverage caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy the rest of the application
COPY . .

# Sync again to ensure everything is consistent (fast if nothing changed)
RUN uv sync --frozen --no-dev

# Final stage
FROM python:3.12-slim-bookworm

# Create a non-root user
RUN useradd -m appuser

# Set working directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
# Copy application code
COPY --from=builder /app /app

# Ensure we use the virtual environment
ENV PATH="/app/.venv/bin:$PATH"
# Production settings
ENV PYTHONUNBUFFERED=1
ENV PORT=3000

# Switch to non-root user
USER appuser

# Create directory for ready images if it doesn't exist (handled by app but good for permissions)
# Since we copied /app, if 'ready' existed it's there. 
# But we need to ensure the user can write to 'ready' and 'state.json'.
# In the builder stage, root owned everything.
# We need to fix permissions.

USER root
RUN mkdir -p ready && \
    touch state.json && \
    chown -R appuser:appuser /app
USER appuser

# Expose the port
EXPOSE 3000

# Run the application using Gunicorn
# 4 workers is a reasonable starting point; adjust based on resources
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:3000", "main:app"]
