FROM python:3.12-slim

# git is needed to commit + push approved profiles to the catalog.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# The broker code + the cooker come from the mounted catalog repo at /catalog;
# we only bake the Python deps into the image so it stays tiny and the code
# tracks the repo. (CATALOG_DIR defaults to the parent of the broker dir.)
WORKDIR /catalog/broker
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV CATALOG_DIR=/catalog \
    BROKER_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
