import type { Conversation, Message, FileAttachment } from '@/types';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const STREAM_DEBUG_ENABLED = String(import.meta.env.VITE_STREAM_DEBUG ?? '1') !== '0';
let activeStreamAbortController: AbortController | null = null;

export class APIError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'APIError';
    this.status = status;
  }
}

export class StreamAbortedError extends Error {
  constructor() {
    super('Stream aborted');
    this.name = 'StreamAbortedError';
  }
}

function isAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return true;
  }
  return error instanceof Error && error.name === 'AbortError';
}

function normalizeSSEChunk(chunk: string): string {
  // Browsers/proxies may surface CRLF line endings; normalize once.
  return chunk.replace(/\r\n/g, '\n');
}

function extractSSEEvents(buffer: string): { events: string[]; rest: string } {
  const events: string[] = [];
  let remaining = buffer;
  let boundaryIndex = remaining.indexOf('\n\n');
  while (boundaryIndex !== -1) {
    events.push(remaining.slice(0, boundaryIndex));
    remaining = remaining.slice(boundaryIndex + 2);
    boundaryIndex = remaining.indexOf('\n\n');
  }
  return { events, rest: remaining };
}

function streamPreview(value: unknown, max = 140): string {
  let raw = '';
  if (typeof value === 'string') {
    raw = value;
  } else if (value == null) {
    raw = '';
  } else {
    try {
      raw = JSON.stringify(value);
    } catch {
      raw = String(value);
    }
  }
  const text = raw.replace(/\n/g, '\\n');
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function streamDebug(scope: string, message: string, extra?: unknown): void {
  if (!STREAM_DEBUG_ENABLED) return;
  if (extra === undefined) {
    console.log(`[api.ts::${scope}] ${message}`);
    return;
  }
  console.log(`[api.ts::${scope}] ${message}`, extra);
}

export function abortActiveStream(): void {
  if (activeStreamAbortController) {
    activeStreamAbortController.abort();
    activeStreamAbortController = null;
  }
}

async function fetchAPI<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });

  if (!response.ok) {
    const error = await response.text();
    throw new APIError(response.status, error);
  }

  return response.json();
}

// Conversation APIs
export async function getConversations(username: string): Promise<Conversation[]> {
  const data = await fetchAPI<{ conversations: Conversation[] }>(`/api/conversations/${username}`);
  return data.conversations;
}

export async function createConversation(username: string, title?: string): Promise<Conversation> {
  const data = await fetchAPI<{ conversation_id: number; title: string }>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ username, title: title || 'New Chat' }),
  });
  return {
    id: data.conversation_id,
    title: data.title,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

export async function getMessages(username: string, conversationId: number): Promise<{ conversation: Conversation; messages: Message[] }> {
  return fetchAPI(`/api/conversations/${username}/${conversationId}`);
}

export async function renameConversation(username: string, conversationId: number, title: string): Promise<void> {
  await fetchAPI(`/api/conversations/${username}/${conversationId}`, {
    method: 'PUT',
    body: JSON.stringify({ username, title }),
  });
}

export async function deleteConversation(username: string, conversationId: number): Promise<void> {
  await fetchAPI(`/api/conversations/${username}/${conversationId}`, {
    method: 'DELETE',
  });
}

// File upload
export async function uploadFiles(files: File[]): Promise<{ files: FileAttachment[]; success: boolean }> {
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));

  const response = await fetch(`${API_BASE_URL}/api/upload`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    throw new APIError(response.status, await response.text());
  }

  return response.json();
}

// Streaming chat
export interface StreamCallbacks {
  onToken?: (token: string) => void;
  onThinking?: (thinking: string) => void;
  onToolCall?: (toolName: string) => void;
  onToolResult?: (result: unknown) => void;
  onStatus?: (status: string) => void;
  onError?: (error: string) => void;
  onDone?: () => void;
}

