FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
RUN mkdir -p /data

ENV PORT=8080
ENV DATA_FILE=/data/data.json

EXPOSE 8080

CMD ["python", "bot.py"]