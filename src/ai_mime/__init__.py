import os
from dotenv import load_dotenv
from lmnr import Laminar
from ai_mime.app_data import get_env_path

load_dotenv(get_env_path())

if os.getenv("LMNR_PROJECT_API_KEY") is not None:
    Laminar.initialize()
