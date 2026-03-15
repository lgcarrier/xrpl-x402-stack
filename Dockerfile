FROM python:3.12-slim

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /app \
    && chown app:app /app

WORKDIR /app
COPY LICENSE ./LICENSE
COPY packages ./packages
COPY examples ./examples
RUN pip install --no-cache-dir /app/packages/core /app/packages/facilitator /app/packages/middleware /app/packages/client /app/packages/payer

USER app
EXPOSE 8000

CMD ["xrpl-x402-facilitator", "--host", "0.0.0.0", "--port", "8000"]
