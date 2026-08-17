# Mumbai Traffic Network Planning — hosting image.
# Serves the dashboard + report + JSON API (read-only). Data collection is a
# separate scheduled process (the current deployment uses collect_campaign),
# not part of the public web process.
FROM python:3.12-slim

# geopandas / shapely / osmnx need a few system libs (GEOS, GDAL, PROJ).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-dev libgdal-dev libspatialindex-dev gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
#
# requirements-server.txt, not requirements.txt: the containers never open a
# notebook, and JupyterLab alone is 61 distributions. See that file for what is
# left out and why.
#
# --only-binary=:all: refuses to build any package from source. The build host
# downloads at about 200 kB/s, so a single silent source build costs more than
# the rest of the image; failing in seconds is better than compiling for ten
# minutes. It also makes the apt layer above measurable — if this succeeds,
# nothing needed a compiler or a system header, and those packages can go.
COPY requirements-server.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements-server.txt

# Keep every prebuilt map payload outside the processed-data mount as well as
# inside the application tree. Existing named volumes hide newly shipped files
# under /app/data/processed, so the web service falls back to this immutable
# seed until a payload has been generated in the shared writable volume.
COPY data/processed/map/ /app/data-seed/map/

# App code + precomputed outputs (docs/, data/processed/).
COPY . .

EXPOSE 8000
ENV PORT=8000
ENV MAP_SEED_DIR=/app/data-seed/map
ENV COVERAGE_SEED_PATH=/app/data-seed/map/coverage.json

# Honour the platform-provided $PORT (Render/Railway/Fly set it); default 8000.
CMD ["sh", "-c", "uvicorn src.web.app:app --host 0.0.0.0 --port ${PORT}"]
