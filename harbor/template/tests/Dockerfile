FROM python:3.11.15-slim-bookworm
COPY . /tests/
RUN chmod -R a-w /tests
WORKDIR /tests
CMD ["sleep", "infinity"]