export async function streamChat(
  message: string,
  callbacks: StreamCallbacks,
  options: {
    conversationId?: number;
    username?: string;
    files?: File[];
    onConversationId?: (conversationId: number) => void;
  } = {}
): Promise<number> {
  const { conversationId, username = 'GAKR', files = [], onConversationId } = options;
  const abortController = new AbortController();
  activeStreamAbortController = abortController;

  const formData = new FormData();
  formData.append('message', message);
  formData.append('username', username);
  if (conversationId) {
    formData.append('conversation_id', conversationId.toString());
  }
  files.forEach((file) => formData.append('files', file));

  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const decoder = new TextDecoder();
  let buffer = '';
  let returnedConversationId = conversationId;
  let doneReceived = false;
  let streamEndedNormally = false;
  let tokenChars = 0;
  let thinkingChars = 0;
  let eventCount = 0;

  streamDebug(
    'streamChat',
    `start api=${API_BASE_URL} conversation_id=${conversationId ?? 'new'} username=${username} files=${files.length} message_preview=${streamPreview(message)}`
  );

  const processEventBlock = (eventBlock: string) => {
    const lines = eventBlock.split('\n');
    let eventType = 'token';
    const dataLines: string[] = [];

    for (const line of lines) {
      const normalizedLine = line.endsWith('\r') ? line.slice(0, -1) : line;
      if (normalizedLine.startsWith('event:')) {
        eventType = normalizedLine.slice(6).trim();
      } else if (normalizedLine.startsWith('data:')) {
        let value = normalizedLine.slice(5);
        if (value.startsWith(' ')) {
          value = value.slice(1);
        }
        dataLines.push(value);
      }
    }
    const data = dataLines.join('\n');
    eventCount += 1;

    if (eventType === 'done') {
      doneReceived = true;
      streamDebug('streamChat', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
      callbacks.onDone?.();
      return;
    }
    if (!data) return;

    try {
      const parsed = JSON.parse(data);
      switch (eventType) {
        case 'conversation': {
          const conversationFromEvent = Number(
            parsed?.conversation_id ?? parsed?.conversationId ?? 0
          );
          if (Number.isFinite(conversationFromEvent) && conversationFromEvent > 0) {
            returnedConversationId = conversationFromEvent;
            streamDebug('streamChat', `event=conversation id=${conversationFromEvent}`);
            onConversationId?.(conversationFromEvent);
          }
          break;
        }
        case 'token': {
          const tokenValue = parsed.content || parsed;
          tokenChars += String(tokenValue ?? '').length;
          streamDebug('streamChat', `event=token chars_total=${tokenChars} preview=${streamPreview(tokenValue)}`);
          callbacks.onToken?.(parsed.content || parsed);
          break;
        }
        case 'thinking': {
          const thinkingValue = parsed.content || parsed;
          thinkingChars += String(thinkingValue ?? '').length;
          streamDebug('streamChat', `event=thinking chars_total=${thinkingChars} preview=${streamPreview(thinkingValue)}`);
          callbacks.onThinking?.(parsed.content || parsed);
          break;
        }
        case 'tool_call':
          streamDebug('streamChat', `event=tool_call preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onToolCall?.(parsed.content || parsed);
          break;
        case 'tool_result':
          streamDebug('streamChat', `event=tool_result preview=${streamPreview(parsed)}`);
          callbacks.onToolResult?.(parsed.content || parsed);
          break;
        case 'status':
          streamDebug('streamChat', `event=status preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onStatus?.(parsed.content || parsed);
          break;
        case 'error':
          console.error(`[api.ts::streamChat] event=error preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onError?.(parsed.content || parsed);
          break;
        case 'done':
          doneReceived = true;
          streamDebug('streamChat', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
          callbacks.onDone?.();
          break;
      }
    } catch {
      // If not JSON, handle plain text by event type
      if (eventType === 'token') {
        tokenChars += String(data ?? '').length;
        streamDebug('streamChat', `event=token(text) chars_total=${tokenChars} preview=${streamPreview(data)}`);
        callbacks.onToken?.(data);
      } else if (eventType === 'conversation') {
        const conversationFromEvent = Number(data);
        if (Number.isFinite(conversationFromEvent) && conversationFromEvent > 0) {
          returnedConversationId = conversationFromEvent;
          streamDebug('streamChat', `event=conversation(text) id=${conversationFromEvent}`);
          onConversationId?.(conversationFromEvent);
        }
      } else if (eventType === 'thinking') {
        thinkingChars += String(data ?? '').length;
        streamDebug('streamChat', `event=thinking(text) chars_total=${thinkingChars} preview=${streamPreview(data)}`);
        callbacks.onThinking?.(data);
      } else if (eventType === 'status') {
        streamDebug('streamChat', `event=status(text) preview=${streamPreview(data)}`);
        callbacks.onStatus?.(data);
      } else if (eventType === 'error') {
        console.error(`[api.ts::streamChat] event=error(text) preview=${streamPreview(data)}`);
        callbacks.onError?.(data);
      } else if (eventType === 'done') {
        doneReceived = true;
        streamDebug('streamChat', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
        callbacks.onDone?.();
      }
    }
  };
  try {
    const response = await fetch(`${API_BASE_URL}/api/chat/stream`, {
      method: 'POST',
      body: formData,
      signal: abortController.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[api.ts::streamChat] http_error status=${response.status} body=${streamPreview(errorText, 220)}`);
      throw new APIError(response.status, errorText);
    }

    reader = response.body?.getReader();
    if (!reader) {
      throw new Error('No response body');
    }

    // Get conversation ID from headers if available
    const headerConvId = response.headers.get('X-Conversation-Id');
    if (headerConvId) {
      returnedConversationId = parseInt(headerConvId, 10);
      if (returnedConversationId) {
        streamDebug('streamChat', `header conversation_id=${returnedConversationId}`);
        onConversationId?.(returnedConversationId);
      }
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        streamEndedNormally = true;
        break;
      }

      buffer += normalizeSSEChunk(decoder.decode(value, { stream: true }));

      // Process complete SSE events
      const parsedBatch = extractSSEEvents(buffer);
      buffer = parsedBatch.rest;

      for (const event of parsedBatch.events) {
        processEventBlock(event);
      }
    }

    // Flush remaining buffered event (if any).
    const trailing = buffer.trim();
    if (trailing) {
      processEventBlock(trailing);
    }

    // Safety: avoid UI stuck in streaming state if done event is missed.
    if (streamEndedNormally && !doneReceived) {
      streamDebug('streamChat', `stream ended without done event; forcing onDone`);
      callbacks.onDone?.();
    }
  } catch (error) {
    if (isAbortError(error)) {
      throw new StreamAbortedError();
    }
    throw error;
  } finally {
    streamDebug('streamChat', `finish events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
    reader?.releaseLock();
    if (activeStreamAbortController === abortController) {
      activeStreamAbortController = null;
    }
  }

  return returnedConversationId || 0;
}

// Stop streaming
export async function stopStreaming(
  conversationId: number,
  username: string = 'GAKR'
): Promise<{ success: boolean; message: string }> {
  const formData = new FormData();
  formData.append('conversation_id', conversationId.toString());
  formData.append('username', username);

  const response = await fetch(`${API_BASE_URL}/api/chat/stop`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new APIError(response.status, errorText);
  }

  return response.json();
}

// Continue streaming
export async function continueStreaming(
  conversationId: number,
  username: string = 'GAKR',
  callbacks: StreamCallbacks
): Promise<void> {
  const abortController = new AbortController();
  activeStreamAbortController = abortController;
  const formData = new FormData();
  formData.append('conversation_id', conversationId.toString());
  formData.append('username', username);

  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const decoder = new TextDecoder();
  let buffer = '';
  let doneReceived = false;
  let streamEndedNormally = false;
  let tokenChars = 0;
  let thinkingChars = 0;
  let eventCount = 0;
  streamDebug('continueStreaming', `start api=${API_BASE_URL} conversation_id=${conversationId} username=${username}`);

  const processEventBlock = (eventBlock: string) => {
    const lines = eventBlock.split('\n');
    let eventType = 'token';
    const dataLines: string[] = [];

    for (const line of lines) {
      const normalizedLine = line.endsWith('\r') ? line.slice(0, -1) : line;
      if (normalizedLine.startsWith('event:')) {
        eventType = normalizedLine.slice(6).trim();
      } else if (normalizedLine.startsWith('data:')) {
        let value = normalizedLine.slice(5);
        if (value.startsWith(' ')) {
          value = value.slice(1);
        }
        dataLines.push(value);
      }
    }
    const data = dataLines.join('\n');
    eventCount += 1;

    if (eventType === 'done') {
      doneReceived = true;
      streamDebug('continueStreaming', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
      callbacks.onDone?.();
      return;
    }
    if (!data) return;

    try {
      const parsed = JSON.parse(data);
      switch (eventType) {
        case 'token': {
          const tokenValue = parsed.content || parsed;
          tokenChars += String(tokenValue ?? '').length;
          streamDebug('continueStreaming', `event=token chars_total=${tokenChars} preview=${streamPreview(tokenValue)}`);
          callbacks.onToken?.(parsed.content || parsed);
          break;
        }
        case 'thinking': {
          const thinkingValue = parsed.content || parsed;
          thinkingChars += String(thinkingValue ?? '').length;
          streamDebug('continueStreaming', `event=thinking chars_total=${thinkingChars} preview=${streamPreview(thinkingValue)}`);
          callbacks.onThinking?.(parsed.content || parsed);
          break;
        }
        case 'tool_call':
          streamDebug('continueStreaming', `event=tool_call preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onToolCall?.(parsed.content || parsed);
          break;
        case 'tool_result':
          streamDebug('continueStreaming', `event=tool_result preview=${streamPreview(parsed)}`);
          callbacks.onToolResult?.(parsed.content || parsed);
          break;
        case 'status':
          streamDebug('continueStreaming', `event=status preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onStatus?.(parsed.content || parsed);
          break;
        case 'error':
          console.error(`[api.ts::continueStreaming] event=error preview=${streamPreview(parsed.content || parsed)}`);
          callbacks.onError?.(parsed.content || parsed);
          break;
        case 'done':
          doneReceived = true;
          streamDebug('continueStreaming', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
          callbacks.onDone?.();
          break;
      }
    } catch {
      // If not JSON, handle plain text by event type
      if (eventType === 'token') {
        tokenChars += String(data ?? '').length;
        streamDebug('continueStreaming', `event=token(text) chars_total=${tokenChars} preview=${streamPreview(data)}`);
        callbacks.onToken?.(data);
      } else if (eventType === 'thinking') {
        thinkingChars += String(data ?? '').length;
        streamDebug('continueStreaming', `event=thinking(text) chars_total=${thinkingChars} preview=${streamPreview(data)}`);
        callbacks.onThinking?.(data);
      } else if (eventType === 'status') {
        streamDebug('continueStreaming', `event=status(text) preview=${streamPreview(data)}`);
        callbacks.onStatus?.(data);
      } else if (eventType === 'error') {
        console.error(`[api.ts::continueStreaming] event=error(text) preview=${streamPreview(data)}`);
        callbacks.onError?.(data);
      } else if (eventType === 'done') {
        doneReceived = true;
        streamDebug('continueStreaming', `event=done events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
        callbacks.onDone?.();
      }
    }
  };

  try {
    const response = await fetch(`${API_BASE_URL}/api/chat/continue`, {
      method: 'POST',
      body: formData,
      signal: abortController.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[api.ts::continueStreaming] http_error status=${response.status} body=${streamPreview(errorText, 220)}`);
      throw new APIError(response.status, errorText);
    }

    reader = response.body?.getReader();
    if (!reader) {
      throw new Error('No response body');
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        streamEndedNormally = true;
        break;
      }

      buffer += normalizeSSEChunk(decoder.decode(value, { stream: true }));

      // Process complete SSE events
      const parsedBatch = extractSSEEvents(buffer);
      buffer = parsedBatch.rest;

      for (const event of parsedBatch.events) {
        processEventBlock(event);
      }
    }

    const trailing = buffer.trim();
    if (trailing) {
      processEventBlock(trailing);
    }

    if (streamEndedNormally && !doneReceived) {
      streamDebug('continueStreaming', `stream ended without done event; forcing onDone`);
      callbacks.onDone?.();
    }
  } catch (error) {
    if (isAbortError(error)) {
      throw new StreamAbortedError();
    }
    throw error;
  } finally {
    streamDebug('continueStreaming', `finish events=${eventCount} token_chars=${tokenChars} thinking_chars=${thinkingChars}`);
    reader?.releaseLock();
    if (activeStreamAbortController === abortController) {
      activeStreamAbortController = null;
    }
  }
}

// Check if conversation has active streaming state
export async function checkStreamingState(
  conversationId: number,
  username: string = 'GAKR'
): Promise<{ hasActiveStreaming: boolean; canContinue: boolean }> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/chat/streaming-state?conversation_id=${conversationId}&username=${username}`);
    if (!response.ok) {
      return { hasActiveStreaming: false, canContinue: false };
    }
    return response.json();
  } catch {
    return { hasActiveStreaming: false, canContinue: false };
  }
}

// Settings APIs
export async function getSettings(): Promise<{
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password_set: boolean;
  hf_space_url: string;
  custom_instructions: string;
}> {
  return fetchAPI('/api/settings');
}

export async function updateSettings(settings: {
  smtp_host?: string;
  smtp_port?: number;
  smtp_username?: string;
  smtp_password?: string;
  hf_space_url?: string;
  custom_instructions?: string;
}): Promise<void> {
  await fetchAPI('/api/settings', {
    method: 'POST',
    body: JSON.stringify(settings),
  });
}

// Model status
export async function getModelStatus(): Promise<{ status: string; error?: string }> {
  return fetchAPI('/api/model-status');
}
