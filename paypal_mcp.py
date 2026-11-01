"""The owner assistant's read-only window on PayPal, through PayPal's official MCP server.

Runs `npx @paypal/mcp` and talks to it over stdin/stdout. Only the tools in READ_ONLY can
be called, whatever the model asks for: money still moves only through policy.py.
"""
import json
import os
import shutil
import subprocess
import threading

READ_ONLY = ("list_invoices", "get_invoice", "list_transactions", "list_disputes", "get_dispute")
TIMEOUT = 45


class MCPError(Exception):
    pass


def available():
    return bool(shutil.which("npx"))


class PayPalMCP:
    def __init__(self, access_token, environment="SANDBOX"):
        env = dict(os.environ, PAYPAL_ACCESS_TOKEN=access_token, PAYPAL_ENVIRONMENT=environment)
        self.proc = subprocess.Popen(["npx", "-y", "@paypal/mcp@1.8.1", "--tools=all"], env=env, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.next_id = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "living-pay", "version": "1.0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise MCPError("PayPal's MCP server stopped: %s" % e)

    def read(self, want_id):
        """Next reply with this id. A hung server is killed, which ends the read."""
        timer = threading.Timer(TIMEOUT, self.proc.kill)
        timer.start()
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("id") == want_id:
                    return message
        finally:
            timer.cancel()
        raise MCPError("PayPal's MCP server didn't answer.")

    def rpc(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        reply = self.read(self.next_id)
        if "error" in reply:
            raise MCPError(reply["error"].get("message") or "PayPal's MCP server returned an error.")
        return reply["result"]

    def tools(self):
        """The read-only tools, in the shape Claude takes them."""
        return [{"name": t["name"], "description": t.get("description") or t["name"],
                 "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}}
                for t in self.rpc("tools/list").get("tools", []) if t["name"] in READ_ONLY]

    def call(self, name, arguments):
        if name not in READ_ONLY:
            raise MCPError("%s isn't allowed: this assistant can only read from PayPal." % name)
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return "\n".join(c.get("text", "") for c in result.get("content", []))
