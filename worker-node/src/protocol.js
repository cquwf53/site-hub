import readline from 'readline';
import { randomUUID } from 'crypto';

export const JSONRPC_VERSION = '2.0';
export const PROTOCOL_VERSION = 1;
export const MAX_LINE_BYTES = 1024 * 1024;

function rpcError(code, message, data) {
  return { code, message, ...(data === undefined ? {} : { data }) };
}

export class JsonRpcPeer {
  constructor({ input, output, requestTimeoutMs = 10000 }) {
    this.output = output;
    this.requestTimeoutMs = requestTimeoutMs;
    this.handlers = new Map();
    this.pending = new Map();
    this.closed = false;
    this.sequence = 0;
    this.reader = readline.createInterface({ input, crlfDelay: Infinity });
    this.reader.on('line', line => void this.#onLine(line));
    this.reader.on('close', () => this.close(new Error('RPC input closed')));
  }

  handle(method, handler) {
    this.handlers.set(method, handler);
  }

  notify(method, params = {}) {
    this.#send({ jsonrpc: JSONRPC_VERSION, method, params });
  }

  request(method, params = {}, timeoutMs = this.requestTimeoutMs) {
    if (this.closed) return Promise.reject(new Error('RPC peer is closed'));
    const id = `n-${process.pid}-${++this.sequence}-${randomUUID()}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`RPC request timed out: ${method}`));
      }, timeoutMs);
      timer.unref?.();
      this.pending.set(id, { resolve, reject, timer });
      this.#send({ jsonrpc: JSONRPC_VERSION, id, method, params });
    });
  }

  close(error = new Error('RPC peer closed')) {
    if (this.closed) return;
    this.closed = true;
    this.reader.close();
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  #send(payload) {
    if (this.closed) throw new Error('RPC peer is closed');
    const encoded = JSON.stringify(payload);
    if (Buffer.byteLength(encoded, 'utf8') > MAX_LINE_BYTES) {
      throw new Error('RPC message exceeds 1 MiB');
    }
    this.output.write(`${encoded}\n`);
  }

  async #onLine(line) {
    if (Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES) {
      this.#send({ jsonrpc: JSONRPC_VERSION, id: null, error: rpcError(-32600, 'Request too large') });
      return;
    }

    let message;
    try {
      message = JSON.parse(line);
    } catch {
      this.#send({ jsonrpc: JSONRPC_VERSION, id: null, error: rpcError(-32700, 'Parse error') });
      return;
    }

    if (message?.jsonrpc !== JSONRPC_VERSION) {
      if (message?.id !== undefined) {
        this.#send({ jsonrpc: JSONRPC_VERSION, id: message.id ?? null, error: rpcError(-32600, 'Invalid Request') });
      }
      return;
    }

    if (!message.method && message.id !== undefined) {
      const pending = this.pending.get(String(message.id));
      if (!pending) return;
      this.pending.delete(String(message.id));
      clearTimeout(pending.timer);
      if (message.error) pending.reject(Object.assign(new Error(message.error.message), { rpcError: message.error }));
      else pending.resolve(message.result);
      return;
    }

    if (typeof message.method !== 'string') return;
    const handler = this.handlers.get(message.method);
    if (!handler) {
      if (message.id !== undefined) {
        this.#send({ jsonrpc: JSONRPC_VERSION, id: message.id, error: rpcError(-32601, 'Method not found') });
      }
      return;
    }

    try {
      const result = await handler(message.params || {}, message);
      if (message.id !== undefined) {
        this.#send({ jsonrpc: JSONRPC_VERSION, id: message.id, result: result ?? null });
      }
    } catch (error) {
      if (message.id !== undefined) {
        this.#send({
          jsonrpc: JSONRPC_VERSION,
          id: message.id,
          error: rpcError(-32000, String(error?.message || error).slice(0, 500)),
        });
      }
    }
  }
}
