"""Claude client wrapper: disk cache (reproducible reruns), usage ledger, tool-use loop with structured final output."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from ghost.config import REPO_ROOT, path as cfg_path, settings

load_dotenv(REPO_ROOT / ".env")

# Models that accept the server-side refusal fallback ("default" form, Claude API).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(RuntimeError):
    pass


class LLM:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.client = anthropic.Anthropic(max_retries=settings()["llm"].get("sdk_max_retries", 8), timeout=300.0)
        self.cache_dir = cfg_path("cache") / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = cfg_path("cache") / "usage" / f"{run_id}.jsonl"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ single call
    def call(self, agent: str, *, system: str, messages: list, tools: list | None = None,
             schema: dict | None = None, case_id: str | None = None, tool_choice: dict | None = None) -> dict:
        cfg = settings()
        model = cfg["models"][agent]
        output_config: dict = {"effort": cfg["effort"][agent]}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        params: dict = {
            "model": model,
            "max_tokens": cfg["llm"]["max_tokens"],
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": output_config,
        }
        if tools:
            params["tools"] = tools
        if tool_choice:
            params["tool_choice"] = tool_choice
        key = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
        cpath = self.cache_dir / f"{key}.json"
        if cpath.exists():
            rec = json.loads(cpath.read_text(encoding="utf-8"))
            self._log(agent, case_id, rec, cached=True)
            return rec

        t0 = time.time()
        if model in FALLBACK_MODELS:
            resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
        else:
            resp = self.client.messages.create(**params)
        rec = {
            "model": resp.model,
            "requested_model": model,
            "stop_reason": resp.stop_reason,
            "content": [b.to_dict() for b in resp.content],
            "usage": resp.usage.to_dict(),
            "latency_s": round(time.time() - t0, 2),
            "request_id": getattr(resp, "_request_id", None),
        }
        cpath.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        self._log(agent, case_id, rec, cached=False)
        return rec

    def _log(self, agent: str, case_id: str | None, rec: dict, cached: bool):
        u = rec["usage"]
        row = {"run_id": self.run_id, "agent": agent, "case_id": case_id, "model": rec["model"],
               "requested_model": rec["requested_model"], "cached": cached,
               "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
               "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
               "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0}
        with self._lock, open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ------------------------------------------------------------ agent loop
    def run_agent(self, agent: str, *, system: str, user: str | list, schema: dict,
                  tools: list | None = None, tool_handler=None, case_id: str | None = None) -> dict:
        """Tool-use loop ending in a JSON-schema-constrained answer. Returns {"output", "transcript"}."""
        messages: list = [{"role": "user", "content": user}]
        max_turns = settings()["llm"]["max_tool_turns"]
        for turn in range(max_turns + 1):
            last = turn == max_turns
            if last and tools:
                messages.append({"role": "user", "content": "Tool budget exhausted. Give the final answer now."})
            rec = self.call(agent, system=system, messages=messages, tools=tools, schema=schema, case_id=case_id,
                            tool_choice={"type": "none"} if (last and tools) else None)
            stop = rec["stop_reason"]
            if stop == "refusal":
                raise AgentError(f"{agent}: refusal")
            if stop == "max_tokens":
                raise AgentError(f"{agent}: max_tokens reached")
            if stop == "tool_use" and tools and tool_handler:
                messages.append({"role": "assistant", "content": rec["content"]})
                results = []
                for b in rec["content"]:
                    if b.get("type") == "tool_use":
                        try:
                            out = tool_handler(b["name"], b.get("input") or {})
                            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out})
                        except Exception as e:  # tool errors go back to the model
                            results.append({"type": "tool_result", "tool_use_id": b["id"],
                                            "content": f"Error: {e}", "is_error": True})
                messages.append({"role": "user", "content": results})
                continue
            text = next((b["text"] for b in rec["content"] if b.get("type") == "text"), None)
            if text is None:
                raise AgentError(f"{agent}: no text output (stop_reason={stop})")
            try:
                return {"output": json.loads(text), "turns": turn + 1}
            except json.JSONDecodeError as e:
                raise AgentError(f"{agent}: invalid JSON output: {e}") from e
        raise AgentError(f"{agent}: no answer within {max_turns} tool turns")


def usage_summary(ledger: Path) -> dict:
    """Aggregate a run ledger into per-model / overall token and cost totals (cached calls report original usage)."""
    cfg = settings()["pricing"]
    per: dict = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        m = per.setdefault(r["model"], {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                                         "cost_usd": 0.0, "cases": set(), "agents": set()})
        m["calls"] += 1
        m["cached_calls"] += r["cached"]
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            m[k] += r[k]
        price = cfg.get(r["model"]) or cfg.get(r["requested_model"]) or {"input": 0, "output": 0}
        m["cost_usd"] += (r["input_tokens"] * price["input"] + r["output_tokens"] * price["output"]
                          + r["cache_read_input_tokens"] * price["input"] * cfg["cache_read_multiplier"]
                          + r["cache_creation_input_tokens"] * price["input"] * cfg["cache_write_multiplier"]) / 1e6
        if r["case_id"]:
            m["cases"].add(r["case_id"])
        m["agents"].add(r["agent"])
    for m in per.values():
        m["cases"] = len(m["cases"])
        m["agents"] = sorted(m["agents"])
        m["cost_usd"] = round(m["cost_usd"], 4)
    return per
