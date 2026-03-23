const MODEL_NAME = "@cf/openai/gpt-oss-120b";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

function sseEvent(eventType, content) {
  return `event: ${eventType}\ndata: ${JSON.stringify({ type: eventType, content })}\n\n`;
}

function parseArgumentsObject(value) {
  if (value == null) return {};
  if (typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value !== "string") return {};
  const raw = value.trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function normalizeToolCall(candidate) {
  if (!candidate || typeof candidate !== "object") return null;

  let name = candidate.name || candidate.tool || candidate.function_name;
  let argumentsValue = candidate.arguments ?? candidate.parameters ?? candidate.args;
  const hasToolShape =
    argumentsValue !== undefined ||
    candidate.function != null ||
    candidate.tool_calls != null ||
    candidate.function_call != null ||
    String(candidate.type || "").toLowerCase().includes("function");

  if (candidate.function && typeof candidate.function === "object") {
    name = name || candidate.function.name;
    if (argumentsValue === undefined) {
      argumentsValue =
        candidate.function.arguments ??
        candidate.function.parameters ??
        candidate.function.args;
    }
  }

  if (!hasToolShape) return null;
  if (typeof name !== "string" || !name.trim()) return null;
  return {
    name: name.trim(),
    parameters: parseArgumentsObject(argumentsValue),
  };
}

function collectToolCalls(value, seen = new WeakSet(), depth = 0) {
  if (depth > 6 || value == null) return [];
  if (Array.isArray(value)) {
    return value.flatMap((item) => collectToolCalls(item, seen, depth + 1));
  }

  if (typeof value !== "object") return [];
  if (seen.has(value)) return [];
  seen.add(value);

  const calls = [];
  const direct = normalizeToolCall(value);
  if (direct) calls.push(direct);

  if (Array.isArray(value.tool_calls)) {
    for (const toolCall of value.tool_calls) {
      const normalized = normalizeToolCall(toolCall);
      if (normalized) calls.push(normalized);
    }
  }

  if (value.function_call && typeof value.function_call === "object") {
    const normalized = normalizeToolCall(value.function_call);
    if (normalized) calls.push(normalized);
  }

  const nestedKeys = ["choices", "message", "delta", "output", "content", "response"];
  for (const key of nestedKeys) {
    if (value[key] !== undefined) {
      calls.push(...collectToolCalls(value[key], seen, depth + 1));
    }
  }

  return calls;
}

function extractToolCallPayload(value) {
  const collected = collectToolCalls(value);
  if (collected.length === 0) return "";

  const uniqueBySig = new Map();
  for (const call of collected) {
    const sig = JSON.stringify(call);
    if (!uniqueBySig.has(sig)) {
      uniqueBySig.set(sig, call);
    }
  }

  const calls = Array.from(uniqueBySig.values());
  return JSON.stringify(calls.length === 1 ? calls[0] : calls);
}

function extractTextFromOutput(output) {
  if (!Array.isArray(output)) return "";
  const chunks = [];

  for (const item of output) {
    if (!item) continue;
    if (item.type === "reasoning") continue;
    if (typeof item.text === "string") {
      chunks.push(item.text);
      continue;
    }
    if (!Array.isArray(item.content)) continue;
    for (const part of item.content) {
      if (!part) continue;
      if (part.type === "reasoning_text") continue;
      if (typeof part.text === "string") {
        chunks.push(part.text);
      } else if (typeof part.output_text === "string") {
        chunks.push(part.output_text);
      } else if (typeof part.content === "string") {
        chunks.push(part.content);
      }
    }
  }

  if (chunks.length > 0) {
    return chunks.join("");
  }
  return "";
}

function extractContent(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);

  if (Array.isArray(value)) {
    return value.map((item) => extractContent(item)).join("");
  }

  if (typeof value !== "object") return "";

  if (typeof value.response === "string") return value.response;
  if (typeof value.output_text === "string") return value.output_text;
  if (typeof value.text === "string") return value.text;
  if (typeof value.content === "string") return value.content;

  if (Array.isArray(value.choices)) {
    const choiceChunks = [];
    for (const choice of value.choices) {
      if (!choice) continue;
      const fromDelta = extractContent(choice.delta);
      const fromMessage = extractContent(choice.message);
      const fromText = extractContent(choice.text);
      if (fromDelta) choiceChunks.push(fromDelta);
      if (fromMessage) choiceChunks.push(fromMessage);
      if (fromText) choiceChunks.push(fromText);
    }
    if (choiceChunks.length > 0) {
      return choiceChunks.join("");
    }
  }

  if (Array.isArray(value.content)) {
    return value.content.map((part) => extractContent(part)).join("");
  }

  if (value.delta != null) {
    return extractContent(value.delta);
  }

  if (value.message != null) {
    return extractContent(value.message);
  }

  if (value.output != null) {
    return extractTextFromOutput(value.output) || extractContent(value.output);
  }

  return "";
}

