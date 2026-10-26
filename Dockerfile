# One image for both services. The hub (`matchlab serve`) is the default command; the fork service
# runs the same image with `matchlab forkd ...` (see infra/main.bicep). The image carries the
# deterministic league, forty simulated matches for their metadata, and the threat grid learned from
# them, so forkd can follow any match the Eventhouse knows without a data volume. The hidden truth
# files are not shipped.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY matchlab ./matchlab
COPY renderer ./renderer
RUN pip install -e ".[eventhub,fabric,agents,cosmos]"

ARG LEAGUE_SEED=7
ARG MATCHES=40
ARG MATCH_SEED=1
RUN matchlab league --seed ${LEAGUE_SEED} \
 && matchlab generate --matches ${MATCHES} --seed ${MATCH_SEED} \
 && python -c "from matchlab.forkd import load_grid; g = load_grid('data/threat_grid.json', 'data'); print('threat grid cached')" \
 && rm -rf data/truth

RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["matchlab", "serve", "--port", "8080"]
