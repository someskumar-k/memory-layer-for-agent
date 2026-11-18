"""Loopback-only MCP transport."""

DEFAULT_PORT = 8765


def validate_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("HTTP port must be an integer from 1024 to 65535")
    return port


def serve_http(config, port=DEFAULT_PORT):
    import uvicorn

    from .server import create_server

    validate_port(port)
    server = create_server(config, port=port)
    uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=port,
                proxy_headers=False, access_log=False)
