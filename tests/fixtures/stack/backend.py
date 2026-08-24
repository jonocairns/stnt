#!/usr/bin/env python3
"""Synthetic HTTP and WebSocket backend for the explicit stack proof."""

import base64
import hashlib
import json
import os
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


STACK = os.environ["STNT_STACK_NAME"]
HTTP_PORT = int(os.environ["STNT_HTTP_PORT"])
WEBSOCKET_PORT = int(os.environ["STNT_WEBSOCKET_PORT"])


class HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"stack": STACK, "transport": "http", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def read_exact(stream, length):
    value = b""
    while len(value) < length:
        chunk = stream.read(length - len(value))
        if not chunk:
            raise EOFError
        value += chunk
    return value


class WebSocketHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = self.rfile.readline()
        if not request.startswith(b"GET "):
            return
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b""}:
                break
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        accept = base64.b64encode(hashlib.sha1(
            (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        self.wfile.write((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())
        first, second = read_exact(self.rfile, 2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", read_exact(self.rfile, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", read_exact(self.rfile, 8))[0]
        mask = read_exact(self.rfile, 4) if second & 0x80 else b""
        payload = read_exact(self.rfile, length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode != 1:
            return
        response = json.dumps({"stack": STACK, "transport": "websocket", "message": payload.decode()}).encode()
        header = bytes([0x81, len(response)]) if len(response) < 126 else bytes([0x81, 126]) + struct.pack("!H", len(response))
        self.wfile.write(header + response)


class WebSocketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


threading.Thread(
    target=ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), HTTPHandler).serve_forever,
    daemon=True,
).start()
WebSocketServer(("127.0.0.1", WEBSOCKET_PORT), WebSocketHandler).serve_forever()