function parseIncomingPayload(body) {
  if (!body || typeof body !== "object") {
    throw new Error("JSON object body is required");
  }

  const aiInput = {};
  const passthroughKeys = [
    "lora",
    "response_format",
    "raw",
    "stream",
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "seed",
    "repetition_penalty",
    "frequency_penalty",
    "presence_penalty",
    "functions",
    "tools",
    "reasoning",
    "instructions",
  ];

  for (const key of passthroughKeys) {
    if (body[key] !== undefined) {
      aiInput[key] = body[key];
    }
  }

  if (typeof body.prompt === "string" && body.prompt.trim()) {
    aiInput.prompt = body.prompt;
  } else if (Array.isArray(body.messages) && body.messages.length > 0) {
    aiInput.messages = body.messages;
  } else if (body.input !== undefined) {
    aiInput.input = body.input;
  } else if (Array.isArray(body.requests) && body.requests.length > 0) {
    aiInput.requests = body.requests;
  } else {
    throw new Error("Provide one of: prompt, messages, input, or requests");
  }

  if (aiInput.stream === undefined) {
    aiInput.stream = true;
  }
  if (aiInput.max_tokens === undefined) {
    aiInput.max_tokens = 128000;
  }

  return aiInput;
}

function chunkTextForSSE(text, maxChunkLength = 80) {
  const chunks = [];
  if (!text) return chunks;

  const words = text.split(/(\s+)/);
  let current = "";

  for (const part of words) {
    if (!part) continue;
    if ((current + part).length > maxChunkLength && current) {
      chunks.push(current);
      current = part;
    } else {
      current += part;
    }
  }

  if (current) {
    chunks.push(current);
  }
  return chunks;
}

async function streamTextResponseAsSSE(textResponse, writer, encoder) {
  let emittedTokenCount = 0;
  let buffer = "";

  const emitPayloadLine = async (line) => {
    const normalized = line.trim();
    if (!normalized || normalized.startsWith("event:")) {
      return;
    }

    const payload = normalized.startsWith("data:")
      ? normalized.slice(5).trim()
      : normalized;

    if (!payload || payload === "[DONE]" || payload === "DONE") {
      return;
    }

    try {
      const parsed = JSON.parse(payload);
      const content = extractContent(parsed);
      if (content) {
        await writer.write(encoder.encode(sseEvent("token", content)));
        emittedTokenCount += 1;
      }
    } catch {
      await writer.write(encoder.encode(sseEvent("token", payload)));
      emittedTokenCount += 1;
    }
  };

  const flushBuffer = async () => {
    if (!buffer.trim()) return;
    await emitPayloadLine(buffer);
    buffer = "";
  };

  if (textResponse && typeof textResponse.getReader === "function") {
    const reader = textResponse.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value) continue;

        const chunk = value instanceof Uint8Array ? new TextDecoder().decode(value) : String(value);
        buffer += chunk;

        let newlineIdx = buffer.indexOf("\n");
        while (newlineIdx !== -1) {
          const line = buffer.slice(0, newlineIdx);
          buffer = buffer.slice(newlineIdx + 1);
          await emitPayloadLine(line);
          newlineIdx = buffer.indexOf("\n");
        }
      }
      await flushBuffer();
    } finally {
      reader.releaseLock();
    }
    return emittedTokenCount;
  }

  if (textResponse && typeof textResponse[Symbol.asyncIterator] === "function") {
    for await (const chunk of textResponse) {
      const content = extractContent(chunk);
      if (content) {
        await writer.write(encoder.encode(sseEvent("token", content)));
        emittedTokenCount += 1;
      }
    }
    return emittedTokenCount;
  }

  const content = extractContent(textResponse);
  if (content) {
    await writer.write(encoder.encode(sseEvent("token", content)));
    emittedTokenCount += 1;
  }
  return emittedTokenCount;
}

