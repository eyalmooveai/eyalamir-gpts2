"""Print the LaForge tools as function-calling definitions, for hosts without MCP."""
import asyncio
import json

from laforge.server import mcp


async def main():
    tools = await mcp.list_tools()
    print(json.dumps([{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": t.inputSchema}}
        for t in tools], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
