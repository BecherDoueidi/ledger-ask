# Runtime image for the Flask app only. MySQL and Ollama are separate
# services (see docker-compose.yml) -- this container has no database or
# LLM baked in, so a code change never requires re-downloading either.
FROM python:3.12-slim

WORKDIR /app

# Installed before the rest of the source so `docker build` can reuse this
# layer on every rebuild that only changes application code, not deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# gunicorn, not `python app.py`: this is a container image meant to be run
# unattended, not a local dev loop -- no need for Flask's debug reloader,
# and gunicorn is what app.py's own dev-server warning tells you to use
# instead ("Do not use it in a production deployment"). Shell form (not
# exec-array form) so $PORT actually expands; timeout raised well above
# gunicorn's 30s default because a fresh LLM generation (schema harvest +
# prompt + Ollama call + up to 2 self-healing retries) can legitimately
# take longer than that.
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 app:app
