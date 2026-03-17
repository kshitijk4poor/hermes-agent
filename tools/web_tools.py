#!/usr/bin/env python3
"""
Standalone Web Tools Module

This module provides generic web tools that work with multiple backend providers.
Currently uses Firecrawl as the backend, and the interface makes it easy to swap
providers without changing the function signatures.

Available tools:
- web_search_tool: Search the web for information
- web_extract_tool: Extract content from specific web pages
- web_crawl_tool: Crawl websites with specific instructions

Backend compatibility:
- Firecrawl: https://docs.firecrawl.dev/introduction
- Tavily: https://docs.tavily.com/

LLM Processing:
- Uses OpenRouter API with Gemini 3 Flash Preview for intelligent content extraction
- Extracts key excerpts and creates markdown summaries to reduce token usage

Debug Mode:
- Set WEB_TOOLS_DEBUG=true to enable detailed logging
- Creates web_tools_debug_UUID.json in ./logs directory
- Captures all tool calls, results, and compression metrics

Usage:
    from web_tools import web_search_tool, web_extract_tool, web_crawl_tool
    
    # Search the web
    results = web_search_tool("Python machine learning libraries", limit=3)
    
    # Extract content from URLs  
    content = web_extract_tool(["https://example.com"], format="markdown")
    
    # Crawl a website
    crawl_data = web_crawl_tool("example.com", "Find contact information")
"""

#TODO: Search Capabilities over the scraped pages
#TODO: Store the pages in something
#TODO: Tool to see what pages are available/saved to search over

import json
import logging
import os
import re
import asyncio
from typing import List, Dict, Any, Optional

import httpx
from firecrawl import Firecrawl
from agent.auxiliary_client import async_call_llm
from tools.debug_helpers import DebugSession
from tools.website_policy import WebsitePolicyError, check_website_access

logger = logging.getLogger(__name__)

_firecrawl_client = None


def _has_firecrawl_config() -> bool:
    return bool(os.getenv("FIRECRAWL_API_KEY") or os.getenv("FIRECRAWL_API_URL"))


def _has_tavily_config() -> bool:
    return bool(os.getenv("TAVILY_API_KEY"))


def _get_active_web_backend() -> str:
    """Prefer Firecrawl when configured, otherwise fall back to Tavily."""
    if _has_firecrawl_config():
        return "firecrawl"
    if _has_tavily_config():
        return "tavily"
    raise ValueError(
        "No web backend configured. Set FIRECRAWL_API_KEY or FIRECRAWL_API_URL "
        "for Firecrawl, or set TAVILY_API_KEY for Tavily."
    )


def _get_tavily_base_url() -> str:
    return (os.getenv("TAVILY_API_URL") or "https://api.tavily.com").rstrip("/")


