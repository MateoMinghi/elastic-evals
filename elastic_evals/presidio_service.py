"""Presidio analyzer service for the collector's redaction step (DESIGN.md §5.2, §9).

Speaks the REST contract of Microsoft's presidio-analyzer image (`POST /analyze`, `GET /health`),
so either can sit behind the gateway's `presidio.endpoint`; this one loads small spaCy models for
several languages (EE_PRESIDIO_MODELS="en:en_core_web_sm,es:es_core_news_sm").

    python -m elastic_evals presidio --port 3000
"""

import os
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


def _models():
    spec = os.environ.get("EE_PRESIDIO_MODELS", "en:en_core_web_sm,es:es_core_news_sm")
    return [dict(zip(("lang_code", "model_name"), item.split(":"))) for item in spec.split(",") if item]


@lru_cache(maxsize=1)
def engine():
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    models = _models()
    nlp = NlpEngineProvider(nlp_configuration={"nlp_engine_name": "spacy", "models": models}).create_engine()
    return AnalyzerEngine(nlp_engine=nlp, supported_languages=[m["lang_code"] for m in models])


class AnalyzeRequest(BaseModel):
    text: str
    language: str = "en"
    entities: list[str] | None = None
    score_threshold: float | None = None


@asynccontextmanager
async def lifespan(_):
    engine()  # load the NLP models before the first request: /health means ready
    yield


app = FastAPI(title="elastic-evals presidio", lifespan=lifespan)


@app.get("/health")
def health():
    return "Presidio Analyzer service is up"


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    eng = engine()
    lang = req.language if req.language in eng.supported_languages else eng.supported_languages[0]
    try:
        results = eng.analyze(text=req.text, language=lang, entities=req.entities or None,
                              score_threshold=req.score_threshold or 0.0)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return [{"entity_type": r.entity_type, "start": r.start, "end": r.end, "score": r.score} for r in results]
