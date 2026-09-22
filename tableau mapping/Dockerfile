# Use a lightweight python image
FROM python:3.11-slim

# Set environment variables for better Python execution in containers
# PYTHONDONTWRITEBYTECODE=1: Prevents Python from writing .pyc files
# PYTHONUNBUFFERED=1: Ensures console output is not buffered by Docker
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Install dependencies first (leverages Docker cache so code changes don't trigger pip reinstall)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create a non-root user for security purposes
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

# Copy the application code
COPY app ./app

# Expose the application port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]