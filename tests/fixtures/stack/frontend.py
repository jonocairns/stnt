#!/usr/bin/env python3
"""Synthetic shared ingress for the explicit two-repository stack proof."""

import http.client
import os
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


STACK = os.environ["STNT_STACK_NAME"]
PORT = int(os.environ["STNT_INGRESS_PORT"])
HTTP_PORT = int(os.environ["STNT_HTTP_PORT"])
WEBSOCKET_PORT = int(os.environ["STNT_WEBSOCKET_PORT"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = f"healthy:{STACK}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/"):
            connection = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
            connection.request("GET", self.path, headers={"Host": f"127.0.0.1:{HTTP_PORT}"})
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            connection.close()
        elif self.path.startswith("/ws/") and self.headers.get("Upgrade", "").lower() == "websocket":
            self.proxy_websocket()
        else:
            self.send_error(404)

    def proxy_websocket(self):
        backend = socket.create_connection(("127.0.0.1", WEBSOCKET_PORT), timeout=5)
        request = [
            f"GET {self.path} HTTP/1.1",
            f"Host: 127.0.0.1:{WEBSOCKET_PORT}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {self.headers['Sec-WebSocket-Key']}",
            f"Sec-WebSocket-Version: {self.headers.get('Sec-WebSocket-Version', '13')}",
            "",
            "",
        ]
        backend.sendall("\r\n".join(request).encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += backend.recv(4096)
        self.connection.sendall(response)
        self.close_connection = True
        sockets = [self.connection, backend]
        while True:
            readable, _, _ = select.select(sockets, [], [], 10)
            if not readable:
                break
            for source in readable:
                data = source.recv(65536)
                if not data:
                    backend.close()
                    return
                target = backend if source is self.connection else self.connection
                target.sendall(data)

    def log_message(self, _format, *_args):
        pass


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
