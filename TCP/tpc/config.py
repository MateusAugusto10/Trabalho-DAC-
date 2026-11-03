import os

PG_DSN = os.getenv("PG_DSN", "postgresql://postgres:postgres@localhost:5432/tpc")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "tpc")
TX_LOG_PATH = os.getenv("TX_LOG_PATH", "reports/coordinator.log.jsonl")
