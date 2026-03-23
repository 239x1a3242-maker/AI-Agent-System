import { create } from 'zustand';
import type { Conversation, Message } from '@/types';

interface ChatStore {
  // Conversations
  conversations: Conversation[];
  currentConversationId: number | null;
  setConversations: (conversations: Conversation[]) => void;
  addConversation: (conversation: Conversation) => void;
  updateConversation: (id: number, updates: Partial<Conversation>) => void;
  removeConversation: (id: number) => void;
  setCurrentConversation: (id: number | null) => void;

  // Messages
  messages: Message[];
  addMessage: (message: Message) => void;
  updateLastMessage: (content: string) => void;
  setMessages: (messages: Message[]) => void;
  clearMessages: () => void;

  // Streaming
  isStreaming: boolean;
  setIsStreaming: (streaming: boolean) => void;
  stopStreaming: () => void;
  continueStreaming: () => void;
  canContinueStreaming: boolean;
  setCanContinueStreaming: (canContinue: boolean) => void;

  // Thinking per conversation
  thinkingByConversation: Record<number, string>;
  thinkingByAssistantTurn: Record<number, string[]>;
  isThinkingExpanded: boolean;
  setCurrentThinking: (conversationId: number, thinking: string) => void;
  appendThinking: (conversationId: number, thinking: string) => void;
  getCurrentThinking: (conversationId: number | null) => string;
  setThinkingForAssistantTurn: (conversationId: number, assistantTurn: number, thinking: string) => void;
  appendThinkingForAssistantTurn: (conversationId: number, assistantTurn: number, thinking: string) => void;
  getThinkingForAssistantTurn: (conversationId: number | null, assistantTurn: number) => string;
  toggleThinkingExpanded: () => void;
  setThinkingExpanded: (expanded: boolean) => void;

  // Files
  pendingFiles: File[];
  setPendingFiles: (files: File[]) => void;
  clearPendingFiles: () => void;

  // User
  username: string;
  setUsername: (username: string) => void;
}

export const useChatStore = create<ChatStore>((set, get) => ({
  // Conversations
  conversations: [],
  currentConversationId: null,
  setConversations: (conversations) => set({ conversations }),
  addConversation: (conversation) =>
    set((state) => ({
      conversations: [conversation, ...state.conversations],
    })),
  updateConversation: (id, updates) =>
    set((state) => ({
      conversations: state.conversations.map((c) =>
        c.id === id ? { ...c, ...updates } : c
      ),
    })),
  removeConversation: (id) =>
    set((state) => ({
      conversations: state.conversations.filter((c) => c.id !== id),
      currentConversationId:
        state.currentConversationId === id ? null : state.currentConversationId,
      thinkingByConversation: Object.fromEntries(
        Object.entries(state.thinkingByConversation).filter(([key]) => parseInt(key) !== id)
      ),
      thinkingByAssistantTurn: Object.fromEntries(
        Object.entries(state.thinkingByAssistantTurn).filter(([key]) => parseInt(key) !== id)
      ),
    })),
  setCurrentConversation: (id) => set({ currentConversationId: id }),

  // Messages
  messages: [],
  addMessage: (message) =>
    set((state) => ({
      messages: [...state.messages, message],
    })),
  updateLastMessage: (content) =>
    set((state) => {
      const messages = [...state.messages];
      const lastMessage = messages[messages.length - 1];
      if (lastMessage && lastMessage.role === 'assistant') {
        messages[messages.length - 1] = {
          ...lastMessage,
          content: lastMessage.content + content,
        };
      }
      return { messages };
    }),
  setMessages: (messages) => set({ messages }),
  clearMessages: () => set({ messages: [] }),

  // Streaming
  isStreaming: false,
  setIsStreaming: (isStreaming) => set({ isStreaming }),
  stopStreaming: () => set({ isStreaming: false }),
  continueStreaming: () => set({ isStreaming: true }),
  canContinueStreaming: false,
  setCanContinueStreaming: (canContinueStreaming) => set({ canContinueStreaming }),

  // Thinking per conversation
  thinkingByConversation: {},
  thinkingByAssistantTurn: {},
  isThinkingExpanded: false,
  setCurrentThinking: (conversationId, currentThinking) =>
    set((state) => ({
      thinkingByConversation: {
        ...state.thinkingByConversation,
        [conversationId]: currentThinking,
      },
    })),
  appendThinking: (conversationId, thinking) =>
    set((state) => ({
      thinkingByConversation: {
        ...state.thinkingByConversation,
        [conversationId]: (state.thinkingByConversation[conversationId] || '') + thinking,
      },
    })),
  getCurrentThinking: (conversationId) =>
    conversationId ? get().thinkingByConversation[conversationId] || '' : '',
  setThinkingForAssistantTurn: (conversationId, assistantTurn, thinking) =>
    set((state) => {
      const existingTurns = [...(state.thinkingByAssistantTurn[conversationId] || [])];
      while (existingTurns.length <= assistantTurn) {
        existingTurns.push('');
      }
      existingTurns[assistantTurn] = thinking;
      return {
        thinkingByAssistantTurn: {
          ...state.thinkingByAssistantTurn,
          [conversationId]: existingTurns,
        },
      };
    }),
  appendThinkingForAssistantTurn: (conversationId, assistantTurn, thinking) =>
    set((state) => {
      const existingTurns = [...(state.thinkingByAssistantTurn[conversationId] || [])];
      while (existingTurns.length <= assistantTurn) {
        existingTurns.push('');
      }
      existingTurns[assistantTurn] = (existingTurns[assistantTurn] || '') + thinking;
      return {
        thinkingByAssistantTurn: {
          ...state.thinkingByAssistantTurn,
          [conversationId]: existingTurns,
        },
      };
    }),
  getThinkingForAssistantTurn: (conversationId, assistantTurn) => {
    if (!conversationId || assistantTurn < 0) return '';
    const turns = get().thinkingByAssistantTurn[conversationId] || [];
    return turns[assistantTurn] || '';
  },
  toggleThinkingExpanded: () =>
    set((state) => ({
      isThinkingExpanded: !state.isThinkingExpanded,
    })),
  setThinkingExpanded: (isThinkingExpanded) => set({ isThinkingExpanded }),

  // Files
  pendingFiles: [],
  setPendingFiles: (pendingFiles) => set({ pendingFiles }),
  clearPendingFiles: () => set({ pendingFiles: [] }),

  // User
  username: 'GAKR',
  setUsername: (username) => set({ username }),
}));
