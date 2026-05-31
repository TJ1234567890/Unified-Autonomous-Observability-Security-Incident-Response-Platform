import os
import sys

from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException

# Load .env from project root regardless of where uvicorn is launched from
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))

from models import SearchRequest, SearchResponse
from search import execute_search

ES_URL = os.getenv("ELASTIC_URL", "http://localhost:9200")
ES_INDEX = "beth-security-logs"

app = FastAPI(title="Security Log Search API", version="1.0.0")

es_client = Elasticsearch(ES_URL)


@app.get("/health")
def health():
    if not es_client.ping():
        raise HTTPException(status_code=503, detail="Elasticsearch unreachable")
    return {"status": "ok", "es_url": ES_URL, "index": ES_INDEX}


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    try:
        return execute_search(es_client, ES_INDEX, req)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
