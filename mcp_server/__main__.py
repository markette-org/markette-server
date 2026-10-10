"""stdio transport for local MCP clients: MARKETTE_TOKEN=mkt_… MARKETTE_API_URL=https://… python -m mcp_server"""

from .server import mcp

if __name__ == "__main__":
    mcp.run("stdio")
