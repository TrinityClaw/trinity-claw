#!/usr/bin/env python3
"""
Minimal OpenAI-compatible proxy — drop-in replacement for `litellm --config`
on Hugging Face Spaces.  No database, no Prisma, no startup failures.

Reads model/api_key/api_base from litellm_config.hf.yaml on every request
so Settings UI changes take effect without a restart.
Listens on port 4000.
"""
import os, yaml, requests, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CONFIG = os.getenv("LITELLM_CONFIG", "/app/config/litellm_config.hf.yaml")

def _cfg():
    with open(CONFIG) as f:
        data = yaml.safe_load(f)
    p = data["model_list"][0]["litellm_params"]
    key = p.get("api_key", "sk-placeholder")
    if key.startswith("os.environ/"):
        key = os.getenv(key[len("os.environ/"):], "sk-placeholder")
    return {
        "model":    p.get("model", "gpt-4o"),
        "api_key":  key,
        "api_base": p.get("api_base", "https://api.openai.com/v1").rstrip("/"),
    }

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    c = _cfg()

    model = body.get("model", "")
    if not model or "trinity-default" in model:
        model = c["model"]
    body["model"] = model

    stream = body.get("stream", False)
    upstream = f"{c['api_base']}/chat/completions"
    headers  = {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}

    if stream:
        def _iter():
            with requests.post(upstream, json=body, headers=headers, stream=True, timeout=600) as r:
                for chunk in r.iter_content(chunk_size=None):
                    yield chunk
        return StreamingResponse(_iter(), media_type="text/event-stream")

    r = requests.post(upstream, json=body, headers=headers, timeout=600)
    return JSONResponse(r.json(), status_code=r.status_code)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000, log_level="warning")
