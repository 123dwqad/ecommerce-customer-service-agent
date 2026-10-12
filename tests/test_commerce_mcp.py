"""Real MCP/SSE integration with a separate process and shared temporary DB."""
import asyncio
import gc
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from app.commerce.api import create_app
from app.commerce.config import Settings


class MCPIntegrationTests(unittest.TestCase):
    def test_authenticated_mcp_round_trip_and_write_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            key = secrets.token_hex(32)
            db_url = "sqlite:///" + str(Path(directory) / "commerce.db")
            settings = Settings(database_url=db_url, redis_url="", tool_transport="mcp",
                signing_key=key, mcp_url=f"http://127.0.0.1:{port}/sse", llm_enabled=False, intent_enabled=False)
            env = {**os.environ, "COMMERCE_SIGNING_KEY": key, "COMMERCE_DATABASE_URL": db_url,
                   "COMMERCE_MCP_PORT": str(port), "COMMERCE_LLM_ENABLED": "false"}
            log = (Path(directory) / "mcp.log").open("w", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, "-m", "mcp_server.server"], env=env,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for _ in range(100):
                    if proc.poll() is not None:
                        self.fail("MCP process exited unexpectedly")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail("MCP startup timed out")
                with TestClient(create_app(settings)) as client:
                    user = {"user_id": "mcpbuyer", "password": "test-password-123"}
                    client.post("/auth/register", json=user)
                    token = client.post("/auth/login", json=user).json()["access_token"]
                    headers = {"Authorization": "Bearer " + token}
                    result = client.post("/chat/", headers=headers, json={"query": "我的订单"}).json()
                    self.assertEqual(result["status"], "ok", result)
                    order = next(o for o in result["data"]["orders"] if o["status"] == "paid")
                    result = client.post("/chat/", headers=headers, json={"query": "取消订单 " + order["order_id"]}).json()
                    self.assertEqual(result["status"], "confirmation_required", result)
                    confirmation = client.post("/operations/confirm", headers=headers,
                        json={"confirmation_id": result["data"]["confirmation_id"]})
                    self.assertEqual(confirmation.json()["status"], "completed")
                    # Read product data through MCP, not a local tool shortcut.
                    result = client.post("/chat/", headers=headers, json={"query": "P100 耳机续航多久"}).json()
                    self.assertIn("6 小时", result["answer"])
                    result = client.post("/chat/", headers=headers,
                        json={"query": "P100 买2件能用优惠券吗"}).json()
                    self.assertEqual(result["status"], "eligible", result)
                    self.assertEqual(result["data"]["facts"]["discount_cents"], 2000)
                    orders = client.get("/orders", headers=headers).json()["orders"]
                    delivered = next(o for o in orders if o["status"] == "delivered")
                    prepared = client.post("/chat/", headers=headers,
                        json={"query": "我要退货 " + delivered["order_id"]}).json()
                    confirmed = client.post("/operations/confirm", headers=headers,
                        json={"confirmation_id": prepared["data"]["confirmation_id"]}).json()
                    progress = client.post("/chat/", headers=headers,
                        json={"query": "售后进度 " + confirmed["after_sale_id"]}).json()
                    self.assertEqual(progress["intent"], "after_sale_query")
                    self.assertEqual(progress["data"]["after_sale"]["status"], "submitted")
                    self.assertEqual([t["tool"] for t in progress["tool_trace"]], ["query_after_sale"])
            finally:
                proc.terminate()
                proc.wait(timeout=10)
                log.close()
                gc.collect()
                # Windows may release the exited subprocess's mapped SQLite handles asynchronously.
                time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
