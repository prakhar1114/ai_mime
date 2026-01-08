import os
from lmnr import Laminar

if os.getenv("LMNR_PROJECT_API_KEY") is not None:
    Laminar.initialize()
