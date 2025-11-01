FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

VOLUME [ "/app" ]

ENV FLASK_APP=main.py
ENV FLASK_ENV=production

EXPOSE 5000

# 启动命令
CMD ["python3", "src/main.py"]
