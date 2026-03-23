import { useState, useRef } from 'react';
import { Send, Paperclip, X, FileText, Square } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useChatStore } from '@/store/chatStore';
import { cn } from '@/lib/utils';

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled?: boolean;
  isStreaming?: boolean;
  onStop?: () => void;
}

export function ChatInput({ onSend, disabled, isStreaming, onStop }: ChatInputProps) {
  const [message, setMessage] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { pendingFiles, setPendingFiles } = useChatStore();

  const handleSend = () => {
    if (!message.trim() && pendingFiles.length === 0) return;
    onSend(message);
    setMessage('');
    setPendingFiles([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.FormEvent<HTMLTextAreaElement>) => {
    const target = e.target as HTMLTextAreaElement;
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length > 0) {
      const currentFiles = pendingFiles || [];
      const newFiles = [...currentFiles, ...files].slice(0, 5);
      setPendingFiles(newFiles);
    }
    // Reset input
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const removeFile = (index: number) => {
    const newFiles = pendingFiles.filter((_, i) => i !== index);
    setPendingFiles(newFiles);
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div className="border-t border-border bg-card p-4">
      {/* File attachments preview */}
      {pendingFiles.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3">
          {pendingFiles.map((file, index) => (
            <div
              key={index}
              className={cn(
                'flex items-center gap-2 px-3 py-2 rounded-lg',
                'bg-muted border border-border'
              )}
            >
              <FileText className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm truncate max-w-[150px]">{file.name}</span>
              <span className="text-xs text-muted-foreground">
                {formatFileSize(file.size)}
              </span>
              <button
                onClick={() => removeFile(index)}
                className="ml-1 p-1 hover:bg-muted-foreground/20 rounded"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Input area */}
      <div className="flex items-end gap-2">
        {/* File upload button */}
        <Button
          variant="ghost"
          size="icon"
          className="flex-shrink-0"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || pendingFiles.length >= 5}
        >
          <Paperclip className="h-5 w-5" />
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".txt,.md,.py,.js,.html,.css,.yaml,.yml,.xml,.sql,.log,.json,.csv,.pdf,.docx,.xlsx,.xlsm,.xls,.jpg,.jpeg,.png,.webp,.bmp,.gif"
          className="hidden"
          onChange={handleFileSelect}
        />

        {/* Text input */}
        <div className="flex-1 relative">
          <Textarea
            ref={textareaRef}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            placeholder="Type your message..."
            disabled={disabled}
            className="min-h-[44px] max-h-[200px] resize-none pr-12"
            rows={1}
          />
        </div>

        {/* Send/Stop/Continue buttons */}
        <div className="flex gap-2">
          {isStreaming ? (
            <Button
              onClick={onStop}
              variant="destructive"
              size="sm"
              className="flex-shrink-0"
            >
              <Square className="h-4 w-4 mr-2" />
              Stop
            </Button>
          ) : (
            <Button
              onClick={handleSend}
              disabled={disabled || (!message.trim() && pendingFiles.length === 0)}
              className="flex-shrink-0"
            >
              <Send className="h-4 w-4 mr-2" />
              Send
            </Button>
          )}
        </div>
      </div>

      {/* File limit hint */}
      <div className="mt-2 text-xs text-muted-foreground text-center">
        {pendingFiles.length > 0 && (
          <span>{pendingFiles.length}/5 files attached</span>
        )}
      </div>
    </div>
  );
}
