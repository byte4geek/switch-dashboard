FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_DATA_DIR=/data

# Create and set the workspace directory
WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Create the data directory
RUN mkdir -p /data

# Expose Flask port
EXPOSE 8080

# Run the application
CMD ["python", "app.py"]
