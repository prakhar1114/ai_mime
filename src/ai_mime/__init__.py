import os
from pathlib import Path
from dotenv import load_dotenv
from lmnr import Laminar

# Load .env from repo root (deterministic) and also allow CWD-based fallback.
_repo_root_env = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_repo_root_env)

if os.getenv("LMNR_PROJECT_API_KEY") is not None:
    Laminar.initialize()
