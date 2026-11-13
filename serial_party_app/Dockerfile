FROM python:3.13-alpine3.22

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY server.py ./
COPY static ./static
EXPOSE 8099
CMD ["python", "server.py"]
