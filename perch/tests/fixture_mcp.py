"""Local MCP server for bridge verification; no credentials or network access."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP('perch-test')


@mcp.tool()
def echo(text: str) -> str:
    return 'Echo: ' + text


@mcp.resource('fixture://document')
def document() -> str:
    return 'Fixture resource'


@mcp.prompt()
def review(subject: str) -> str:
    return 'Review ' + subject


if __name__ == '__main__':
    mcp.run()
