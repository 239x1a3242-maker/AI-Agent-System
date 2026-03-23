import { useState, useEffect, useCallback } from 'react';
import {
  Plus,
  MessageSquare,
  Pencil,
  Trash2,
  Settings as SettingsIcon,
  Sun,
  Moon,
  X,
  Loader2,
  FolderOpen,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ScrollArea } from '@/components/ui/scroll-area';
import { useChatStore } from '@/store/chatStore';
import { useTheme } from '@/hooks/useTheme';
import { Settings as SettingsDialog } from '../Settings/Settings';
import {
  getConversations,
  createConversation,
  deleteConversation,
  renameConversation,
} from '@/services/api';
import type { Conversation } from '@/types';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

interface SidebarProps {
  isOpen: boolean;
  onToggle: () => void;
  workspaceOpen: boolean;
  onWorkspaceOpenChange: (open: boolean) => void;
}

export function Sidebar({ isOpen, onToggle, workspaceOpen, onWorkspaceOpenChange }: SidebarProps) {
  const {
    conversations,
    setConversations,
    currentConversationId,
    setCurrentConversation,
    addConversation,
    removeConversation,
    updateConversation,
    setMessages,
    username,
    setCurrentThinking,
    setThinkingExpanded,
  } = useChatStore();

  const { theme, toggleTheme } = useTheme();
  const [isLoading, setIsLoading] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [conversationToDelete, setConversationToDelete] = useState<Conversation | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);

  const loadConversations = useCallback(async () => {
    try {
      setIsLoading(true);
      const data = await getConversations(username);
      setConversations(data);
    } catch (error) {
      console.error('Failed to load conversations:', error);
      toast.error('Failed to load conversations');
    } finally {
      setIsLoading(false);
    }
  }, [setConversations, username]);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  const getNextChatTitle = () => {
    const chatNumbers = conversations
      .map((conv) => {
        const match = conv.title.trim().match(/^chat\s+(\d+)$/i);
        return match ? parseInt(match[1], 10) : null;
      })
      .filter((n): n is number => n !== null);

    const nextNumber = chatNumbers.length > 0 ? Math.max(...chatNumbers) + 1 : 1;
    return `Chat ${nextNumber}`;
  };

  const handleNewChat = async () => {
    try {
      setIsCreating(true);
      const conversation = await createConversation(username, getNextChatTitle());
      addConversation(conversation);
      setCurrentConversation(conversation.id);
      setMessages([]);
      setCurrentThinking(conversation.id, '');
      setThinkingExpanded(false);
      toast.success('New chat created');
    } catch (error) {
      console.error('Failed to create conversation:', error);
      toast.error('Failed to create new chat');
    } finally {
      setIsCreating(false);
    }
  };

  const handleSelectConversation = (id: number) => {
    setCurrentConversation(id);
    setCurrentThinking(id, '');
    setThinkingExpanded(false);
  };

  const handleDeleteClick = (conversation: Conversation, e: React.MouseEvent) => {
    e.stopPropagation();
    setConversationToDelete(conversation);
    setDeleteDialogOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (!conversationToDelete) return;

    try {
      await deleteConversation(username, conversationToDelete.id);
      removeConversation(conversationToDelete.id);
      if (currentConversationId === conversationToDelete.id) {
        setCurrentConversation(null);
        setMessages([]);
        setCurrentThinking(currentConversationId, '');
      }
      toast.success('Conversation deleted');
    } catch (error) {
      console.error('Failed to delete conversation:', error);
      toast.error('Failed to delete conversation');
    } finally {
      setDeleteDialogOpen(false);
      setConversationToDelete(null);
    }
  };

  const handleRenameClick = (conversation: Conversation, e: React.MouseEvent) => {
    e.stopPropagation();
    setRenamingId(conversation.id);
    setRenameValue(conversation.title);
  };

  const handleRenameSubmit = async (conversationId: number) => {
    if (!renameValue.trim()) {
      setRenamingId(null);
      return;
    }

    try {
      await renameConversation(username, conversationId, renameValue.trim());
      updateConversation(conversationId, { title: renameValue.trim() });
      toast.success('Conversation renamed');
    } catch (error) {
      console.error('Failed to rename conversation:', error);
      toast.error('Failed to rename conversation');
    } finally {
      setRenamingId(null);
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    const now = new Date();
    const diffDays = Math.floor((now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24));

    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    return date.toLocaleDateString();
  };

  const getDisplayTitle = (title: string, conversationId: number) => {
    const fullTitle = (title || '').trim() || `Chat ${conversationId}`;
    if (fullTitle.length <= 10) return fullTitle;
    return `${fullTitle.slice(0, 10)}...`;
  };

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 lg:hidden"
          onClick={onToggle}
        />
      )}

      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-50 w-72 bg-card border-r border-border',
          'flex min-h-0 flex-col overflow-hidden transition-transform duration-300 ease-in-out',
          isOpen ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        <div className="p-4 border-b border-border">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
                <span className="text-primary-foreground font-bold text-sm">AI</span>
              </div>
              <span className="font-semibold">Agent System</span>
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              onClick={onToggle}
            >
              <X className="h-5 w-5" />
            </Button>
          </div>

          <Button
            onClick={handleNewChat}
            disabled={isCreating}
            className="w-full justify-start gap-2"
            variant="outline"
          >
            {isCreating ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Plus className="h-4 w-4" />
            )}
            New Chat
          </Button>

          <Button
            onClick={() => onWorkspaceOpenChange(!workspaceOpen)}
            className="w-full justify-start gap-2 mt-2"
            variant={workspaceOpen ? 'default' : 'outline'}
          >
            <FolderOpen className="h-4 w-4" />
            Workspace
          </Button>
        </div>

        <div className="flex-1 min-h-0">
          <ScrollArea className="h-full">
            <div className="p-2 space-y-1">
              <div className="px-3 py-2 text-xs font-medium text-muted-foreground uppercase">
                Recent Conversations
              </div>

              {isLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              ) : (
                <>
                  {conversations.map((conversation) => (
                    <div
                      key={conversation.id}
                      onClick={() => handleSelectConversation(conversation.id)}
                      className={cn(
                        'group relative flex items-center gap-2 px-3 py-2 pr-16 rounded-lg cursor-pointer',
                        'hover:bg-accent transition-colors',
                        currentConversationId === conversation.id && 'bg-accent'
                      )}
                    >
                      <MessageSquare className="h-4 w-4 text-muted-foreground flex-shrink-0" />
                      <div className="flex-1 min-w-0">
                        {renamingId === conversation.id ? (
                          <input
                            type="text"
                            value={renameValue}
                            onChange={(e) => setRenameValue(e.target.value)}
                            onBlur={() => handleRenameSubmit(conversation.id)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                handleRenameSubmit(conversation.id);
                              } else if (e.key === 'Escape') {
                                setRenamingId(null);
                              }
                            }}
                            autoFocus
                            className="w-full bg-transparent border border-border rounded px-2 py-1 text-sm outline-none focus:ring-1 focus:ring-primary"
                            onClick={(e) => e.stopPropagation()}
                          />
                        ) : (
                          <>
                            <div className="min-w-0 flex-1">
                              <div
                                className="truncate text-sm font-medium"
                                title={(conversation.title || '').trim() || `Chat ${conversation.id}`}
                              >
                                {getDisplayTitle(conversation.title, conversation.id)}
                              </div>
                              <div className="mt-0.5 text-xs text-muted-foreground truncate">
                                {formatDate(conversation.updated_at)}
                              </div>
                            </div>
                            <div className="absolute right-2 top-1/2 -translate-y-1/2 inline-flex items-center rounded-full border border-border bg-background/90 p-0.5 opacity-0 invisible transition-all duration-150 group-hover:opacity-100 group-hover:visible group-focus-within:opacity-100 group-focus-within:visible">
                              <button
                                type="button"
                                className="h-5 w-5 rounded-full flex items-center justify-center text-foreground hover:bg-accent"
                                onClick={(e) => handleRenameClick(conversation, e)}
                                title="Rename conversation"
                              >
                                <Pencil className="h-2.5 w-2.5" />
                              </button>
                              <button
                                type="button"
                                className="h-5 w-5 rounded-full flex items-center justify-center text-destructive hover:bg-destructive/10"
                                onClick={(e) => handleDeleteClick(conversation, e)}
                                title="Delete conversation"
                              >
                                <Trash2 className="h-2.5 w-2.5" />
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  ))}

                  {conversations.length === 0 && (
                    <div className="px-3 py-8 text-center text-muted-foreground text-sm">
                      No conversations yet.
                      <br />
                      Click "New Chat" to begin.
                    </div>
                  )}
                </>
              )}
            </div>
          </ScrollArea>
        </div>

        <div className="p-4 border-t border-border space-y-2">
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            onClick={toggleTheme}
          >
            {theme === 'dark' ? (
              <>
                <Sun className="h-4 w-4" />
                Light Mode
              </>
            ) : (
              <>
                <Moon className="h-4 w-4" />
                Dark Mode
              </>
            )}
          </Button>
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            onClick={() => setSettingsOpen(true)}
          >
            <SettingsIcon className="h-4 w-4" />
            Settings
          </Button>
        </div>
      </aside>

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Conversation</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete "{conversationToDelete?.title}"?
              This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleConfirmDelete}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
