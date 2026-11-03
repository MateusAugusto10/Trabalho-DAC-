.PHONY: up down install run test
up:      ; docker compose up -d --wait
down:    ; docker compose down -v
install: ; python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
run:     ; .venv/bin/python run.py all
test:    ; .venv/bin/python -m pytest -v
