# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
COPY tools/fetch_source.py /app/fetch_source.py
RUN python /app/fetch_source.py
RUN python -m compileall -q /opt/sqlglot
FROM source AS runtime
COPY selector.py /app/selector.py
COPY precision_selector.py default_dependencies.py shared_executor_dependencies.py /app/
COPY import_state_selector.py import_state_readers.py /app/
COPY tools/import_check.py /app/tools/import_check.py
COPY tools/fork_import.py tools/comparison_proof.py tools/comparison_values.py /app/tools/
COPY tools/import_state.py tools/import_state_probe.py /app/tools/
COPY tools/check_import_state_profile.py /app/tools/
COPY artifacts /app/artifacts
RUN python -m compileall -q /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONHASHSEED=0
RUN python -B /app/tools/check_import_state_profile.py
CMD ["python", "/app/precision_selector.py"]
