// Minimal, spec-faithful MCP server over JSON-RPC 2.0.
//
// We hand-roll the protocol (rather than pull in @modelcontextprotocol/sdk) so
// the Worker has zero dependencies and the whole thing is unit-testable under
// plain `node --test`. We implement the Streamable HTTP transport in its
// simplest compliant form: each POST carries one JSON-RPC message (or a batch)
// and we answer with a single application/json JSON-RPC response. Sessions are
// tracked with the Mcp-Session-Id header so start_exam can remember the active
// code for later calls.

import { TOOL_DEFS, callTool } from './tools.js';
import { PROCTOR_INSTRUCTIONS } from './proctor.js';

const SUPPORTED_PROTOCOLS = ['2025-06-18', '2025-03-26', '2024-11-05'];
const DEFAULT_PROTOCOL = '2025-06-18';
export const SERVER_INFO = { name: 'slay-your-own-exam-voice', version: '1.0.0' };

function rpcResult(id, result) { return { jsonrpc: '2.0', id, result }; }
function rpcError(id, code, message, data) {
  const error = { code, message };
  if (data !== undefined) error.data = data;
  return { jsonrpc: '2.0', id, error };
}

// Handle one JSON-RPC request object. Returns a response object, or null for
// notifications (which get no reply).
async function handleOne(msg, ctx) {
  if (!msg || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') {
    return rpcError(msg && msg.id != null ? msg.id : null, -32600, 'Invalid Request');
  }
  const { method, id } = msg;
  const isNotification = id === undefined || id === null;

  switch (method) {
    case 'initialize': {
      const wanted = msg.params && msg.params.protocolVersion;
      const protocolVersion = SUPPORTED_PROTOCOLS.includes(wanted) ? wanted : DEFAULT_PROTOCOL;
      return rpcResult(id, {
        protocolVersion,
        capabilities: { tools: { listChanged: false } },
        serverInfo: SERVER_INFO,
        instructions: PROCTOR_INSTRUCTIONS,
      });
    }
    case 'notifications/initialized':
    case 'notifications/cancelled':
      return null; // notifications: no response
    case 'ping':
      return isNotification ? null : rpcResult(id, {});
    case 'tools/list':
      return rpcResult(id, { tools: TOOL_DEFS });
    case 'tools/call': {
      const params = msg.params || {};
      const name = params.name;
      if (!name) return rpcError(id, -32602, 'Missing tool name');
      const result = await callTool(name, params.arguments || {}, ctx);
      return rpcResult(id, result);
    }
    default:
      if (isNotification) return null;
      return rpcError(id, -32601, `Method not found: ${method}`);
  }
}

// Entry point used by the Worker. `body` is the parsed JSON (object or array).
// Returns { responses } where responses is null (nothing to send, HTTP 202) or
// a JSON-RPC response object/array to serialize.
export async function handleRpc(body, ctx) {
  if (Array.isArray(body)) {
    const out = [];
    for (const m of body) {
      const r = await handleOne(m, ctx);
      if (r) out.push(r);
    }
    return { responses: out.length ? out : null };
  }
  const r = await handleOne(body, ctx);
  return { responses: r };
}
