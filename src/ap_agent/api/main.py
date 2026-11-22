"""FastAPI app (placeholder)."""

from fastapi import FastAPI

app = FastAPI(title="AP Invoice Tracking Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
