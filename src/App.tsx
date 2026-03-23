import { useState, useEffect, useRef } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Toaster } from '@/components/ui/sonner';
import { Sidebar } from '@/components/Sidebar/Sidebar';
import { Chat } from '@/components/Chat/Chat';
import { Workspace } from '@/components/Workspace/Workspace';
import { useTheme } from '@/hooks/useTheme';
import { cn } from '@/lib/utils';
import './App.css';

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 1024);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [chatPanePercent, setChatPanePercent] = useState(50);
  const [isResizingWorkspace, setIsResizingWorkspace] = useState(false);
  const workspaceContainerRef = useRef<HTMLDivElement>(null);
  const { theme } = useTheme();

  // Keep sidebar closed by default on small screens.
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 1024) {
        setSidebarOpen(false);
      }
    };

    handleResize();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    if (!isResizingWorkspace) return;

    const handleMouseMove = (event: MouseEvent) => {
      const container = workspaceContainerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      if (rect.width <= 0) return;

      const nextPercent = ((event.clientX - rect.left) / rect.width) * 100;
      const clamped = Math.max(30, Math.min(70, nextPercent));
      setChatPanePercent(clamped);
    };

    const handleMouseUp = () => {
      setIsResizingWorkspace(false);
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizingWorkspace]);

  return (
    <div className={`min-h-screen bg-background text-foreground ${theme}`}>
      <div className="flex h-screen overflow-hidden">
        <Sidebar
          isOpen={sidebarOpen}
          onToggle={() => setSidebarOpen(!sidebarOpen)}
          workspaceOpen={workspaceOpen}
          onWorkspaceOpenChange={setWorkspaceOpen}
        />

        <main
          className={cn(
            'flex-1 flex flex-col min-w-0 transition-[margin] duration-300 ease-in-out',
            sidebarOpen ? 'lg:ml-72' : 'lg:ml-0'
          )}
        >
          <header className="h-14 border-b border-border flex items-center px-4 gap-4">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="shrink-0"
            >
              {sidebarOpen ? <ChevronLeft className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
            </Button>

            <div className="flex-1" />

            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground hidden sm:inline">
                AI Agent System
              </span>
            </div>
          </header>

          <div className="flex-1 overflow-hidden flex" ref={workspaceContainerRef}>
            <div
              className={cn(
                'h-full min-w-0',
                workspaceOpen && !isResizingWorkspace
                  ? 'transition-[width] duration-300 ease-in-out'
                  : ''
              )}
              style={workspaceOpen ? { width: `${chatPanePercent}%` } : { width: '100%' }}
            >
              <Chat />
            </div>
            {workspaceOpen && (
              <>
                <div
                  role="separator"
                  aria-orientation="vertical"
                  aria-label="Resize workspace panel"
                  onMouseDown={(event) => {
                    event.preventDefault();
                    setIsResizingWorkspace(true);
                  }}
                  className={cn(
                    'h-full w-1 cursor-col-resize border-l border-r border-border',
                    isResizingWorkspace ? 'bg-border' : 'bg-transparent hover:bg-border/60'
                  )}
                />
                <div
                  className={cn(
                    'h-full min-w-0',
                    !isResizingWorkspace ? 'transition-[width] duration-300 ease-in-out' : ''
                  )}
                  style={{ width: `${100 - chatPanePercent}%` }}
                >
                  <Workspace open={workspaceOpen} onOpenChange={setWorkspaceOpen} />
                </div>
              </>
            )}
          </div>
        </main>
      </div>

      <Toaster position="top-right" />
    </div>
  );
}

export default App;
