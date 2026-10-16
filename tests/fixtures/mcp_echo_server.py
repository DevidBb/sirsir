"""Маленький MCP-сервер для тестов (stdio): echo, add, delete_all, needs_object."""

try:
    from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

server = Server("echo")


@server.tool()
def echo(text: str, times: int = 1) -> str:
    """Повторить текст."""
    return " ".join([text] * times)


@server.tool()
def add(numbers: list[float]) -> str:
    """Сложить числа."""
    return str(sum(numbers))


@server.tool()
def delete_all() -> str:
    """Удалить всё (для проверки подтверждения)."""
    return "всё удалено"


@server.tool()
def needs_object(options: dict) -> str:
    """Инструмент со сложным аргументом."""
    return "ok"


@server.tool()
def fail() -> str:
    """Всегда ошибка."""
    raise ValueError("сломалось нарочно")


if __name__ == "__main__":
    server.run()
