FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libffi-dev \
    libnacl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the current directory contents into the container at /app
COPY . /app

# Upgrade pip and install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create a downloads directory and set permissions
RUN mkdir -p downloads && chmod 777 downloads

# Set environment variable to ensure Python output is sent straight to terminal
ENV PYTHONUNBUFFERED=1

# Print Python version and installed packages for debugging
RUN python --version && pip list

# Run app.py when the container launches
CMD ["python", "app.py"]
