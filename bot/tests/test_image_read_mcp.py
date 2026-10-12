import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import ImageContent, TextContent
from vikingbot.agent.tools.base import MultimodalToolResult, ToolContext
from vikingbot.agent.tools.mcp import MCPToolWrapper
from vikingbot.agent.tools.ov_file import VikingMultiReadTool


@pytest.mark.asyncio
async def test_mcp_wrapper_keeps_images_out_of_text_and_in_model_content():
    data = base64.b64encode(b"test-image").decode()
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                content=[
                    TextContent(type="text", text="choose x/y"),
                    ImageContent(type="image", mimeType="image/png", data=data),
                ]
            )
        )
    )
    tool = MCPToolWrapper(
        session, "ov", SimpleNamespace(name="read", description="read", inputSchema={})
    )
    result = await tool.execute(ToolContext(), uris="viking://resources/a.png")
    assert isinstance(result, MultimodalToolResult)
    assert data not in str(result)
    assert result.content[1]["image_url"]["url"] == f"data:image/png;base64,{data}"


@pytest.mark.asyncio
async def test_bot_read_forwards_crop_parameters(monkeypatch):
    client = SimpleNamespace(
        read_image=AsyncMock(
            return_value={
                "text": "crop",
                "data": base64.b64encode(b"\x89PNG\r\n\x1a\ntest").decode(),
            }
        )
    )
    tool = VikingMultiReadTool()
    monkeypatch.setattr(tool, "_get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(tool, "_release_client", AsyncMock())
    uri = "viking://resources/a.png"
    result = await tool.execute(ToolContext(), uris=[uri], x=0.5, y=0.25, width=100, height=200)
    assert isinstance(result, MultimodalToolResult)
    client.read_image.assert_awaited_once_with(uri, x=0.5, y=0.25, width=100, height=200)
    invalid = await tool.execute(ToolContext(), uris=[uri, uri], x=0, y=0)
    assert "exactly one" in invalid
