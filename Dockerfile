FROM python:3.12-slim
WORKDIR /app
COPY kalshi_arb ./kalshi_arb
EXPOSE 8000
# Live dashboard; override with e.g. `docker run ... demo --host 0.0.0.0 --no-open`
ENTRYPOINT ["python", "-m", "kalshi_arb"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
