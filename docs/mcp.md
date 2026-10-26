# Mandate over MCP

Agents that speak the Model Context Protocol can use Mandate directly. Two transports, one handler
(`apps/api/mandate/mcp.py`):

* **stdio**: `python -m mandate.mcp` (add `--demo-mandate` to have a demo mandate signed for you, standing in for the human).
* **HTTP**: `POST /mcp` on the running API, plain JSON-RPC (no streaming).

Claude Desktop style config:

```json
{ "mcpServers": { "mandate": { "command": "python", "args": ["-m", "mandate.mcp", "--demo-mandate"],
                               "cwd": "apps/api", "env": { "PYTHONPATH": "." } } } }
```

| Tool | Does | Mutates |
|---|---|---|
| `search_catalog` | demo merchants' catalog; review text is flagged as untrusted | no |
| `list_mandates`, `get_mandate` | mandates a human signed: limits, balance, expiry | no |
| `purchase` | tries to buy inside a mandate; the guard answers allow, step_up or deny with reasons | creates an order awaiting buyer approval |
| `capture` | captures an order the buyer approved | yes |
| `order_status`, `receipt`, `dispute_status` | reads | no |

**What is missing is the point.** There is no tool to create, sign, widen or revoke a mandate. Authority is minted
by a human in the buyer app. A prompt-injected agent can only ask `purchase` for something, and `purchase` is the same
guarded path as the web app. Unknown tools and unexpected arguments are rejected; tool failures come back as
`isError` results, not crashes.

No authentication is built into `/mcp`: it is a sandbox demo behind the rate limiter. A real deployment would put it
behind the same auth as the rest of the API.
