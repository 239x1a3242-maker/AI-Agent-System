import { useEffect, useRef, useCallback, useState } from 'react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useChatStore } from '@/store/chatStore';
import { ChatMessage } from './ChatMessage';
import { ChatInput } from './ChatInput';
import { ThinkPanel } from '@/components/ThinkPanel/ThinkPanel';
import {
  streamChat,
  getMessages,
  getConversations,
  stopStreaming as stopStreamingAPI,
  continueStreaming as continueStreamingAPI,
  checkStreamingState,
  abortActiveStream,
  StreamAbortedError,
} from '@/services/api';
import type { Message } from '@/types';
import { toast } from 'sonner';

const STREAMING_PLACEHOLDER_ID = -1;

export function Chat() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const activeConversationIdRef = useRef<number | null>(null);
  const streamConversationIdRef = useRef<number | null>(null);
  const streamHasVisibleTokenRef = useRef(false);
  const loadRequestSeqRef = useRef(0);
  const [continueNotice, setContinueNotice] = useState('');
  const [streamStatus, setStreamStatus] = useState('');
  const [streamStatusConversationId, setStreamStatusConversationId] = useState<number | null>(null);

  const {
    messages,
    currentConversationId,
    username,
    pendingFiles,
    isStreaming,
    setIsStreaming,
    addMessage,
    setMessages,
    updateLastMessage,
    clearPendingFiles,
    setConversations,
    setCurrentConversation,
    stopStreaming,
    continueStreaming,
    canContinueStreaming,
    setCanContinueStreaming,
    getCurrentThinking,
    setCurrentThinking,
  } = useChatStore();

  const loadConversationMessages = useCallback(async (conversationId: number) => {
    const requestSeq = ++loadRequestSeqRef.current;
    try {
      const data = await getMessages(username, conversationId);
      if (requestSeq !== loadRequestSeqRef.current) return;
      if (useChatStore.getState().currentConversationId !== conversationId) return;

      setMessages(data.messages);
      
      // Check streaming state
      const streamingState = await checkStreamingState(conversationId, username);
      if (requestSeq !== loadRequestSeqRef.current) return;
      if (useChatStore.getState().currentConversationId !== conversationId) return;

      setCanContinueStreaming(streamingState.canContinue);
      setContinueNotice(streamingState.canContinue ? 'Generation stopped.' : '');
    } catch (error) {
      if (requestSeq !== loadRequestSeqRef.current) return;
      console.error('Failed to load messages:', error);
      toast.error('Failed to load conversation');
    }
  }, [setCanContinueStreaming, setMessages, username]);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  // Load messages when conversation changes
  useEffect(() => {
    if (currentConversationId) {
      const frameId = window.requestAnimationFrame(() => {
        void loadConversationMessages(currentConversationId);
      });
      return () => window.cancelAnimationFrame(frameId);
    } else if (!isStreaming) {
      const frameId = window.requestAnimationFrame(() => {
        setMessages([]);
        setCanContinueStreaming(false);
        setContinueNotice('');
        setStreamStatus('');
        setStreamStatusConversationId(null);
        streamHasVisibleTokenRef.current = false;
      });
      return () => window.cancelAnimationFrame(frameId);
    }
    return undefined;
  }, [currentConversationId, isStreaming, loadConversationMessages, setCanContinueStreaming, setMessages]);

  const ensureAssistantContinueAnchor = useCallback((conversationId: number | null) => {
    const currentMessages = useChatStore.getState().messages;
    const lastMessage = currentMessages[currentMessages.length - 1];
    if (!lastMessage || lastMessage.role === 'user') {
      const assistantPlaceholder: Message = {
        id: Date.now() + 1,
        conversation_id: conversationId || currentConversationId || 0,
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
      };
      addMessage(assistantPlaceholder);
    }
  }, [addMessage, currentConversationId]);

  const markInterruptedForContinue = useCallback(async (notice?: string) => {
    const conversationId = activeConversationIdRef.current ?? currentConversationId;
    const interruptionNotice = (notice && notice.trim()) || 'Server busy. Try Continue.';

    stopStreaming();
    setIsStreaming(false);
    setStreamStatus('');
    setStreamStatusConversationId(null);
    streamHasVisibleTokenRef.current = false;
    activeConversationIdRef.current = null;
    streamConversationIdRef.current = null;
    setContinueNotice(interruptionNotice);

    if (!conversationId) {
      return;
    }

    try {
      // Ensure backend state becomes resumable for this conversation.
      await stopStreamingAPI(conversationId, username);
    } catch (error) {
      console.error('Failed to force stop during interruption:', error);
    }

    ensureAssistantContinueAnchor(conversationId);
    setCanContinueStreaming(true);
  }, [currentConversationId, username, stopStreaming, setIsStreaming, ensureAssistantContinueAnchor, setCanContinueStreaming]);

  const toContinueNotice = useCallback((raw?: string) => {
    const rawText = (raw || '').trim();
    const text = rawText.toLowerCase();
    if (!text) {
      return 'Server busy. Try Continue.';
    }
    if (text.includes('no stream data received from model server')) {
      return 'Model returned no stream tokens. Click Continue.';
    }
    if (text.includes('failed after') || text.includes('http ') || text.includes('request timeout')) {
      return `Model request failed: ${rawText.slice(0, 180)}`;
    }
    if (
      text.includes('network') ||
      text.includes('fetch') ||
      text.includes('connection') ||
      text.includes('timeout')
    ) {
      return 'Internet connection interrupted. Click Continue.';
    }
    return 'Server busy. Try Continue.';
  }, []);

  const normalizeStatusText = useCallback((rawStatus: string) => {
    const normalized = String(rawStatus || '').trim();
    if (!normalized) return '';

    if (normalized.toLowerCase() === 'thinking') {
      return 'Thinking';
    }
    return normalized;
  }, []);

  const handleStopStreaming = useCallback(async () => {
    const conversationId = activeConversationIdRef.current ?? currentConversationId;
    if (!conversationId) return;
    
    try {
      // Stop reading from active SSE immediately in the browser.
      abortActiveStream();
      const result = await stopStreamingAPI(conversationId, username);
      stopStreaming();
      setStreamStatus('');
      setStreamStatusConversationId(null);
      streamHasVisibleTokenRef.current = false;
      activeConversationIdRef.current = null;
      streamConversationIdRef.current = null;
      setContinueNotice('Generation stopped.');
      ensureAssistantContinueAnchor(conversationId);
      setCanContinueStreaming(true);
      toast.success(result.message || 'Streaming stopped');
    } catch (error) {
      console.error('Failed to stop streaming:', error);
      toast.error('Failed to stop streaming');
    }
  }, [currentConversationId, username, stopStreaming, setCanContinueStreaming, ensureAssistantContinueAnchor]);

  const handleContinueStreaming = useCallback(async () => {
    if (!currentConversationId) return;
    
    continueStreaming();
    activeConversationIdRef.current = currentConversationId;
    streamConversationIdRef.current = currentConversationId;
    streamHasVisibleTokenRef.current = false;
    setContinueNotice('');
    setCanContinueStreaming(false); // Reset continue state
    setStreamStatusConversationId(currentConversationId);
    setStreamStatus('Thinking');
    let hasReceivedToken = false;
    let hasStreamError = false;

    try {
      const callbacks = {
        onToken: (token: string) => {
          hasReceivedToken = true;
          if (!streamHasVisibleTokenRef.current) {
            streamHasVisibleTokenRef.current = true;
            setStreamStatus('');
          }
          const state = useChatStore.getState();
          if (state.currentConversationId !== streamConversationIdRef.current) {
            return;
          }
          // Update the last message (assistant's response)
          const currentMessages = state.messages;
          const lastMessage = currentMessages[currentMessages.length - 1];
          if (lastMessage && lastMessage.role === 'assistant') {
            updateLastMessage(token);
          }
        },
        onThinking: (thinking: string) => {
          const state = useChatStore.getState();
          const convId = streamConversationIdRef.current ?? state.currentConversationId;
          if (convId != null) {
            state.appendThinking(convId, thinking);
            if (!state.isThinkingExpanded) {
              state.setThinkingExpanded(true);
            }
          }
        },
        onToolCall: (tool: string) => {
          console.log('[Chat.tsx::continue] tool_call:', tool);
        },
        onToolResult: (result: unknown) => {
          console.log('[Chat.tsx::continue] tool_result:', result);
        },
        onStatus: (status: string) => {
          if (streamHasVisibleTokenRef.current) return;
          setStreamStatus(normalizeStatusText(status));
        },
        onError: (error: string) => {
          console.error('Stream onError (continue):', error);
          hasStreamError = true;
          setStreamStatus('');
          setStreamStatusConversationId(null);
          streamHasVisibleTokenRef.current = false;
          const notice = toContinueNotice(error);
          toast.error(notice);
          void markInterruptedForContinue(notice);
        },
        onDone: () => {
          setIsStreaming(false);
          setStreamStatus('');
          setStreamStatusConversationId(null);
          streamHasVisibleTokenRef.current = false;
          activeConversationIdRef.current = null;
          streamConversationIdRef.current = null;
        },
      };

      await continueStreamingAPI(currentConversationId, username, callbacks);
      if (!hasStreamError && hasReceivedToken) {
        toast.success('Streaming continued');
      }
    } catch (error) {
      if (error instanceof StreamAbortedError) {
        return;
      }
      console.error('Failed to continue streaming:', error);
      const notice = toContinueNotice(error instanceof Error ? error.message : '');
      toast.error(notice);
      await markInterruptedForContinue(notice);
    }
  }, [currentConversationId, username, continueStreaming, setCanContinueStreaming, updateLastMessage, setIsStreaming, markInterruptedForContinue, toContinueNotice, normalizeStatusText]);

  const handleSendMessage = useCallback(
    async (content: string) => {
      if (!content.trim() && pendingFiles.length === 0) return;

      // Add user message to UI immediately
      const userMessage: Message = {
        id: Date.now(),
        conversation_id: currentConversationId || 0,
        role: 'user',
        content,
        timestamp: new Date().toISOString(),
      };
      addMessage(userMessage);

      // Start streaming
      setIsStreaming(true);
      activeConversationIdRef.current = currentConversationId;
      streamConversationIdRef.current = currentConversationId;
      streamHasVisibleTokenRef.current = false;
      setContinueNotice('');
      setCanContinueStreaming(false); // Reset continue state for new message
      // Clear thinking for this conversation on new message
      if (currentConversationId != null) {
        setCurrentThinking(currentConversationId, '');
      }
      setStreamStatusConversationId(currentConversationId);
      setStreamStatus(pendingFiles.length > 0 ? 'Uploading and processing files...' : 'Thinking');

      let conversationId = currentConversationId;

      try {
        const callbacks = {
          onToken: (token: string) => {
            if (!streamHasVisibleTokenRef.current) {
              streamHasVisibleTokenRef.current = true;
              setStreamStatus('');
            }
            const state = useChatStore.getState();
            if (state.currentConversationId !== streamConversationIdRef.current) {
              return;
            }
            // Update the last message (assistant's response)
            const currentMessages = state.messages;
            const lastMessage = currentMessages[currentMessages.length - 1];
            if (lastMessage && lastMessage.role === 'assistant') {
              updateLastMessage(token);
            } else {
              // Create new assistant message
              const assistantMessage: Message = {
                id: Date.now() + 1,
                conversation_id: conversationId || 0,
                role: 'assistant',
                content: token,
                timestamp: new Date().toISOString(),
              };
              addMessage(assistantMessage);
            }
          },
          onThinking: (thinking: string) => {
            const state = useChatStore.getState();
            const convId = streamConversationIdRef.current ?? state.currentConversationId;
            if (convId != null) {
              state.appendThinking(convId, thinking);
              if (!state.isThinkingExpanded) {
                state.setThinkingExpanded(true);
              }
            }
          },
          onToolCall: (tool: string) => {
            console.log('[Chat.tsx::send] tool_call:', tool);
          },
          onToolResult: (result: unknown) => {
            console.log('[Chat.tsx::send] tool_result:', result);
          },
          onStatus: (status: string) => {
            if (streamHasVisibleTokenRef.current) return;
            setStreamStatus(normalizeStatusText(status));
          },
          onError: (error: string) => {
            console.error('Stream onError (send):', error);
            setStreamStatus('');
            setStreamStatusConversationId(null);
            streamHasVisibleTokenRef.current = false;
            const notice = toContinueNotice(error);
            toast.error(notice);
            void markInterruptedForContinue(notice);
          },
          onDone: () => {
            setIsStreaming(false);
            setStreamStatus('');
            setStreamStatusConversationId(null);
            streamHasVisibleTokenRef.current = false;
            activeConversationIdRef.current = null;
            streamConversationIdRef.current = null;
          },
        };

        conversationId = await streamChat(content, callbacks, {
          conversationId: currentConversationId || undefined,
          username,
          files: pendingFiles,
          onConversationId: (id: number) => {
            activeConversationIdRef.current = id;
            streamConversationIdRef.current = id;
            setStreamStatusConversationId(id);
            if (!useChatStore.getState().currentConversationId) {
              setCurrentConversation(id);
            }
          },
        });

        // Update conversation ID if new
        if (!currentConversationId && conversationId) {
          setCurrentConversation(conversationId);
          // Refresh conversations list
          const conversations = await getConversations(username);
          setConversations(conversations);
        }

        clearPendingFiles();
      } catch (error) {
        if (error instanceof StreamAbortedError) {
          return;
        }
        console.error('Chat error:', error);
        const notice = toContinueNotice(error instanceof Error ? error.message : '');
        toast.error(notice);
        await markInterruptedForContinue(notice);
      }
    },
    [
      addMessage,
      clearPendingFiles,
      currentConversationId,
      markInterruptedForContinue,
      normalizeStatusText,
      pendingFiles,
      setCanContinueStreaming,
      setConversations,
      setCurrentConversation,
      setIsStreaming,
      toContinueNotice,
      updateLastMessage,
      username,
    ]
  );

  const hasMessages = messages.length > 0;
  const showStreamStatusForCurrentConversation =
    Boolean(streamStatus) &&
    isStreaming &&
    (
      currentConversationId == null ||
      streamStatusConversationId == null ||
      streamStatusConversationId === currentConversationId
    );

  return (
    <div className="flex flex-col h-full">
      {/* Messages area */}
      <div className="flex-1 overflow-hidden">
        {hasMessages ? (
          <ScrollArea className="h-full" ref={scrollRef}>
            <div className="max-w-5xl mx-auto py-4">
              {/* Think Panel */}
              {(getCurrentThinking(currentConversationId) || isStreaming) && (
                <ThinkPanel />
              )}
              {/* Messages */}
              {messages.map((message, index) => (
                <ChatMessage
                  key={message.id}
                  message={message}
                  isStreaming={
                    isStreaming &&
                    index === messages.length - 1 &&
                    message.role === 'assistant'
                  }
                  statusText={
                    showStreamStatusForCurrentConversation &&
                    message.role === 'assistant' &&
                    index === messages.length - 1
                      ? streamStatus
                      : undefined
                  }
                  canContinue={
                    !isStreaming &&
                    message.role === 'assistant' &&
                    index === messages.length - 1 &&
                    canContinueStreaming
                  }
                  continueNotice={continueNotice}
                  onContinue={handleContinueStreaming}
                />
              ))}

              {/* Streaming indicator for empty response */}
              {isStreaming &&
                (messages.length === 0 ||
                  messages[messages.length - 1]?.role === 'user') && (
                  <ChatMessage
                    message={{
                      id: STREAMING_PLACEHOLDER_ID,
                      conversation_id: currentConversationId || 0,
                      role: 'assistant',
                      content: '',
                      timestamp: new Date().toISOString(),
                    }}
                    isStreaming={true}
                    statusText={showStreamStatusForCurrentConversation ? streamStatus : undefined}
                  />
                )}

              <div ref={messagesEndRef} />
            </div>
          </ScrollArea>
        ) : (
          <div className="h-full flex flex-col items-center justify-center p-8">
            <div className="text-center max-w-md">
              <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mx-auto mb-6">
                <span className="text-3xl">🤖</span>
              </div>
              <h1 className="text-2xl font-bold mb-2">
                Welcome to AI Agent System
              </h1>
              <p className="text-muted-foreground mb-6">
                Start a conversation with your AI assistant. I can help you with
                web search, file operations, email sending, and more.
              </p>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="p-3 rounded-lg bg-muted">
                  <span className="font-medium">🔍 Web Search</span>
                  <p className="text-muted-foreground text-xs mt-1">
                    Search the internet for current information
                  </p>
                </div>
                <div className="p-3 rounded-lg bg-muted">
                  <span className="font-medium">📁 File Operations</span>
                  <p className="text-muted-foreground text-xs mt-1">
                    Read, write, and manage files in workspace
                  </p>
                </div>
                <div className="p-3 rounded-lg bg-muted">
                  <span className="font-medium">📧 Email</span>
                  <p className="text-muted-foreground text-xs mt-1">
                    Send emails directly from the chat
                  </p>
                </div>
                <div className="p-3 rounded-lg bg-muted">
                  <span className="font-medium">📎 File Upload</span>
                  <p className="text-muted-foreground text-xs mt-1">
                    Upload and analyze documents, images, and more
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="max-w-5xl mx-auto w-full">
        <ChatInput
          onSend={handleSendMessage}
          disabled={isStreaming}
          isStreaming={isStreaming}
          onStop={handleStopStreaming}
        />
      </div>
    </div>
  );
}
