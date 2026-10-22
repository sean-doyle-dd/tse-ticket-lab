FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# START_CMD lets the lab swap how the app is launched (with or without ddtrace-run).
ENV START_CMD="ddtrace-run python app.py"
CMD ["sh", "-c", "exec $START_CMD"]
