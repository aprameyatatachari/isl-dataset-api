from fastapi import FastAPI
import requests
import uvicorn
from bs4 import BeautifulSoup

app = FastAPI()
url = "https://www.indiansignlanguage.org/search-dictionary/"