function streamAsSSE(aiResponse, fallbackResolver) {
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const encoder = new TextEncoder();

  (async () => {
    try {
      let emitted = await streamTextResponseAsSSE(aiResponse, writer, encoder);
      if (emitted === 0 && typeof fallbackResolver === "function") {
        const fallbackResponse = await fallbackResolver();
        const fallbackToolPayload = extractToolCallPayload(fallbackResponse);

        if (fallbackToolPayload) {
          await writer.write(encoder.encode(sseEvent("token", fallbackToolPayload)));
          emitted += 1;
        } else {
          const fallbackText = extractContent(fallbackResponse);
          for (const chunk of chunkTextForSSE(fallbackText)) {
            await writer.write(encoder.encode(sseEvent("token", chunk)));
            emitted += 1;
          }
        }
      }

      if (emitted === 0) {
        await writer.write(encoder.encode(sseEvent("error", "No content generated")));
      }
      await writer.write(encoder.encode(sseEvent("done", "")));
    } catch (error) {
      await writer.write(
        encoder.encode(
          sseEvent("error", error instanceof Error ? error.message : "Unknown streaming error")
        )
      );
    } finally {
      await writer.close();
    }
  })();

  return new Response(readable, {
    headers: {
      "content-type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      ...CORS_HEADERS,
    },
  });
}

/**
 * Cloudflare Worker entry point
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (request.method === "GET" && pathname === "/health") {
      return jsonResponse({
        status: "ok",
        model_loaded: true,
        model: MODEL_NAME,
      });
    }

    if (request.method === "POST" && pathname === "/chat") {
      try {
        const body = await request.json();
        const aiInput = parseIncomingPayload(body);
        if (aiInput.stream) {
          const streamInput = { ...aiInput, stream: true };
          const streamResult = await env.AI.run(MODEL_NAME, streamInput);
          return streamAsSSE(streamResult, async () => {
            const fallbackInput = { ...aiInput, stream: false };
            return env.AI.run(MODEL_NAME, fallbackInput);
          });
        }

        const result = await env.AI.run(MODEL_NAME, { ...aiInput, stream: false });
        return jsonResponse(result);
      } catch (error) {
        return jsonResponse(
          { error: error instanceof Error ? error.message : "Unknown error" },
          400
        );
      }
    }

    if (request.method === "GET" && pathname === "/") {
      try {
        const response = await env.AI.run(MODEL_NAME, {
          instructions: "You are a concise assistant. Reply in at most 60 words.",
          input: "What is the origin of the phrase Hello, World?",
          max_tokens: 80,
          temperature: 0.2,
        });
        return jsonResponse({
          model: MODEL_NAME,
          answer: extractContent(response),
        });
      } catch (error) {
        return jsonResponse(
          { error: error instanceof Error ? error.message : "Unknown error" },
          500
        );
      }
    }

    return new Response("Not Found", {
      status: 404,
      headers: CORS_HEADERS,
    });
  },
};
