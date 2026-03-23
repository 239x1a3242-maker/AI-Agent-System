import { ChevronRight, ChevronDown, Brain } from 'lucide-react';
import { useChatStore } from '@/store/chatStore';
import { cn } from '@/lib/utils';

export function ThinkPanel() {
  const {
    currentConversationId,
    getCurrentThinking,
    isThinkingExpanded,
    toggleThinkingExpanded,
    isStreaming,
  } = useChatStore();

  const currentThinking = getCurrentThinking(currentConversationId);

  if (!currentThinking && !isStreaming) {
    return null;
  }

  return (
    <div className="mb-4">
      <button
        onClick={toggleThinkingExpanded}
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-lg',
          'bg-muted/50 hover:bg-muted transition-colors',
          'text-sm font-medium text-muted-foreground'
        )}
      >
        {isThinkingExpanded ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        <Brain className="h-4 w-4" />
        <span>Think</span>
        {isStreaming && (
          <span className="ml-2 flex items-center gap-1">
            <span className="w-1.5 h-1.5 bg-primary rounded-full animate-pulse" />
            <span className="w-1.5 h-1.5 bg-primary rounded-full animate-pulse delay-75" />
            <span className="w-1.5 h-1.5 bg-primary rounded-full animate-pulse delay-150" />
          </span>
        )}
      </button>

      {isThinkingExpanded && currentThinking && (
        <div className="mt-2 p-4 rounded-lg bg-muted/30 border border-border">
          <div className="flex items-start gap-3">
            <Brain className="h-5 w-5 text-primary mt-0.5 flex-shrink-0" />
            <div className="flex-1">
              <div className="text-xs font-medium text-muted-foreground mb-2">
                Reasoning Process
              </div>
              <div className="text-sm text-foreground whitespace-pre-wrap font-mono leading-relaxed">
                {currentThinking}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
