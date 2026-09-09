"""FastAPI application: upload a lab report or JSON, get the full explainable analysis.

Run:  python -m uvicorn app:app --reload --port 8000
Then: http://127.0.0.1:8000
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engine.config import get_config
from engine.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"                    # the interface served at /
WEB_ALT = ROOT / "web_v2"             # alternative layout, kept and served at /v2
SAMPLES = ROOT / "samples"

MAX_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".json", ".pdf", ".csv", ".tsv", ".txt"}

app = FastAPI(title="Disease Correlation & Risk Prediction Engine", version="1.0")

_pipeline = None


def pipeline():
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/v2")
def alt_index():
    """Alternative layout, kept alongside the main interface. Same APIs, same results.

    Delete the web_v2 directory to remove it; this route disappears with it.
    """
    if not (WEB_ALT / "index.html").exists():
        raise HTTPException(status_code=404, detail="the alternative layout is not installed")
    return FileResponse(WEB_ALT / "index.html")


@app.get("/api/health")
def health():
    cfg = get_config()
    return {
        "status": "ok" if cfg.validation["ok"] else "config_error",
        "config": cfg.validation["counts"],
        "errors": cfg.validation["errors"],
        "warnings": cfg.validation["warnings"][:20],
    }


@app.get("/api/config/summary")
def config_summary():
    """What the engine is running on - shown in the dashboard's Reference tab."""
    cfg = get_config()
    return {
        "disease_master": {
            "source_file": cfg.dm_meta.get("source_file"),
            "sheet": cfg.dm_meta.get("source_sheet"),
            "columns": cfg.dm_meta.get("columns"),
            "disease_count": len(cfg.diseases),
            "skipped_rows": cfg.dm_meta.get("skipped_rows"),
            "provenance_note": cfg.dm_meta.get("provenance_note"),
        },
        "counts": cfg.validation["counts"],
        "profiles": cfg.dm_profiles,
        "field_legend": cfg.dm_legend,
        "data_quality_notes": cfg.dm_notes,
        "unclear_mappings_resolved": cfg.dm_unclear,
        "unmappable": cfg.unmappable,
        "cohorts": [
            {
                "id": c["id"], "name": c["name"],
                "category": c.get("category"), "domain": c.get("domain"),
                "description": c.get("description"),
                "profiles_touched": c.get("profiles_touched", []),
                "cross_profile": bool(c.get("cross_profile_rationale")),
                "cross_profile_rationale": c.get("cross_profile_rationale"),
                "mode": c.get("mode", "weighted"),
                "evidence": c.get("evidence", []),
                "diseases": c.get("diseases", []),
                "expected_parameters": c.get("expected_parameters", []),
                "source_file": c.get("_source_file"),
            }
            for c in cfg.cohorts
        ],
        "parameters": [
            {"id": p["id"], "name": p["name"], "profile": p.get("profile"),
             "type": p["type"], "unit": p.get("unit"),
             "alias_count": len(p.get("aliases", []))}
            for p in cfg.parameters
        ],
    }


@app.get("/api/diseases")
def diseases():
    cfg = get_config()
    return [
        {"id": d["id"], "name": d["name"], "classification": d.get("classification"),
         "profiles": d.get("profiles"), "urgency": d.get("urgency"),
         "icd10": d.get("icd10"), "review_status": d.get("review_status"),
         "markers": d["fields"].get("Related Markers/Tests"),
         "high_risk_indicators": d["fields"].get("High-Risk Indicators"),
         "mapped_by": [c["id"] for c, _ in cfg.links_by_disease.get(d["name"], [])]}
        for d in cfg.diseases
    ]


# Labels for the non-JSON samples, which cannot carry a "_label" key of their own.
SAMPLE_LABELS = {
    "p7_report.pdf": "PDF lab report (male, 6 panels, table extraction)",
    "p8_thyroid.csv": "CSV report (female, thyrotoxic pattern)",
}


@app.get("/api/samples")
def list_samples():
    out = []
    for f in sorted(SAMPLES.iterdir()):
        if f.suffix.lower() not in ALLOWED_SUFFIXES or not f.is_file():
            continue
        label = SAMPLE_LABELS.get(f.name)
        if label is None and f.suffix.lower() == ".json":
            try:
                label = json.loads(f.read_text(encoding="utf-8")).get("_label")
            except Exception:
                label = None
        out.append({"file": f.name,
                    "label": label or f.stem.replace("_", " ").title(),
                    "kind": f.suffix.lstrip(".").upper()})
    return out


@app.post("/api/analyse/sample")
def analyse_sample(file: str = Form(...), sex: str = Form(None), age: float = Form(None)):
    target = (SAMPLES / file).resolve()
    if not str(target).startswith(str(SAMPLES.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="sample not found")
    result = pipeline().run(target.read_bytes(), target.name,
                            sex=_clean_sex(sex), age=age)
    return JSONResponse(result)


@app.post("/api/analyse")
async def analyse_upload(file: UploadFile = File(...),
                         sex: str = Form(None),
                         age: float = Form(None),
                         patient_id: str = Form(None)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail="unsupported file type '%s'. Upload a JSON, PDF, CSV or TXT report." % suffix)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="the uploaded file is empty")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="file is larger than 20 MB")

    try:
        result = pipeline().run(data, file.filename or "upload",
                                sex=_clean_sex(sex), age=age, patient_id=patient_id)
    except Exception as exc:                      # surfaced rather than swallowed
        raise HTTPException(status_code=422,
                            detail="could not analyse this file: %s" % exc) from exc

    if result["summary"]["parameters_recognised"] == 0:
        result["warnings"] = list(result.get("warnings", [])) + [
            "No laboratory parameters were recognised in this file. If it is a scanned PDF "
            "it has no text layer and would need OCR; if it is JSON, check that test names "
            "and values are present."]
    return JSONResponse(result)


def _clean_sex(value):
    if not value:
        return None
    v = str(value).strip().lower()
    return v if v in ("male", "female") else None


app.mount("/static", StaticFiles(directory=WEB), name="static")
if WEB_ALT.is_dir():
    app.mount("/v2-static", StaticFiles(directory=WEB_ALT), name="v2-static")
