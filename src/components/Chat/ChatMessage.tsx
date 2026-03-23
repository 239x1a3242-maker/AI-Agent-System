import { useState, type ComponentPropsWithoutRef } from 'react';
import { User, Bot, Copy, Check, FileText, Play, Brain } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { Message, FileAttachment } from '@/types';
import { cn } from '@/lib/utils';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus, vs } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { useTheme } from '@/hooks/useTheme';

type MarkdownNode = {
  position?: {
    start?: {
      line?: number;
    };
  };
};

type TableRendererProps = ComponentPropsWithoutRef<'table'> & {
  node?: MarkdownNode;
};

type CodeRendererProps = ComponentPropsWithoutRef<'code'> & {
  inline?: boolean;
  node?: MarkdownNode;
};

interface ChatMessageProps {
  message: Message;
  isStreaming?: boolean;
  statusText?: string;
  canContinue?: boolean;
  continueNotice?: string;
  onContinue?: () => void;
}

export function ChatMessage({
  message,
  isStreaming,
  statusText,
  canContinue,
  continueNotice,
  onContinue,
}: ChatMessageProps) {
  const isUser = message.role === 'user';
  const [copied, setCopied] = useState(false);
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const { isDark } = useTheme();
  const hasVisibleContent = Boolean((message.content || '').trim());

  const handleCopy = async () => {
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleCopyTarget = async (copyKey: string, text: string) => {
    const normalized = String(text || '').trim();
    if (!normalized) return;
    await navigator.clipboard.writeText(normalized);
    setCopiedTarget(copyKey);
    setTimeout(() => {
      setCopiedTarget((current) => (current === copyKey ? null : current));
    }, 2000);
  };

  const renderFileAttachments = (files?: FileAttachment[]) => {
    if (!files || files.length === 0) return null;

    return (
      <div className="flex flex-wrap gap-2 mb-3">
        {files.map((file, index) => (
          <div
            key={index}
            className={cn(
              'flex items-center gap-2 px-3 py-2 rounded-lg',
              'bg-muted/50 border border-border'
            )}
          >
            <FileText className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm truncate max-w-[200px]">{file.filename}</span>
            <span className="text-xs text-muted-foreground">
              {(file.size / 1024).toFixed(1)} KB
            </span>
            <span className={cn('text-xs', file.success ? 'text-emerald-600' : 'text-destructive')}>
              {file.success ? 'Processed' : 'Failed'}
            </span>
          </div>
        ))}
      </div>
    );
  };

  if (isUser) {
    return (
      <div className="flex justify-end p-4">
        <div className="max-w-[88%] flex flex-row-reverse gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center flex-shrink-0">
            <User className="h-4 w-4 text-primary-foreground" />
          </div>

          <div className="flex-1 min-w-0">
            <div className="bg-primary text-primary-foreground rounded-2xl rounded-tr-sm px-4 py-3">
              {renderFileAttachments(message.files)}
              <p className="whitespace-pre-wrap text-[1.02rem] leading-relaxed">{message.content}</p>
            </div>
            {message.timestamp_formatted && (
              <div className="text-right mt-1">
                <span className="text-xs text-muted-foreground">
                  {new Date(message.timestamp_formatted).toLocaleTimeString()}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group flex justify-start p-4">
      <div className="max-w-[95%] xl:max-w-[96%] flex gap-3">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
          <Bot className="h-4 w-4 text-primary" />
        </div>

        <div className="flex-1 min-w-0">
          {hasVisibleContent && (
            <div className="bg-muted/50 rounded-2xl rounded-tl-sm px-4 py-3">
              <div className="chat-markdown max-w-none text-[1.02rem] leading-relaxed">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                  h1({ children }) {
                    return <h1 className="text-xl font-bold mt-4 mb-2 first:mt-0">{children}</h1>;
                  },
                  h2({ children }) {
                    return <h2 className="text-lg font-semibold mt-4 mb-2 first:mt-0">{children}</h2>;
                  },
                  h3({ children }) {
                    return <h3 className="text-base font-semibold mt-3 mb-1.5 first:mt-0">{children}</h3>;
                  },
                  p({ children }) {
                    return <p className="my-2 leading-relaxed whitespace-pre-wrap">{children}</p>;
                  },
                  ul({ children }) {
                    return <ul className="list-disc pl-5 my-2 space-y-1">{children}</ul>;
                  },
                  ol({ children }) {
                    return <ol className="list-decimal pl-5 my-2 space-y-1">{children}</ol>;
                  },
                  li({ children }) {
                    return <li className="leading-relaxed">{children}</li>;
                  },
                  blockquote({ children }) {
                    return (
                      <blockquote className="my-3 border-l-4 border-border pl-3 text-muted-foreground">
                        {children}
                      </blockquote>
                    );
                  },
                  table({ children, node }: TableRendererProps) {
                    const copyKey = `table-${message.id}-${node?.position?.start?.line ?? '0'}`;
                    return (
                      <div className="my-3 relative overflow-x-auto rounded-lg border border-border bg-background/60">
                        <button
                          type="button"
                          onClick={(event) => {
                            const table = event.currentTarget.parentElement?.querySelector('table');
                            const tableText = table?.textContent ?? '';
                            void handleCopyTarget(copyKey, tableText);
                          }}
                          className="absolute right-2 top-2 z-10 h-7 w-7 rounded-md border border-border bg-background/90 text-muted-foreground hover:text-foreground hover:bg-background flex items-center justify-center"
                          title="Copy table"
                        >
                          {copiedTarget === copyKey ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                        </button>
                        <table className="w-full border-collapse text-base">{children}</table>
                      </div>
                    );
                  },
                  thead({ children }) {
                    return <thead className="bg-muted/70">{children}</thead>;
                  },
                  tr({ children }) {
                    return <tr className="border-b border-border last:border-b-0">{children}</tr>;
                  },
                  th({ children }) {
                    return <th className="px-3 py-2 text-left font-semibold">{children}</th>;
                  },
                  td({ children }) {
                    return <td className="px-3 py-2 align-top">{children}</td>;
                  },
                  hr() {
                    return <hr className="my-4 border-border" />;
                  },
                  a({ href, children }) {
                    return (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-primary underline underline-offset-2"
                      >
                        {children}
                      </a>
                    );
                  },
                  code({ inline, className, children, node, ...props }: CodeRendererProps) {
                    const match = /language-([\w-]+)/.exec(className || '');
                    const language = match?.[1] || 'text';
                    const isInlineCode = inline ?? !match;
                    return isInlineCode ? (
                      <code
                        className={cn(
                          'rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em]',
                          className
                        )}
                        {...props}
                      >
                        {children}
                      </code>
                    ) : (
                      <div className="relative my-3">
                        <button
                          type="button"
                          onClick={() => {
                            const codeText = String(children).replace(/\n$/, '');
                            const copyKey = `code-${message.id}-${node?.position?.start?.line ?? '0'}-${language}`;
                            void handleCopyTarget(copyKey, codeText);
                          }}
                          className="absolute right-2 top-2 z-10 h-7 w-7 rounded-md border border-border bg-background/90 text-muted-foreground hover:text-foreground hover:bg-background flex items-center justify-center"
                          title="Copy code"
                        >
                          {copiedTarget === `code-${message.id}-${node?.position?.start?.line ?? '0'}-${language}` ? (
                            <Check className="h-3.5 w-3.5" />
                          ) : (
                            <Copy className="h-3.5 w-3.5" />
                          )}
                        </button>
                        <SyntaxHighlighter
                          style={isDark ? vscDarkPlus : vs}
                          language={language}
                          PreTag="pre"
                          customStyle={{
                            margin: 0,
                            borderRadius: '0.5rem',
                            border: '1px solid hsl(var(--border))',
                            background: isDark ? '#1e1e1e' : '#f6f8fa',
                            padding: '0.9rem',
                            fontSize: '0.95rem',
                          }}
                          wrapLongLines
                        >
                          {String(children).replace(/\n$/, '')}
                        </SyntaxHighlighter>
                      </div>
                    );
                  },
                  }}
                >
                  {message.content}
                </ReactMarkdown>
              </div>
            </div>
          )}

          {statusText && (
            <div className="mt-2">
              <div
                className={cn(
                  'inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs border border-border',
                  'bg-muted text-muted-foreground font-medium'
                )}
              >
                <Brain className="h-3 w-3 text-primary" />
                <span className={cn(isStreaming ? 'thinking-shimmer' : '')}>{statusText}</span>
              </div>
            </div>
          )}

          {!isStreaming && hasVisibleContent && (
            <div className="flex items-center gap-2 mt-2 opacity-0 group-hover:opacity-100 transition-opacity">
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1 text-xs"
                onClick={handleCopy}
              >
                {copied ? (
                  <>
                    <Check className="h-3 w-3" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3 w-3" />
                    Copy
                  </>
                )}
              </Button>
            </div>
          )}

          {canContinue && (
            <div className="mt-2 flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                {continueNotice || 'Generation stopped.'}
              </span>
              <Button
                variant="default"
                size="sm"
                className="h-7 gap-1 text-xs"
                onClick={onContinue}
              >
                <Play className="h-3 w-3" />
                Continue
              </Button>
            </div>
          )}

          {message.timestamp_formatted && hasVisibleContent && (
            <div className="mt-1">
              <span className="text-xs text-muted-foreground">
                {new Date(message.timestamp_formatted).toLocaleTimeString()}
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
