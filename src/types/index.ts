export interface Conversation {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface FileAttachment {
  filename: string;
  mime_type: string;
  size: number;
  content: string;
  success: boolean;
  error?: string;
}

export interface Message {
  id: number;
  conversation_id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  files?: FileAttachment[];
  timestamp: string;
  timestamp_formatted?: string;
}

export interface ChatState {
  messages: Message[];
  isStreaming: boolean;
  currentThinking: string;
  isThinkingExpanded: boolean;
}

export interface ToolCall {
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ToolResult {
  success: boolean;
  result?: unknown;
  error?: string;
}

export type Theme = 'light' | 'dark';
