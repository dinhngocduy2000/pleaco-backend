
FROM public.ecr.aws/lambda/python:3.12

# ── Lambda expects your code & deps in this directory ──
WORKDIR ${LAMBDA_TASK_ROOT}

# ── Copy & install production dependencies first (best layer caching) ──
COPY requirements.txt .
RUN pip install --no-cache-dir --target "${LAMBDA_TASK_ROOT}" -r requirements.txt

# ── Copy the entire app/ folder
#    → ends up as ${LAMBDA_TASK_ROOT}/app/
COPY app/ ./app/

# ── Optional: reduce image size (remove caches & compiled files)
RUN find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    find . -type f -name "*.pyc"       -delete             || true


CMD ["app.cmd.main.lambda_handler"]

