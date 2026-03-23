const MODEL_NAME = "@cf/llava-hf/llava-1.5-7b-hf";
const DEFAULT_PROMPT =
  "Describe this image in detail. If there is readable text in the image, transcribe it exactly.";

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

function decodeBase64Image(base64Input) {
  if (typeof base64Input !== "string" || !base64Input.trim()) {
    throw new Error("image must be a non-empty base64 string");
  }

  const trimmed = base64Input.trim();
  const content = trimmed.includes(",") ? trimmed.split(",").pop() : trimmed;
  if (!content) {
    throw new Error("image base64 payload is empty");
  }

  let binary;
  try {
    binary = atob(content);
  } catch {
    throw new Error("image is not valid base64");
  }

  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function normalizeImageInput(image) {
  if (Array.isArray(image)) {
    const bytes = image.map((value) => Number(value));
    if (bytes.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
      throw new Error("image array must contain integer byte values between 0 and 255");
    }
    return bytes;
  }

  if (image && typeof image === "object" && Array.isArray(image.data)) {
    const bytes = image.data.map((value) => Number(value));
    if (bytes.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
      throw new Error("image.data must contain integer byte values between 0 and 255");
    }
    return bytes;
  }

  if (typeof image === "string") {
    return Array.from(decodeBase64Image(image));
  }

  throw new Error("Provide image as base64 string or byte array");
}

function resolveDescription(result) {
  if (typeof result === "string") return result.trim();
  if (!result || typeof result !== "object") return "";

  const direct = [result.description, result.response, result.text];
  for (const value of direct) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  if (result.result && typeof result.result === "object") {
    const nested = [result.result.description, result.result.response, result.result.text];
    for (const value of nested) {
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
    }
  }

  return "";
}

async function parseJson(request) {
  try {
    return await request.json();
  } catch {
    throw new Error("Invalid JSON body");
  }
}

async function handleDescribe(request, env) {
  const body = await parseJson(request);
  const image = normalizeImageInput(body?.image);
  const prompt =
    typeof body?.prompt === "string" && body.prompt.trim() ? body.prompt.trim() : DEFAULT_PROMPT;

  const aiInput = {
    prompt,
    image,
  };

  if (typeof body?.max_tokens === "number" && Number.isFinite(body.max_tokens) && body.max_tokens > 0) {
    aiInput.max_tokens = Math.floor(body.max_tokens);
  }

  const result = await env.AI.run(MODEL_NAME, aiInput);
  const description = resolveDescription(result);

  if (!description) {
    return jsonResponse(
      {
        error: "Workers AI returned an empty description",
        model: MODEL_NAME,
        result,
      },
      502
    );
  }

  return jsonResponse({
    model: MODEL_NAME,
    filename: body?.filename || null,
    mime_type: body?.mime_type || null,
    description,
    result,
  });
}

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

    if (request.method === "POST" && (pathname === "/describe" || pathname === "/chat")) {
      try {
        return await handleDescribe(request, env);
      } catch (error) {
        return jsonResponse(
          {
            error: error instanceof Error ? error.message : "Unknown error",
          },
          400
        );
      }
    }

    if (request.method === "GET" && pathname === "/") {
      return jsonResponse({
        status: "ok",
        model: MODEL_NAME,
        endpoints: {
          health: "/health",
          describe: "/describe",
        },
      });
    }

    return new Response("Not Found", {
      status: 404,
      headers: CORS_HEADERS,
    });
  },
};
