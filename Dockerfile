# Use official lightweight Python image
FROM python:3.10-slim

# Install system dependencies needed for compiling python packages (like psycopg2)
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file first to utilize Docker build cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all backend source code
COPY . .

# Expose port 7860 (Hugging Face Spaces default requirement)
EXPOSE 7860

# Start FastAPI application using Uvicorn on port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
