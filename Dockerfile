FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy all files from the current directory
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variables for Hugging Face (PORT is 7860 by default)
ENV PORT=7860
ENV WEBAPP_HOST=0.0.0.0
ENV WEBAPP_PORT=7860

# Expose the default Hugging Face Spaces port
EXPOSE 7860

# Run both the bot and the web app using our runner
CMD ["python", "render_runner.py"]
