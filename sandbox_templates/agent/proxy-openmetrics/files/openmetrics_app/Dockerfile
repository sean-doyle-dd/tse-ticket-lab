# Pinned to 3.12-slim for stability. Flask-BasicAuth currently depends on
# `from flask import Markup`, which Flask 3.1+ removed, so we pair this with
# a Flask<3 pin in requirements.txt.
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt /app
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . /app

ENTRYPOINT ["python3"]
CMD ["app.py"]