def _tavily_request(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY environment variable not set.")

    response = httpx.post(
        f"{_get_tavily_base_url()}/{endpoint.lstrip('/')}",
        json={"api_key": api_key, **payload},
        timeout=60,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.is_error:
        detail = data.get("detail") or data.get("error") or response.text
        raise RuntimeError(f"Tavily API error ({response.status_code}): {detail}")

    if isinstance(data, dict) and (data.get("error") or data.get("detail")):
        detail = data.get("error") or data.get("detail")
        raise RuntimeError(f"Tavily API error: {detail}")

    return data if isinstance(data, dict) else {}

def _get_firecrawl_client():
    """Get or create the Firecrawl client (lazy initialization).

    Uses the cloud API by default (requires FIRECRAWL_API_KEY).
    Set FIRECRAWL_API_URL to point at a self-hosted instance instead —
    in that case the API key is optional (set USE_DB_AUTHENTICATION=false
    on your Firecrawl server to disable auth entirely).
    """
    global _firecrawl_client
    if _firecrawl_client is None:
        api_key = os.getenv("FIRECRAWL_API_KEY")
        api_url = os.getenv("FIRECRAWL_API_URL")
        if not api_key and not api_url:
            raise ValueError(
                "FIRECRAWL_API_KEY environment variable not set. "
                "Set it for cloud Firecrawl, or set FIRECRAWL_API_URL "
                "to use a self-hosted instance."
            )
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if api_url:
            kwargs["api_url"] = api_url
        _firecrawl_client = Firecrawl(**kwargs)
    return _firecrawl_client


def _to_dict(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    if isinstance(value, dict):
        return value
    return {}


def _normalize_firecrawl_search_results(response: Any) -> List[Dict[str, Any]]:
    web_results: List[Dict[str, Any]] = []
    if hasattr(response, "web"):
        for result in response.web or []:
            result_dict = _to_dict(result)
            if result_dict:
                web_results.append(result_dict)
    elif hasattr(response, "model_dump"):
        response_dict = response.model_dump()
        if response_dict.get("web"):
            web_results = list(response_dict["web"])
    elif isinstance(response, dict) and response.get("web"):
        web_results = list(response["web"])
    return web_results


def _normalize_tavily_search_results(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    web_results: List[Dict[str, Any]] = []
    for index, result in enumerate(response.get("results") or [], start=1):
        result_dict = _to_dict(result)
        web_results.append(
            {
                "title": result_dict.get("title", ""),
                "url": result_dict.get("url", ""),
                "description": result_dict.get("content", ""),
                "position": index,
                **({"score": result_dict["score"]} if "score" in result_dict else {}),
            }
        )
    return web_results


def _normalize_tavily_documents(response: Dict[str, Any], fallback_url: str = "") -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []

    for item in response.get("results") or []:
        result_dict = _to_dict(item)
        metadata = result_dict.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        final_url = (
            result_dict.get("url")
            or result_dict.get("source_url")
            or metadata.get("sourceURL")
            or fallback_url
        )
        content = (
            result_dict.get("raw_content")
            or result_dict.get("content")
            or result_dict.get("markdown")
            or result_dict.get("text")
            or ""
        )
        documents.append(
            {
                "url": final_url,
                "title": result_dict.get("title") or metadata.get("title", ""),
                "content": content,
                "raw_content": content,
                "metadata": metadata,
            }
        )

    failed_results = response.get("failed_results") or response.get("failed_urls") or []
    for item in failed_results:
        result_dict = _to_dict(item)
        failed_url = result_dict.get("url") or result_dict.get("source_url") or item
        error = result_dict.get("error") or result_dict.get("reason") or "Extraction failed"
        documents.append(
            {
                "url": failed_url or fallback_url,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": str(error),
            }
        )

    return documents

DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000

# Allow per-task override via env var
DEFAULT_SUMMARIZER_MODEL = os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip() or None

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


async def process_content_with_llm(
    content: str, 
    url: str = "", 
    title: str = "",
    model: str = DEFAULT_SUMMARIZER_MODEL,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> Optional[str]:
    """
    Process web content using LLM to create intelligent summaries with key excerpts.
    
    This function uses Gemini 3 Flash Preview (or specified model) via OpenRouter API 
    to intelligently extract key information and create markdown summaries,
    significantly reducing token usage while preserving all important information.
    
    For very large content (>500k chars), uses chunked processing with synthesis.
    For extremely large content (>2M chars), refuses to process entirely.
    
    Args:
        content (str): The raw content to process
        url (str): The source URL (for context, optional)
        title (str): The page title (for context, optional)
        model (str): The model to use for processing (default: google/gemini-3-flash-preview)
        min_length (int): Minimum content length to trigger processing (default: 5000)
        
    Returns:
        Optional[str]: Processed markdown content, or None if content too short or processing fails
    """
    # Size thresholds
    MAX_CONTENT_SIZE = 2_000_000  # 2M chars - refuse entirely above this
    CHUNK_THRESHOLD = 500_000     # 500k chars - use chunked processing above this
    CHUNK_SIZE = 100_000          # 100k chars per chunk
    MAX_OUTPUT_SIZE = 5000        # Hard cap on final output size
    
    try:
        content_len = len(content)
        
        # Refuse if content is absurdly large
        if content_len > MAX_CONTENT_SIZE:
            size_mb = content_len / 1_000_000
            logger.warning("Content too large (%.1fMB > 2MB limit). Refusing to process.", size_mb)
            return f"[Content too large to process: {size_mb:.1f}MB. Try using web_crawl with specific extraction instructions, or search for a more focused source.]"
        
        # Skip processing if content is too short
        if content_len < min_length:
            logger.debug("Content too short (%d < %d chars), skipping LLM processing", content_len, min_length)
            return None
        
        # Create context information
        context_info = []
        if title:
            context_info.append(f"Title: {title}")
        if url:
            context_info.append(f"Source: {url}")
        context_str = "\n".join(context_info) + "\n\n" if context_info else ""
        
        # Check if we need chunked processing
        if content_len > CHUNK_THRESHOLD:
            logger.info("Content large (%d chars). Using chunked processing...", content_len)
            return await _process_large_content_chunked(
                content, context_str, model, CHUNK_SIZE, MAX_OUTPUT_SIZE
            )
        
        # Standard single-pass processing for normal content
        logger.info("Processing content with LLM (%d characters)", content_len)
        
        processed_content = await _call_summarizer_llm(content, context_str, model)
        
        if processed_content:
            # Enforce output cap
            if len(processed_content) > MAX_OUTPUT_SIZE:
                processed_content = processed_content[:MAX_OUTPUT_SIZE] + "\n\n[... summary truncated for context management ...]"
            
            # Log compression metrics
            processed_length = len(processed_content)
            compression_ratio = processed_length / content_len if content_len > 0 else 1.0
            logger.info("Content processed: %d -> %d chars (%.1f%%)", content_len, processed_length, compression_ratio * 100)
        
        return processed_content
        
    except Exception as e:
        logger.debug("Error processing content with LLM: %s", e)
        return f"[Failed to process content: {str(e)[:100]}. Content size: {len(content):,} chars]"


async def _call_summarizer_llm(
    content: str, 
    context_str: str, 
    model: str, 
    max_tokens: int = 20000,
    is_chunk: bool = False,
    chunk_info: str = ""
) -> Optional[str]:
    """
    Make a single LLM call to summarize content.
    
    Args:
        content: The content to summarize
        context_str: Context information (title, URL)
        model: Model to use
        max_tokens: Maximum output tokens
        is_chunk: Whether this is a chunk of a larger document
        chunk_info: Information about chunk position (e.g., "Chunk 2/5")
        
    Returns:
        Summarized content or None on failure
    """
    if is_chunk:
        # Chunk-specific prompt - aware that this is partial content
        system_prompt = """You are an expert content analyst processing a SECTION of a larger document. Your job is to extract and summarize the key information from THIS SECTION ONLY.

Important guidelines for chunk processing:
1. Do NOT write introductions or conclusions - this is a partial document
2. Focus on extracting ALL key facts, figures, data points, and insights from this section
3. Preserve important quotes, code snippets, and specific details verbatim
4. Use bullet points and structured formatting for easy synthesis later
5. Note any references to other sections (e.g., "as mentioned earlier", "see below") without trying to resolve them

Your output will be combined with summaries of other sections, so focus on thorough extraction rather than narrative flow."""

        user_prompt = f"""Extract key information from this SECTION of a larger document:

{context_str}{chunk_info}

SECTION CONTENT:
{content}

Extract all important information from this section in a structured format. Focus on facts, data, insights, and key details. Do not add introductions or conclusions."""

    else:
        # Standard full-document prompt
        system_prompt = """You are an expert content analyst. Your job is to process web content and create a comprehensive yet concise summary that preserves all important information while dramatically reducing bulk.

Create a well-structured markdown summary that includes:
1. Key excerpts (quotes, code snippets, important facts) in their original format
2. Comprehensive summary of all other important information
3. Proper markdown formatting with headers, bullets, and emphasis

Your goal is to preserve ALL important information while reducing length. Never lose key facts, figures, insights, or actionable information. Make it scannable and well-organized."""

        user_prompt = f"""Please process this web content and create a comprehensive markdown summary:

{context_str}CONTENT TO PROCESS:
{content}

Create a markdown summary that captures all key information in a well-organized, scannable format. Include important quotes and code snippets in their original formatting. Focus on actionable information, specific details, and unique insights."""

    # Call the LLM with retry logic
    max_retries = 6
    retry_delay = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            call_kwargs = {
                "task": "web_extract",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
            }
            if model:
                call_kwargs["model"] = model
            response = await async_call_llm(**call_kwargs)
            return response.choices[0].message.content.strip()
        except RuntimeError:
            logger.warning("No auxiliary model available for web content processing")
            return None
        except Exception as api_error:
            last_error = api_error
            if attempt < max_retries - 1:
                logger.warning("LLM API call failed (attempt %d/%d): %s", attempt + 1, max_retries, str(api_error)[:100])
                logger.warning("Retrying in %ds...", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                raise last_error
    
    return None


async def _process_large_content_chunked(
    content: str, 
    context_str: str, 
    model: str, 
    chunk_size: int,
    max_output_size: int
) -> Optional[str]:
    """
    Process large content by chunking, summarizing each chunk in parallel,
    then synthesizing the summaries.
    
    Args:
        content: The large content to process
        context_str: Context information
        model: Model to use
        chunk_size: Size of each chunk in characters
        max_output_size: Maximum final output size
        
    Returns:
        Synthesized summary or None on failure
    """
    # Split content into chunks
    chunks = []
    for i in range(0, len(content), chunk_size):
        chunk = content[i:i + chunk_size]
        chunks.append(chunk)
    
    logger.info("Split into %d chunks of ~%d chars each", len(chunks), chunk_size)
    
    # Summarize each chunk in parallel
    async def summarize_chunk(chunk_idx: int, chunk_content: str) -> tuple[int, Optional[str]]:
        """Summarize a single chunk."""
        try:
            chunk_info = f"[Processing chunk {chunk_idx + 1} of {len(chunks)}]"
            summary = await _call_summarizer_llm(
                chunk_content, 
                context_str, 
                model, 
                max_tokens=10000,
                is_chunk=True,
                chunk_info=chunk_info
            )
            if summary:
                logger.info("Chunk %d/%d summarized: %d -> %d chars", chunk_idx + 1, len(chunks), len(chunk_content), len(summary))
            return chunk_idx, summary
        except Exception as e:
            logger.warning("Chunk %d/%d failed: %s", chunk_idx + 1, len(chunks), str(e)[:50])
            return chunk_idx, None
    
    # Run all chunk summarizations in parallel
    tasks = [summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks)
    
    # Collect successful summaries in order
    summaries = []
    for chunk_idx, summary in sorted(results, key=lambda x: x[0]):
        if summary:
            summaries.append(f"## Section {chunk_idx + 1}\n{summary}")
    
    if not summaries:
        logger.debug("All chunk summarizations failed")
        return "[Failed to process large content: all chunk summarizations failed]"
    
    logger.info("Got %d/%d chunk summaries", len(summaries), len(chunks))
    
    # If only one chunk succeeded, just return it (with cap)
    if len(summaries) == 1:
        result = summaries[0]
        if len(result) > max_output_size:
            result = result[:max_output_size] + "\n\n[... truncated ...]"
        return result
    
    # Synthesize the summaries into a final summary
    logger.info("Synthesizing %d summaries...", len(summaries))
    
    combined_summaries = "\n\n---\n\n".join(summaries)
    
    synthesis_prompt = f"""You have been given summaries of different sections of a large document. 
Synthesize these into ONE cohesive, comprehensive summary that:
1. Removes redundancy between sections
2. Preserves all key facts, figures, and actionable information
3. Is well-organized with clear structure
4. Is under {max_output_size} characters

{context_str}SECTION SUMMARIES:
{combined_summaries}

Create a single, unified markdown summary."""

    try:
        call_kwargs = {
            "task": "web_extract",
            "messages": [
                {"role": "system", "content": "You synthesize multiple summaries into one cohesive, comprehensive summary. Be thorough but concise."},
                {"role": "user", "content": synthesis_prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 20000,
        }
        if model:
            call_kwargs["model"] = model
        response = await async_call_llm(**call_kwargs)
        final_summary = response.choices[0].message.content.strip()
        
        # Enforce hard cap
        if len(final_summary) > max_output_size:
            final_summary = final_summary[:max_output_size] + "\n\n[... summary truncated for context management ...]"
        
        original_len = len(content)
        final_len = len(final_summary)
        compression = final_len / original_len if original_len > 0 else 1.0
        
        logger.info("Synthesis complete: %d -> %d chars (%.2f%%)", original_len, final_len, compression * 100)
        return final_summary
        
    except Exception as e:
        logger.warning("Synthesis failed: %s", str(e)[:100])
        # Fall back to concatenated summaries with truncation
        fallback = "\n\n".join(summaries)
        if len(fallback) > max_output_size:
            fallback = fallback[:max_output_size] + "\n\n[... truncated due to synthesis failure ...]"
        return fallback


def clean_base64_images(text: str) -> str:
    """
    Remove base64 encoded images from text to reduce token count and clutter.
    
    This function finds and removes base64 encoded images in various formats:
    - (data:image/png;base64,...)
    - (data:image/jpeg;base64,...)
    - (data:image/svg+xml;base64,...)
    - data:image/[type];base64,... (without parentheses)
    
    Args:
        text: The text content to clean
        
    Returns:
        Cleaned text with base64 images replaced with placeholders
    """
    # Pattern to match base64 encoded images wrapped in parentheses
    # Matches: (data:image/[type];base64,[base64-string])
    base64_with_parens_pattern = r'\(data:image/[^;]+;base64,[A-Za-z0-9+/=]+\)'
    
    # Pattern to match base64 encoded images without parentheses
    # Matches: data:image/[type];base64,[base64-string]
    base64_pattern = r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+'
    
    # Replace parentheses-wrapped images first
    cleaned_text = re.sub(base64_with_parens_pattern, '[BASE64_IMAGE_REMOVED]', text)
    
    # Then replace any remaining non-parentheses images
    cleaned_text = re.sub(base64_pattern, '[BASE64_IMAGE_REMOVED]', cleaned_text)
    
    return cleaned_text


def web_search_tool(query: str, limit: int = 5) -> str:
    """
    Search the web for information using available search API backend.
    
    This function provides a generic interface for web search that can work
    with multiple backends. Currently uses Firecrawl.
    
    Note: This function returns search result metadata only (URLs, titles, descriptions).
    Use web_extract_tool to get full content from specific URLs.
    
    Args:
        query (str): The search query to look up
        limit (int): Maximum number of results to return (default: 5)
    
    Returns:
        str: JSON string containing search results with the following structure:
             {
                 "success": bool,
                 "data": {
                     "web": [
                         {
                             "title": str,
                             "url": str,
                             "description": str,
                             "position": int
                         },
                         ...
                     ]
                 }
             }
    
    Raises:
        Exception: If search fails or API key is not set
    """
    debug_call_data = {
        "parameters": {
            "query": query,
            "limit": limit
        },
        "backend": None,
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0
    }
    
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return json.dumps({"error": "Interrupted", "success": False})

        logger.info("Searching the web for: '%s' (limit: %d)", query, limit)

        backend = _get_active_web_backend()
        debug_call_data["backend"] = backend

        if backend == "firecrawl":
            response = _get_firecrawl_client().search(query=query, limit=limit)
            web_results = _normalize_firecrawl_search_results(response)
        else:
            response = _tavily_request(
                "/search",
                {
                    "query": query,
                    "max_results": limit,
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            web_results = _normalize_tavily_search_results(response)

        results_count = len(web_results)
        logger.info("Found %d search results", results_count)
        
        # Build response with just search metadata (URLs, titles, descriptions)
        response_data = {
            "success": True,
            "data": {
                "web": web_results
            }
        }
        
        # Capture debug information
        debug_call_data["results_count"] = results_count
        
        # Convert to JSON
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        
        debug_call_data["final_response_size"] = len(result_json)
        
        # Log debug information
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        
        return result_json
        
    except Exception as e:
        error_msg = f"Error searching web: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        
        return json.dumps({"error": error_msg}, ensure_ascii=False)


async def web_extract_tool(
    urls: List[str], 
    format: str = None, 
    use_llm_processing: bool = True,
    model: str = DEFAULT_SUMMARIZER_MODEL,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> str:
    """
    Extract content from specific web pages using available extraction API backend.
    
    This function provides a generic interface for web content extraction that
    can work with multiple backends. Currently uses Firecrawl.
    
    Args:
        urls (List[str]): List of URLs to extract content from
        format (str): Desired output format ("markdown" or "html", optional)
        use_llm_processing (bool): Whether to process content with LLM for summarization (default: True)
        model (str): The model to use for LLM processing (default: google/gemini-3-flash-preview)
        min_length (int): Minimum content length to trigger LLM processing (default: 5000)
    
    Returns:
        str: JSON string containing extracted content. If LLM processing is enabled and successful,
             the 'content' field will contain the processed markdown summary instead of raw content.
    
    Raises:
        Exception: If extraction fails or API key is not set
    """
    debug_call_data = {
        "parameters": {
            "urls": urls,
            "format": format,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length
        },
        "backend": None,
        "error": None,
        "pages_extracted": 0,
        "pages_processed_with_llm": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "compression_metrics": [],
        "processing_applied": []
    }
    
    try:
        logger.info("Extracting content from %d URL(s)", len(urls))
        backend: Optional[str] = None
        
        # Determine requested formats for Firecrawl v2
        formats: List[str] = []
        if format == "markdown":
            formats = ["markdown"]
        elif format == "html":
            formats = ["html"]
        else:
            # Default: request markdown for LLM-readiness and include html as backup
            formats = ["markdown", "html"]
        
        # Always use individual scraping for simplicity and reliability
        # Batch scraping adds complexity without much benefit for small numbers of URLs
        results: List[Dict[str, Any]] = []
        
        from tools.interrupt import is_interrupted as _is_interrupted
        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "error": "Interrupted", "title": ""})
                continue

            # Website policy check — block before fetching
            try:
                blocked = check_website_access(url)
            except WebsitePolicyError as policy_err:
                results.append({"url": url, "title": "", "content": "", "error": f"Website policy error: {policy_err}"})
                continue
            if blocked:
                logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
                results.append({
                    "url": url, "title": "", "content": "",
                    "error": blocked["message"],
                    "blocked_by_policy": {"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]},
                })
                continue

            try:
                if backend is None:
                    backend = _get_active_web_backend()
                    debug_call_data["backend"] = backend
                logger.info("Scraping: %s", url)

                if backend == "firecrawl":
                    scrape_result = _get_firecrawl_client().scrape(url=url, formats=formats)
                    result_dict = _to_dict(scrape_result)
                    metadata = result_dict.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = _to_dict(metadata)
                    title = metadata.get("title", "")
                    content_markdown = result_dict.get("markdown")
                    content_html = result_dict.get("html")
                    normalized_results = [
                        {
                            "url": metadata.get("sourceURL", url),
                            "title": title,
                            "content": content_markdown if (format == "markdown" or (format is None and content_markdown)) else content_html or content_markdown or "",
                            "raw_content": content_markdown if (format == "markdown" or (format is None and content_markdown)) else content_html or content_markdown or "",
                            "metadata": metadata,
                        }
                    ]
                else:
                    extract_result = _tavily_request(
                        "/extract",
                        {
                            "urls": [url],
                            "include_images": False,
                        },
                    )
                    normalized_results = _normalize_tavily_documents(extract_result, fallback_url=url)
                    if not normalized_results:
                        normalized_results = [{
                            "url": url,
                            "title": "",
                            "content": "",
                            "raw_content": "",
                            "error": "No content returned",
                        }]

                for normalized in normalized_results:
                    final_url = normalized.get("url", url)
                    title = normalized.get("title", "")
                    try:
                        final_blocked = check_website_access(final_url)
                    except WebsitePolicyError:
                        final_blocked = None
                    if final_blocked:
                        logger.info("Blocked redirected web_extract for %s by rule %s", final_blocked["host"], final_blocked["rule"])
                        results.append({
                            "url": final_url,
                            "title": title,
                            "content": "",
                            "raw_content": "",
                            "error": final_blocked["message"],
                            "blocked_by_policy": {"host": final_blocked["host"], "rule": final_blocked["rule"], "source": final_blocked["source"]},
                        })
                        continue
                    results.append(normalized)
                
            except Exception as scrape_err:
                logger.debug("Scrape failed for %s: %s", url, scrape_err)
                results.append({
                    "url": url,
                    "title": "",
                    "content": "",
                    "raw_content": "",
                    "error": str(scrape_err)
                })

        response = {"results": results}
        
        pages_extracted = len(response.get('results', []))
        logger.info("Extracted content from %d pages", pages_extracted)
        
        debug_call_data["pages_extracted"] = pages_extracted
        debug_call_data["original_response_size"] = len(json.dumps(response))
        
        # Process each result with LLM if enabled
        if use_llm_processing:
            logger.info("Processing extracted content with LLM (parallel)...")
            debug_call_data["processing_applied"].append("llm_processing")
            
            # Prepare tasks for parallel processing
            async def process_single_result(result):
                """Process a single result with LLM and return updated result with metrics."""
                url = result.get('url', 'Unknown URL')
                title = result.get('title', '')
                raw_content = result.get('raw_content', '') or result.get('content', '')
                
                if not raw_content:
                    return result, None, "no_content"
                
                original_size = len(raw_content)
                
                # Process content with LLM
                processed = await process_content_with_llm(
                    raw_content, url, title, model, min_length
                )
                
                if processed:
                    processed_size = len(processed)
                    compression_ratio = processed_size / original_size if original_size > 0 else 1.0
                    
                    # Update result with processed content
                    result['content'] = processed
                    result['raw_content'] = raw_content
                    
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": processed_size,
                        "compression_ratio": compression_ratio,
                        "model_used": model
                    }
                    return result, metrics, "processed"
                else:
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": original_size,
                        "compression_ratio": 1.0,
                        "model_used": None,
                        "reason": "content_too_short"
                    }
                    return result, metrics, "too_short"
            
            # Run all LLM processing in parallel
            results_list = response.get('results', [])
            tasks = [process_single_result(result) for result in results_list]
            processed_results = await asyncio.gather(*tasks)
            
            # Collect metrics and print results
            for result, metrics, status in processed_results:
                url = result.get('url', 'Unknown URL')
                if status == "processed":
                    debug_call_data["compression_metrics"].append(metrics)
                    debug_call_data["pages_processed_with_llm"] += 1
                    logger.info("%s (processed)", url)
                elif status == "too_short":
                    debug_call_data["compression_metrics"].append(metrics)
                    logger.info("%s (no processing - content too short)", url)
                else:
                    logger.warning("%s (no content to process)", url)
        else:
            # Print summary of extracted pages for debugging (original behavior)
            for result in response.get('results', []):
                url = result.get('url', 'Unknown URL')
                content_length = len(result.get('raw_content', ''))
                logger.info("%s (%d characters)", url, content_length)
        
        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "error": r.get("error"),
                **({  "blocked_by_policy": r["blocked_by_policy"]} if "blocked_by_policy" in r else {}),
            }
            for r in response.get("results", [])
        ]
        trimmed_response = {"results": trimmed_results}

        if trimmed_response.get("results") == []:
            result_json = json.dumps({"error": "Content was inaccessible or not found"}, ensure_ascii=False)

            cleaned_result = clean_base64_images(result_json)
        
        else:
            result_json = json.dumps(trimmed_response, indent=2, ensure_ascii=False)
            
            cleaned_result = clean_base64_images(result_json)
        
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_removal")
        
        # Log debug information
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return cleaned_result
            
    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return json.dumps({"error": error_msg}, ensure_ascii=False)


async def web_crawl_tool(
    url: str, 
    instructions: str = None, 
    depth: str = "basic", 
    use_llm_processing: bool = True,
    model: str = DEFAULT_SUMMARIZER_MODEL,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> str:
    """
    Crawl a website with specific instructions using available crawling API backend.
    
    This function provides a generic interface for web crawling that can work
    with multiple backends. Currently uses Firecrawl.
    
    Args:
        url (str): The base URL to crawl (can include or exclude https://)
        instructions (str): Instructions for what to crawl/extract using LLM intelligence (optional)
        depth (str): Depth of extraction ("basic" or "advanced", default: "basic")
        use_llm_processing (bool): Whether to process content with LLM for summarization (default: True)
        model (str): The model to use for LLM processing (default: google/gemini-3-flash-preview)
        min_length (int): Minimum content length to trigger LLM processing (default: 5000)
    
    Returns:
        str: JSON string containing crawled content. If LLM processing is enabled and successful,
             the 'content' field will contain the processed markdown summary instead of raw content.
             Each page is processed individually.
    
    Raises:
        Exception: If crawling fails or API key is not set
    """
    debug_call_data = {
        "parameters": {
            "url": url,
            "instructions": instructions,
            "depth": depth,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length
        },
        "backend": None,
        "error": None,
        "pages_crawled": 0,
        "pages_processed_with_llm": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "compression_metrics": [],
        "processing_applied": []
    }
    
    try:
        # Ensure URL has protocol
        if not url.startswith(('http://', 'https://')):
            url = f'https://{url}'
            logger.info("Added https:// prefix to URL: %s", url)
        
        instructions_text = f" with instructions: '{instructions}'" if instructions else ""
        logger.info("Crawling %s%s", url, instructions_text)
        
        # Website policy check — block before crawling
        try:
            blocked = check_website_access(url)
        except WebsitePolicyError as policy_err:
            return json.dumps({"results": [{"url": url, "title": "", "content": "", "error": f"Website policy error: {policy_err}"}]}, ensure_ascii=False)
        if blocked:
            logger.info("Blocked web_crawl for %s by rule %s", blocked["host"], blocked["rule"])
            return json.dumps({"results": [{"url": url, "title": "", "content": "", "error": blocked["message"],
                "blocked_by_policy": {"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]}}]}, ensure_ascii=False)

        from tools.interrupt import is_interrupted as _is_int
        if _is_int():
            return json.dumps({"error": "Interrupted", "success": False})

        backend = _get_active_web_backend()
        debug_call_data["backend"] = backend

        pages: List[Dict[str, Any]] = []

        if backend == "firecrawl":
            crawl_params = {
                "limit": 20,
                "scrape_options": {"formats": ["markdown"]},
            }
            if instructions:
                logger.info("Instructions parameter ignored (not supported in crawl API)")

            try:
                crawl_result = _get_firecrawl_client().crawl(url=url, **crawl_params)
            except Exception as e:
                logger.debug("Crawl API call failed: %s", e)
                raise

            data_list = []
            if hasattr(crawl_result, 'data'):
                data_list = crawl_result.data if crawl_result.data else []
                logger.info("Status: %s", getattr(crawl_result, 'status', 'unknown'))
                logger.info("Retrieved %d pages", len(data_list))
                if not data_list:
                    logger.debug("CrawlJob attributes: %s", [attr for attr in dir(crawl_result) if not attr.startswith('_')])
                    logger.debug("Status: %s", getattr(crawl_result, 'status', 'N/A'))
                    logger.debug("Total: %s", getattr(crawl_result, 'total', 'N/A'))
                    logger.debug("Completed: %s", getattr(crawl_result, 'completed', 'N/A'))
            elif isinstance(crawl_result, dict) and 'data' in crawl_result:
                data_list = crawl_result.get("data", [])
            else:
                logger.warning("Unexpected crawl result type")
                logger.debug("Result type: %s", type(crawl_result))
                if hasattr(crawl_result, '__dict__'):
                    logger.debug("Result attributes: %s", list(crawl_result.__dict__.keys()))

            for item in data_list:
                item_dict = _to_dict(item)
                metadata = item_dict.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = _to_dict(metadata)

                page_url = metadata.get("sourceURL", metadata.get("url", "Unknown URL"))
                title = metadata.get("title", "")
                content = item_dict.get("markdown") or item_dict.get("html") or ""

                try:
                    page_blocked = check_website_access(page_url)
                except WebsitePolicyError:
                    page_blocked = None
                if page_blocked:
                    logger.info("Blocked crawled page %s by rule %s", page_blocked["host"], page_blocked["rule"])
                    pages.append({
                        "url": page_url,
                        "title": title,
                        "content": "",
                        "raw_content": "",
                        "error": page_blocked["message"],
                        "blocked_by_policy": {"host": page_blocked["host"], "rule": page_blocked["rule"], "source": page_blocked["source"]},
                    })
                    continue

                pages.append({
                    "url": page_url,
                    "title": title,
                    "content": content,
                    "raw_content": content,
                    "metadata": metadata,
                })
        else:
            crawl_payload = {
                "url": url,
                "limit": 20,
                "extract_depth": depth,
            }
            if instructions:
                crawl_payload["instructions"] = instructions

            for page in _normalize_tavily_documents(_tavily_request("/crawl", crawl_payload), fallback_url=url):
                page_url = page.get("url", url)
                try:
                    page_blocked = check_website_access(page_url)
                except WebsitePolicyError:
                    page_blocked = None
                if page_blocked:
                    pages.append({
                        "url": page_url,
                        "title": page.get("title", ""),
                        "content": "",
                        "raw_content": "",
                        "error": page_blocked["message"],
                        "blocked_by_policy": {"host": page_blocked["host"], "rule": page_blocked["rule"], "source": page_blocked["source"]},
                    })
                    continue
                pages.append(page)

        response = {"results": pages}
        
        pages_crawled = len(response.get('results', []))
        logger.info("Crawled %d pages", pages_crawled)
        
        debug_call_data["pages_crawled"] = pages_crawled
        debug_call_data["original_response_size"] = len(json.dumps(response))
        
        # Process each result with LLM if enabled
        if use_llm_processing:
            logger.info("Processing crawled content with LLM (parallel)...")
            debug_call_data["processing_applied"].append("llm_processing")
            
            # Prepare tasks for parallel processing
            async def process_single_crawl_result(result):
                """Process a single crawl result with LLM and return updated result with metrics."""
                page_url = result.get('url', 'Unknown URL')
                title = result.get('title', '')
                content = result.get('content', '')
                
                if not content:
                    return result, None, "no_content"
                
                original_size = len(content)
                
                # Process content with LLM
                processed = await process_content_with_llm(
                    content, page_url, title, model, min_length
                )
                
                if processed:
                    processed_size = len(processed)
                    compression_ratio = processed_size / original_size if original_size > 0 else 1.0
                    
                    # Update result with processed content
                    result['raw_content'] = content
                    result['content'] = processed
                    
                    metrics = {
                        "url": page_url,
                        "original_size": original_size,
                        "processed_size": processed_size,
                        "compression_ratio": compression_ratio,
                        "model_used": model
                    }
                    return result, metrics, "processed"
                else:
                    metrics = {
                        "url": page_url,
                        "original_size": original_size,
                        "processed_size": original_size,
                        "compression_ratio": 1.0,
                        "model_used": None,
                        "reason": "content_too_short"
                    }
                    return result, metrics, "too_short"
            
            # Run all LLM processing in parallel
            results_list = response.get('results', [])
            tasks = [process_single_crawl_result(result) for result in results_list]
            processed_results = await asyncio.gather(*tasks)
            
            # Collect metrics and print results
            for result, metrics, status in processed_results:
                page_url = result.get('url', 'Unknown URL')
                if status == "processed":
                    debug_call_data["compression_metrics"].append(metrics)
                    debug_call_data["pages_processed_with_llm"] += 1
                    logger.info("%s (processed)", page_url)
                elif status == "too_short":
                    debug_call_data["compression_metrics"].append(metrics)
                    logger.info("%s (no processing - content too short)", page_url)
                else:
                    logger.warning("%s (no content to process)", page_url)
        else:
            # Print summary of crawled pages for debugging (original behavior)
            for result in response.get('results', []):
                page_url = result.get('url', 'Unknown URL')
                content_length = len(result.get('content', ''))
                logger.info("%s (%d characters)", page_url, content_length)
        
        # Trim output to minimal fields per entry: title, content, error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "error": r.get("error"),
                **({  "blocked_by_policy": r["blocked_by_policy"]} if "blocked_by_policy" in r else {}),
            }
            for r in response.get("results", [])
        ]
        trimmed_response = {"results": trimmed_results}
        
        result_json = json.dumps(trimmed_response, indent=2, ensure_ascii=False)
        # Clean base64 images from crawled content
        cleaned_result = clean_base64_images(result_json)
        
        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_removal")
        
        # Log debug information
        _debug.log_call("web_crawl_tool", debug_call_data)
        _debug.save()
        
        return cleaned_result
        
    except Exception as e:
        error_msg = f"Error crawling website: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_crawl_tool", debug_call_data)
        _debug.save()
        
        return json.dumps({"error": error_msg}, ensure_ascii=False)


# Convenience function to check if any supported web backend is available.
def check_firecrawl_api_key() -> bool:
    """
    Check if a supported web backend is configured.

    Firecrawl remains the preferred backend when present, but Tavily-only
    setups should also expose the web tools as available.
    """
    return _has_firecrawl_config() or _has_tavily_config()


def check_auxiliary_model() -> bool:
    """Check if an auxiliary text model is available for LLM content processing."""
    try:
        from agent.auxiliary_client import resolve_provider_client
        for p in ("openrouter", "nous", "custom", "codex"):
            client, _ = resolve_provider_client(p)
            if client is not None:
                return True
        return False
    except Exception:
        return False


def get_debug_session_info() -> Dict[str, Any]:
    """Get information about the current debug session."""
    return _debug.get_session_info()


if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)
    
    # Check if API keys are available
    firecrawl_available = check_firecrawl_api_key()
    nous_available = check_auxiliary_model()

    if not firecrawl_available:
        print("❌ No supported web backend configured")
        print("Set FIRECRAWL_API_KEY / FIRECRAWL_API_URL or TAVILY_API_KEY")
    else:
        print(f"✅ Web backend configured: {_get_active_web_backend()}")
    
    if not nous_available:
        print("❌ No auxiliary model available for LLM content processing")
        print("Set OPENROUTER_API_KEY, configure Nous Portal, or set OPENAI_BASE_URL + OPENAI_API_KEY")
        print("⚠️  Without an auxiliary model, LLM content processing will be disabled")
    else:
        print(f"✅ Auxiliary model available: {DEFAULT_SUMMARIZER_MODEL}")
    
    if not firecrawl_available:
        exit(1)
    
    print("🛠️  Web tools ready for use!")
    
    if nous_available:
        print(f"🧠 LLM content processing available with {DEFAULT_SUMMARIZER_MODEL}")
        print(f"   Default min length for processing: {DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION} chars")
    
    # Show debug mode status
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")
    
    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool, web_crawl_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract and crawl (asynchronous)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("      crawl_data = await web_crawl_tool('example.com', 'Find docs')")
    print("  asyncio.run(main())")
    
    if nous_available:
        print("\nLLM-enhanced usage:")
        print("  # Content automatically processed for pages >5000 chars (default)")
        print("  content = await web_extract_tool(['https://python.org/about/'])")
        print("")
        print("  # Customize processing parameters")
        print("  crawl_data = await web_crawl_tool(")
        print("      'docs.python.org',")
        print("      'Find key concepts',")
        print("      model='google/gemini-3-flash-preview',")
        print("      min_length=3000")
        print("  )")
        print("")
        print("  # Disable LLM processing")
        print("  raw_content = await web_extract_tool(['https://example.com'], use_llm_processing=False)")
    
    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Debug logs capture:")
    print("  # - All tool calls with parameters")
    print("  # - Original API responses")
    print("  # - LLM compression metrics")
    print("  # - Final processed results")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")
    
    print(f"\n📝 Run 'python test_web_tools_llm.py' to test LLM processing capabilities")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information on any topic. Returns up to 5 relevant results with titles, URLs, and descriptions.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web"
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns page content in markdown format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass the PDF link directly and it converts to markdown text. Pages under 5000 chars return full markdown; larger pages are LLM-summarized and capped at ~5000 chars per page. Pages over 2M chars are refused. If a URL fails or times out, use the browser tool to access it instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=5),
    check_fn=check_firecrawl_api_key,
    requires_env=["FIRECRAWL_API_KEY", "TAVILY_API_KEY"],
    emoji="🔍",
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown"),
    check_fn=check_firecrawl_api_key,
    requires_env=["FIRECRAWL_API_KEY", "TAVILY_API_KEY"],
    is_async=True,
    emoji="📄",
)
