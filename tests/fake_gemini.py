"""A stand-in for the Gemini generateContent endpoint."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from uniagent.httpjson import Response


@dataclass
class FakeGemini:
    api_key: str = "AIza-secret"
    answers: list = field(default_factory=list)          # dicts (or raw strings) returned in order
    quota_models: set = field(default_factory=set)       # these models answer 429
    missing_models: set = field(default_factory=set)     # these answer 404
    fail_next: list = field(default_factory=list)        # [(status, payload)] consumed first
    log: list = field(default_factory=list)              # (model, headers, body)

    def __call__(self, method, url, headers, body, timeout):
        model = re.search(r"/models/([^:]+):generateContent", url).group(1)
        self.log.append((model, dict(headers), body))
        if headers.get("x-goog-api-key") != self.api_key:
            return Response(400, {}, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}})
        if self.fail_next:
            status, payload = self.fail_next.pop(0)
            return Response(status, {}, payload)
        if model in self.missing_models:
            return Response(404, {}, {"error": {"code": 404, "message": f"models/{model} is not found"}})
        if model in self.quota_models:
            return Response(429, {}, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                                "details": [{"retryDelay": "37s"}]}})
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseSchema" in body["generationConfig"] and "systemInstruction" in body
        answer = self.answers.pop(0) if self.answers else {"commands": [], "questions": []}
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Response(200, {}, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})
