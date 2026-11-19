FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. Dependencies first: this layer is reused until pyproject.toml changes.
COPY pyproject.toml README.md ./
COPY logseek/__init__.py logseek/__init__.py
RUN pip install -e .

# 2. Bake a demo log into the image at build time (the build machine is fast;
#    a free 0.1 CPU host would need minutes to generate it at startup).
#    Only generator.py is copied here, so editing the web page does not
#    regenerate the 100 MB file.
COPY logseek/generator.py logseek/generator.py
ARG LOG_SIZE=100MB
RUN python -m logseek.generator --out data/app.log --size ${LOG_SIZE}

# 3. The rest of the code.
COPY logseek logseek

# Do not run as root.
RUN useradd --create-home app
USER app

EXPOSE 8000

# "exec" makes Python PID 1, so it receives SIGTERM and shuts down cleanly.
# Mount your own log with: docker run -v /path/to/my.log:/app/data/app.log ...
CMD ["sh", "-c", "exec logseek-web data/app.log --host 0.0.0.0 --port ${PORT:-8000}"]
