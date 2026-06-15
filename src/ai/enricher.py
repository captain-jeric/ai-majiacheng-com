"""Content enrichment using AI (second-pass analysis).

For items that pass the score threshold, this module:
1. Searches the web for relevant context (via DuckDuckGo)
2. Feeds search results + item content to AI to generate grounded background knowledge
"""

import asyncio
import json
import re
import sys
import os
from typing import List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from ddgs import DDGS

from .client import AIClient
from .prompts import (
    CONCEPT_EXTRACTION_SYSTEM, CONCEPT_EXTRACTION_USER,
    CONTENT_ENRICHMENT_SYSTEM, CONTENT_ENRICHMENT_USER,
)
from .utils import parse_json_response
from ..models import ContentItem


ZH_ONLY_ENRICHMENT_SYSTEM = """You are a knowledgeable AI news editor writing for a Chinese technical audience.

Given a high-scoring news item, its content, and optional web search results, produce a concise grounded Chinese analysis.

Rules:
- Write all text fields in Simplified Chinese.
- Keep technical names and widely-used acronyms in English when natural.
- Do not fabricate facts.
- Use only information from the provided content and search results.
- Return valid JSON only.
- For sources, include 0-3 URLs only if they appear verbatim in the search results and are directly relevant; otherwise return [].
"""

ZH_ONLY_ENRICHMENT_USER = """请为下面这条 AI 新闻生成中文分析。

新闻：
- 标题：{title}
- URL：{url}
- 一句话摘要：{summary}
- 评分：{score}/10
- 评分原因：{reason}
- 标签：{tags}

正文：
{content}
{comments_section}

搜索结果：
{web_context}

只返回 JSON：
{{
  "title_zh": "<中文短标题，不超过15个词>",
  "whats_new_zh": "<1-2句话：具体发生了什么>",
  "why_it_matters_zh": "<1-2句话：为什么重要，影响谁>",
  "key_details_zh": "<1-2句话：关键技术细节、限制或背景>",
  "background_zh": "<2-3句话：必要背景；如果不需要则为空字符串>",
  "community_discussion_zh": "<1-2句话：如果有社区讨论则总结，否则为空字符串>",
  "sources": ["<搜索结果中的URL>", "..."]
}}"""


