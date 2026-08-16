# Mumbai Traffic Network Planning — hosting image.
# Serves the dashboard + report + JSON API (read-only). Data collection is a
# separate scheduled job (see scripts/crontab.example), not part of this image.
FROM python:3.12-slim

# geopandas / shapely / osmnx need a few system libs (GEOS, GDAL, PROJ).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-dev libgdal-dev libspatialindex-dev gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + precomputed outputs (docs/, data/processed/).
COPY . .

EXPOSE 8000
ENV PORT=8000

# Honour the platform-provided $PORT (Render/Railway/Fly set it); default 8000.
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT}"]
