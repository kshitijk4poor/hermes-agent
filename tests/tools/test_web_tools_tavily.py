import json

import pytest


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_web_search_tool_uses_tavily_backend(monkeypatch):
    from tools import web_tools

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    calls = []

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(
            {
                "results": [
                    {
                        "title": "Hermes Agent",
                        "url": "https://example.com/hermes",
                        "content": "Project overview",
                        "score": 0.98,
                    }
                ]
            }
        )

    monkeypatch.setattr(web_tools.httpx, "post", fake_post)

    result = json.loads(web_tools.web_search_tool("hermes agent", limit=3))

    assert calls[0]["url"] == "https://api.tavily.com/search"
    assert calls[0]["json"]["max_results"] == 3
    assert result["success"] is True
    assert result["data"]["web"][0]["title"] == "Hermes Agent"
    assert result["data"]["web"][0]["description"] == "Project overview"


@pytest.mark.asyncio
async def test_web_extract_tool_uses_tavily_backend(monkeypatch):
    from tools import web_tools

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(web_tools, "check_website_access", lambda url: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    def fake_post(url, json, timeout):
        assert url == "https://api.tavily.com/extract"
        assert json["urls"] == ["https://example.com/doc"]
        return _FakeResponse(
            {
                "results": [
                    {
                        "url": "https://example.com/doc",
                        "title": "Example Doc",
                        "raw_content": "Full extracted content",
                    }
                ]
            }
        )

    monkeypatch.setattr(web_tools.httpx, "post", fake_post)

    result = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/doc"],
            use_llm_processing=False,
        )
    )

    assert result["results"][0]["title"] == "Example Doc"
    assert result["results"][0]["content"] == "Full extracted content"
    assert result["results"][0]["error"] is None


@pytest.mark.asyncio
async def test_web_crawl_tool_uses_tavily_backend(monkeypatch):
    from tools import web_tools

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(web_tools, "check_website_access", lambda url: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    def fake_post(url, json, timeout):
        assert url == "https://api.tavily.com/crawl"
        assert json["url"] == "https://example.com"
        assert json["instructions"] == "Find docs"
        assert json["extract_depth"] == "advanced"
        return _FakeResponse(
            {
                "results": [
                    {
                        "url": "https://example.com/docs",
                        "title": "Docs",
                        "raw_content": "Documentation page",
                    }
                ]
            }
        )

    monkeypatch.setattr(web_tools.httpx, "post", fake_post)

    result = json.loads(
        await web_tools.web_crawl_tool(
            "https://example.com",
            instructions="Find docs",
            depth="advanced",
            use_llm_processing=False,
        )
    )

    assert result["results"][0]["url"] == "https://example.com/docs"
    assert result["results"][0]["content"] == "Documentation page"
    assert result["results"][0]["error"] is None
