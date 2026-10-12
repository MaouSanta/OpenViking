# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Search Service for OpenViking.

Provides semantic search operations: search, find.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from openviking.core.path_variables import resolve_path_variables
from openviking.core.uri_validation import validate_optional_viking_uris
from openviking.resource.atomic_bundle import find_resource_bundle_root
from openviking.server.identity import RequestContext
from openviking.storage.viking_fs import VikingFS
from openviking.utils.image_search import (
    image_bytes_to_data_uri,
    is_data_image_uri,
    is_http_url,
    is_viking_uri,
)
from openviking_cli.exceptions import InvalidArgumentError, NotInitializedError
from openviking_cli.utils import get_logger

if TYPE_CHECKING:
    from openviking.session import Session

logger = get_logger(__name__)


def _ensure_non_empty_query(query: str, image_url: Optional[str] = None) -> None:
    if not query.strip() and not image_url:
        raise InvalidArgumentError("Search query or image_url must not be empty.")


class SearchService:
    """Semantic search service."""

    def __init__(self, viking_fs: Optional[VikingFS] = None):
        self._viking_fs = viking_fs

    def set_viking_fs(self, viking_fs: VikingFS) -> None:
        """Set VikingFS instance (for deferred initialization)."""
        self._viking_fs = viking_fs

    def _ensure_initialized(self) -> VikingFS:
        """Ensure VikingFS is initialized."""
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")
        return self._viking_fs

    def is_intent_enabled(self) -> bool:
        """Whether search uses session context for LLM intent analysis.

        When false, callers should skip session.load / get_context_for_search:
        VikingFS.search ignores session_info and searches with the raw query.
        Default is True (matches RetrievalConfig) when config is unset.
        """
        if not self._viking_fs or self._viking_fs.retrieval_config is None:
            return True
        return bool(self._viking_fs.retrieval_config.enable_intent)

    async def _resolve_image_url(
        self,
        image_url: Optional[str],
        ctx: RequestContext,
    ) -> Optional[str]:
        if not image_url:
            return None
        image_url = resolve_path_variables(image_url)
        if is_viking_uri(image_url):
            viking_fs = self._ensure_initialized()
            content = await viking_fs.read_file_bytes(image_url, ctx=ctx)
            return image_bytes_to_data_uri(content, image_url)
        if is_data_image_uri(image_url) or is_http_url(image_url):
            return image_url
        raise InvalidArgumentError(
            "image_url must be a data:image base64 URI, http(s) URL, or viking:// URI."
        )

    async def search(
        self,
        query: str,
        ctx: RequestContext,
        target_uri: Union[str, List[str]] = "",
        session: Optional["Session"] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
        level: Optional[List[int]] = None,
        image_url: Optional[str] = None,
    ) -> Any:
        """Complex search with session context.

        Args:
            query: Query string
            target_uri: Target directory URI(s), supports str or List[str]
            session: Session object for context
            limit: Max results
            score_threshold: Score threshold
            filter: Metadata filters
            level: Filter by level (0=abstract, 1=overview, 2=file)

        Returns:
            FindResult
        """
        resolved_image_url = await self._resolve_image_url(image_url, ctx)
        _ensure_non_empty_query(query, resolved_image_url)
        target_uri = validate_optional_viking_uris(target_uri, field_name="target_uri")
        viking_fs = self._ensure_initialized()

        session_info = None
        # Intent off: session_info is unused by VikingFS — skip the archive/message scan.
        if session is not None and self.is_intent_enabled() and not resolved_image_url:
            session_info = await session.get_context_for_search(query)

        result = await viking_fs.search(
            query=query,
            ctx=ctx,
            target_uri=target_uri,
            session_info=session_info,
            limit=limit,
            score_threshold=score_threshold,
            filter=filter,
            level=level,
            image_url=resolved_image_url,
        )
        return result

    async def find(
        self,
        query: str,
        ctx: RequestContext,
        target_uri: Union[str, List[str]] = "",
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
        level: Optional[List[int]] = None,
        image_url: Optional[str] = None,
    ) -> Any:
        """Semantic search without session context.

        Args:
            query: Query string
            target_uri: Target directory URI(s), supports str or List[str]
            limit: Max results
            score_threshold: Score threshold
            filter: Metadata filters
            level: Filter by level (0=abstract, 1=overview, 2=file)

        Returns:
            FindResult
        """
        resolved_image_url = await self._resolve_image_url(image_url, ctx)
        _ensure_non_empty_query(query, resolved_image_url)
        target_uri = validate_optional_viking_uris(target_uri, field_name="target_uri")
        viking_fs = self._ensure_initialized()
        result = await viking_fs.find(
            query=query,
            ctx=ctx,
            target_uri=target_uri,
            limit=limit,
            score_threshold=score_threshold,
            filter=filter,
            level=level,
            image_url=resolved_image_url,
        )
        return result

    async def find_resource(
        self,
        query: str,
        ctx: RequestContext,
        target_uri: Union[str, List[str]] = "viking://resources",
        limit: int = 10,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Find bundles by their strongest matching indexed artifact."""
        if limit < 1:
            raise InvalidArgumentError("limit must be at least 1")
        _ensure_non_empty_query(query)
        resolved_target_uri = validate_optional_viking_uris(
            target_uri,
            field_name="target_uri",
            allowed_scopes="resources",
        )
        viking_fs = self._ensure_initialized()

        grouped: Dict[str, Dict[str, Any]] = {}
        root_cache: Dict[tuple[str, bool], Optional[str]] = {}
        candidate_limit = limit
        # ponytail: 100k child ceiling; move grouping into the vector backend if this is hit.
        max_candidates = 100_000
        while True:
            child_result = await viking_fs.find(
                query=query,
                ctx=ctx,
                target_uri=resolved_target_uri,
                limit=candidate_limit,
                score_threshold=score_threshold,
                filter=filter,
                level=[0, 1, 2],
            )
            for match in child_result.resources:
                matched_uri = str(match.uri)
                uri_is_directory = int(match.level) != 2
                cache_key = matched_uri, uri_is_directory
                if cache_key not in root_cache:
                    root_cache[cache_key] = await find_resource_bundle_root(
                        viking_fs,
                        matched_uri,
                        ctx=ctx,
                        uri_is_directory=uri_is_directory,
                    )
                root_uri = root_cache[cache_key]
                if not root_uri:
                    continue
                score = float(match.score)
                current = grouped.get(root_uri)
                if current is None or score > current["score"]:
                    grouped[root_uri] = {
                        "uri": root_uri,
                        "score": score,
                        "matched_uri": matched_uri,
                        "match_reason": match.match_reason,
                    }
            if (
                len(grouped) >= limit
                or len(child_result.resources) < candidate_limit
                or candidate_limit >= max_candidates
            ):
                break
            candidate_limit = min(max_candidates, candidate_limit * 2)

        resources = sorted(grouped.values(), key=lambda item: (-item["score"], item["uri"]))[:limit]
        return {"resources": resources, "total": len(resources)}