class ContentEnricher:
    """Enriches high-scoring content items with background knowledge."""

    def __init__(self, ai_client: AIClient):
        self.client = ai_client

    def _get_concurrency(self) -> int:
        """Return the configured enrichment concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "enrichment_concurrency", 1)
        return max(concurrency, 1)

    def _is_zh_only(self) -> bool:
        config = getattr(self.client, "config", None)
        languages = getattr(config, "languages", []) or []
        return set(languages) == {"zh"}

    async def enrich_batch(self, items: List[ContentItem]) -> None:
        """Enrich items in-place with background knowledge.

        Args:
            items: Content items to enrich (modified in-place)
        """
        concurrency = self._get_concurrency()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ContentItem, progress_task) -> None:
            async with semaphore:
                try:
                    await self._enrich_item(item)
                except Exception as e:
                    print(f"Error enriching item {item.id}: {e}, falling back to translation")
                    await self._translate_item(item)
            progress.advance(progress_task)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task("Enriching", total=len(items))
            coros = [
                _process(item, task) for item in items
            ]
            await asyncio.gather(*coros)

    async def _web_search(self, query: str, max_results: int = 3) -> list:
        """Search the web for context via DuckDuckGo.

        Returns:
            List of dicts with keys: title, url, body
        """
        try:
            # Suppress primp "Impersonate ... does not exist" stderr warning
            stderr = sys.stderr
            sys.stderr = open(os.devnull, "w")
            try:
                ddgs = DDGS()
                results = await asyncio.to_thread(ddgs.text, query, max_results=max_results)
            finally:
                sys.stderr.close()
                sys.stderr = stderr
        except Exception:
            return []

        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "body": r.get("body", "")}
            for r in (results or [])
        ]

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    async def _extract_concepts(self, item: ContentItem, content_text: str) -> List[str]:
        """Ask AI to identify concepts that need explanation.

        Args:
            item: Content item
            content_text: Extracted content text

        Returns:
            List of search queries for concepts that need explanation
        """
        user_prompt = CONCEPT_EXTRACTION_USER.format(
            title=item.title,
            summary=item.ai_summary or item.title,
            tags=", ".join(item.ai_tags) if item.ai_tags else "",
            content=content_text[:700],
        )

        try:
            response = await self.client.complete(
                system=CONCEPT_EXTRACTION_SYSTEM,
                user=user_prompt,
                max_tokens=256,
            )
            result = self._parse_json_response(response)
            if result is None:
                return []
            queries = result.get("queries", [])
            return queries[:2]
        except Exception:
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _enrich_item(self, item: ContentItem) -> None:
        """Enrich a single item with background knowledge.

        Steps:
        1. Ask AI which concepts in the news need explanation
        2. Search the web for those concepts
        3. Ask AI to generate background based on search results

        Args:
            item: Content item to enrich (modified in-place via metadata)
        """
        # Extract content text and comments separately
        content_text = ""
        comments_text = ""
        if item.content:
            if "--- Top Comments ---" in item.content:
                main, comments_part = item.content.split("--- Top Comments ---", 1)
                content_text = main.strip()[:2200]
                comments_text = comments_part.strip()[:600]
            else:
                content_text = item.content[:2200]

        # Step 1: AI identifies concepts to explain
        queries = await self._extract_concepts(item, content_text)

        # Step 2: Search web for each concept
        all_results = []
        web_sections = []
        for query in queries:
            results = await self._web_search(query)
            all_results.extend(results)
            if results:
                lines = [f"- [{r['title']}]({r['url']}): {r['body']}" for r in results]
                web_sections.append(f"**{query}:**\n" + "\n".join(lines))
        web_context = "\n\n".join(web_sections) if web_sections else ""

        # Index of available URLs for citation validation
        available_urls = {r["url"]: r["title"] for r in all_results if r.get("url")}

        # Step 3: AI generates background grounded in search results
        if self._is_zh_only():
            system_prompt = ZH_ONLY_ENRICHMENT_SYSTEM
            user_prompt = ZH_ONLY_ENRICHMENT_USER.format(
                title=item.title,
                url=str(item.url),
                summary=item.ai_summary or item.title,
                score=item.ai_score or 0,
                reason=item.ai_reason or "",
                tags=", ".join(item.ai_tags) if item.ai_tags else "",
                content=content_text,
                comments_section=f"\n社区讨论：\n{comments_text}" if comments_text else "",
                web_context=web_context or "无可用搜索结果。",
            )
            max_tokens = 1536
        else:
            system_prompt = CONTENT_ENRICHMENT_SYSTEM
            user_prompt = CONTENT_ENRICHMENT_USER.format(
                title=item.title,
                url=str(item.url),
                summary=item.ai_summary or item.title,
                score=item.ai_score or 0,
                reason=item.ai_reason or "",
                tags=", ".join(item.ai_tags) if item.ai_tags else "",
                content=content_text,
                comments_section=f"\n**Community Comments:**\n{comments_text}" if comments_text else "",
                web_context=web_context or "No web search results available.",
            )
            max_tokens = 2048

        response = await self.client.complete(
            system=system_prompt,
            user=user_prompt,
            max_tokens=max_tokens,
        )

        # Parse JSON response with robust fallback
        result = self._parse_json_response(response)
        if result is None:
            # Gracefully degrade: fall back to a lightweight translation
            # instead of dropping the item untranslated.
            print(f"Warning: could not parse enrichment response for {item.id}, falling back to translation")
            await self._translate_item(item)
            return

        # Combine structured sub-fields into per-language detailed_summary
        for lang in ("en", "zh"):
            if result.get(f"title_{lang}"):
                val = result[f"title_{lang}"]
                item.metadata[f"title_{lang}"] = val.get("text") or str(val) if isinstance(val, dict) else str(val)

            parts = []
            for field in ("whats_new", "why_it_matters", "key_details"):
                text = result.get(f"{field}_{lang}", "").strip()
                if text:
                    parts.append(text)
            if parts:
                item.metadata[f"detailed_summary_{lang}"] = " ".join(parts)

            if result.get(f"background_{lang}"):
                val = result[f"background_{lang}"]
                item.metadata[f"background_{lang}"] = val.get("text") or str(val) if isinstance(val, dict) else str(val)

            if result.get(f"community_discussion_{lang}"):
                val = result[f"community_discussion_{lang}"]
                item.metadata[f"community_discussion_{lang}"] = val.get("text") or str(val) if isinstance(val, dict) else str(val)

        # Store citation sources — only URLs that actually came from our search results
        if result.get("sources") and available_urls:
            valid = [
                {"url": u, "title": available_urls[u]}
                for u in result["sources"]
                if u in available_urls
            ]
            if valid:
                item.metadata["sources"] = valid

        # Backward-compatible fallback fields (English as default)
        item.metadata["detailed_summary"] = item.metadata.get("detailed_summary_en", "")
        item.metadata["background"] = item.metadata.get("background_en", "")
        item.metadata["community_discussion"] = item.metadata.get("community_discussion_en", "")

    async def _translate_item(self, item: ContentItem) -> None:
        """Lightweight translation fallback: when full enrichment fails, at least
        translate the title and summary to Chinese so the item is not dropped."""
        try:
            response = await self.client.complete(
                system="You are a translator. Translate to Simplified Chinese. Return only valid JSON, no other text.",
                user=(
                    f'Title: {item.title}\n'
                    f'Summary: {item.ai_summary or item.title}\n\n'
                    'Return JSON:\n'
                    '{"title_zh": "<中文标题>", "summary_zh": "<用中文写1-2句摘要>"}'
                ),
                max_tokens=512,
            )
            result = self._parse_json_response(response)
            if result:
                if result.get("title_zh"):
                    item.metadata["title_zh"] = result["title_zh"]
                if result.get("summary_zh"):
                    item.metadata["detailed_summary_zh"] = result["summary_zh"]
        except Exception:
            pass
