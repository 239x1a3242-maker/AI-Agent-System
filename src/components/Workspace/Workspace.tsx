import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileCode,
  FilePlus2,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Save,
  Trash2,
  Upload,
  X,
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ScrollArea } from '@/components/ui/scroll-area';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

interface WorkspaceProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface RawWorkspaceEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number;
}

interface FileItem {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size: number;
  children?: FileItem[];
  expanded?: boolean;
}

const CODE_LIKE_EXTENSIONS = new Set([
  'js',
  'jsx',
  'ts',
  'tsx',
  'py',
  'java',
  'c',
  'cpp',
  'cs',
  'go',
  'rs',
  'php',
  'rb',
  'swift',
  'kt',
  'sql',
  'html',
  'css',
  'scss',
  'json',
  'xml',
  'yaml',
  'yml',
  'md',
  'sh',
  'bat',
  'ps1',
]);

function buildFileTree(flatFiles: RawWorkspaceEntry[], expandedPaths: Set<string>): FileItem[] {
  const root: FileItem[] = [];
  const pathMap = new Map<string, FileItem>();
  const sorted = [...flatFiles].sort((a, b) => String(a.path).localeCompare(String(b.path)));

  sorted.forEach((file) => {
    const parts = file.path.split('/');
    let currentPath = '';

    parts.forEach((part, index) => {
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      const isFile = index === parts.length - 1 && !file.is_directory;

      if (!pathMap.has(currentPath)) {
        const item: FileItem = {
          name: part,
          path: currentPath,
          type: isFile ? 'file' : 'directory',
          size: isFile ? file.size : 0,
          children: isFile ? undefined : [],
          expanded: expandedPaths.has(currentPath),
        };
        pathMap.set(currentPath, item);

        if (currentPath.includes('/')) {
          const parentPath = currentPath.substring(0, currentPath.lastIndexOf('/'));
          const parent = pathMap.get(parentPath);
          if (parent && parent.children) {
            parent.children.push(item);
          }
        } else {
          root.push(item);
        }
      }
    });
  });

  const sortItems = (items: FileItem[]): FileItem[] => {
    return [...items]
      .sort((a, b) => {
        if (a.type !== b.type) {
          return a.type === 'directory' ? -1 : 1;
        }
        return a.name.localeCompare(b.name);
      })
      .map((item) =>
        item.type === 'directory' && item.children
          ? { ...item, children: sortItems(item.children) }
          : item
      );
  };

  return sortItems(root);
}

function filterFileTree(items: FileItem[], query: string): FileItem[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return items;

  const visit = (item: FileItem): FileItem | null => {
    const matchesSelf =
      item.name.toLowerCase().includes(normalized) ||
      item.path.toLowerCase().includes(normalized);

    if (item.type === 'directory' && item.children) {
      const matchedChildren = item.children
        .map((child) => visit(child))
        .filter((child): child is FileItem => Boolean(child));
      if (matchesSelf || matchedChildren.length > 0) {
        return { ...item, expanded: true, children: matchedChildren };
      }
      return null;
    }

    return matchesSelf ? item : null;
  };

  return items
    .map((item) => visit(item))
    .filter((item): item is FileItem => Boolean(item));
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function isCodeLikeFile(filename: string): boolean {
  const extension = filename.includes('.') ? filename.split('.').pop()?.toLowerCase() : '';
  if (!extension) return false;
  return CODE_LIKE_EXTENSIONS.has(extension);
}

function basenamePath(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  const parts = normalized.split('/');
  return parts[parts.length - 1] || normalized;
}

function parentPath(path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '');
  const index = normalized.lastIndexOf('/');
  return index >= 0 ? normalized.slice(0, index) : '';
}

function joinWorkspacePath(base: string, name: string): string {
  const cleanedBase = base.trim().replace(/^\/+|\/+$/g, '');
  const cleanedName = name.trim().replace(/^\/+|\/+$/g, '');
  if (!cleanedBase) return cleanedName;
  if (!cleanedName) return cleanedBase;
  return `${cleanedBase}/${cleanedName}`;
}

function isDescendantPath(path: string, possibleParent: string): boolean {
  const normalizedPath = path.replace(/^\/+|\/+$/g, '');
  const normalizedParent = possibleParent.replace(/^\/+|\/+$/g, '');
  return normalizedPath.startsWith(`${normalizedParent}/`);
}

function buildCopyName(name: string, isDirectory: boolean): string {
  if (isDirectory) {
    return `${name}-copy`;
  }
  const dotIndex = name.lastIndexOf('.');
  if (dotIndex <= 0) {
    return `${name}-copy`;
  }
  const stem = name.slice(0, dotIndex);
  const ext = name.slice(dotIndex);
  return `${stem}-copy${ext}`;
}

export function Workspace({ open, onOpenChange }: WorkspaceProps) {
  const [rawEntries, setRawEntries] = useState<RawWorkspaceEntry[]>([]);
  const [files, setFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createFileDialog, setCreateFileDialog] = useState(false);
  const [createFolderDialog, setCreateFolderDialog] = useState(false);
  const [renameDialog, setRenameDialog] = useState(false);
  const [newItemName, setNewItemName] = useState('');
  const [newItemPath, setNewItemPath] = useState('');
  const [renameTargetPath, setRenameTargetPath] = useState('');
  const [renameTargetType, setRenameTargetType] = useState<'file' | 'directory' | null>(null);
  const [renameName, setRenameName] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [selectedFileName, setSelectedFileName] = useState<string>('');
  const [selectedFileContent, setSelectedFileContent] = useState('');
  const [originalFileContent, setOriginalFileContent] = useState('');
  const [fileLoading, setFileLoading] = useState(false);
  const [fileSaving, setFileSaving] = useState(false);
  const [explorerPanePercent, setExplorerPanePercent] = useState(58);
  const [isResizingEditor, setIsResizingEditor] = useState(false);
  const [draggingPath, setDraggingPath] = useState<string | null>(null);
  const [draggingType, setDraggingType] = useState<'file' | 'directory' | null>(null);
  const [dropTargetPath, setDropTargetPath] = useState<string | null>(null);
  const fileUploadRef = useRef<HTMLInputElement>(null);
  const editorSplitContainerRef = useRef<HTMLDivElement>(null);
  const editorTextareaRef = useRef<HTMLTextAreaElement>(null);
  const lineNumbersRef = useRef<HTMLDivElement>(null);

  const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
  const hasUnsavedChanges = selectedFileContent !== originalFileContent;
  const lineCount = useMemo(
    () => Math.max(1, selectedFileContent.split(/\r?\n/).length),
    [selectedFileContent]
  );
  const lineNumbers = useMemo(
    () => Array.from({ length: lineCount }, (_, index) => index + 1),
    [lineCount]
  );

  const loadWorkspace = useCallback(
    async (showLoading = true) => {
      try {
        if (showLoading) {
          setLoading(true);
        }

        const response = await fetch(`${API_URL}/api/workspace/list`);
        if (!response.ok) {
          toast.error('Failed to load workspace');
          return;
        }

        const data = await response.json();
        const entries: RawWorkspaceEntry[] = data.files || [];
        setRawEntries(entries);

        if (selectedFilePath) {
          const fileStillExists = entries.some(
            (entry) => !entry.is_directory && entry.path === selectedFilePath
          );
          if (!fileStillExists) {
            setSelectedFilePath(null);
            setSelectedFileName('');
            setSelectedFileContent('');
            setOriginalFileContent('');
          }
        }
      } catch (error) {
        console.error('Failed to load workspace:', error);
        toast.error('Failed to load workspace');
      } finally {
        if (showLoading) {
          setLoading(false);
        }
      }
    },
    [API_URL, selectedFilePath]
  );

  useEffect(() => {
    setFiles(buildFileTree(rawEntries, expandedPaths));
  }, [rawEntries, expandedPaths]);

  const openFile = useCallback(
    async (path: string, name: string) => {
      try {
        setFileLoading(true);
        const response = await fetch(
          `${API_URL}/api/workspace/read-file?path=${encodeURIComponent(path)}`
        );
        if (!response.ok) {
          throw new Error(await response.text());
        }

        const data = await response.json();
        const content = String(data.content ?? '');
        setSelectedFilePath(path);
        setSelectedFileName(name);
        setSelectedFileContent(content);
        setOriginalFileContent(content);
      } catch (error) {
        console.error('Failed to open file:', error);
        toast.error('Failed to open file');
      } finally {
        setFileLoading(false);
      }
    },
    [API_URL]
  );

  const saveFile = useCallback(async () => {
    if (!selectedFilePath) return;
    if (!hasUnsavedChanges) return;

    const confirmedSave = window.confirm(
      `Confirm save changes to "${selectedFileName || selectedFilePath}"?`
    );
    if (!confirmedSave) return;

    try {
      setFileSaving(true);
      const response = await fetch(`${API_URL}/api/workspace/save-file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: selectedFilePath,
          content: selectedFileContent,
        }),
      });

      if (!response.ok) {
        throw new Error(await response.text());
      }

      setOriginalFileContent(selectedFileContent);
      toast.success('File saved');
      void loadWorkspace(false);
    } catch (error) {
      console.error('Failed to save file:', error);
      toast.error('Failed to save file');
    } finally {
      setFileSaving(false);
    }
  }, [
    API_URL,
    hasUnsavedChanges,
    selectedFileName,
    selectedFilePath,
    selectedFileContent,
    loadWorkspace,
  ]);

  const resetEditorSelection = useCallback(() => {
    setSelectedFilePath(null);
    setSelectedFileName('');
    setSelectedFileContent('');
    setOriginalFileContent('');
  }, []);

  const confirmDiscardUnsavedChanges = useCallback(
    (actionLabel: string): boolean => {
      if (!hasUnsavedChanges) return true;
      return window.confirm(
        `Confirm to ${actionLabel}? Unsaved changes in "${selectedFileName || selectedFilePath || 'current file'}" will be lost.`
      );
    },
    [hasUnsavedChanges, selectedFileName, selectedFilePath]
  );

  const cancelEditing = useCallback(() => {
    if (!selectedFilePath) return;
    const confirmed = confirmDiscardUnsavedChanges('cancel and close the editor');
    if (!confirmed) return;
    resetEditorSelection();
    toast.success('Editor closed');
  }, [confirmDiscardUnsavedChanges, resetEditorSelection, selectedFilePath]);

  const requestOpenFile = useCallback(
    (path: string, name: string) => {
      if (selectedFilePath === path) return;
      const confirmed = confirmDiscardUnsavedChanges('switch files');
      if (!confirmed) return;
      void openFile(path, name);
    },
    [confirmDiscardUnsavedChanges, openFile, selectedFilePath]
  );

  const closeWorkspace = useCallback(() => {
    const confirmed = confirmDiscardUnsavedChanges('close workspace');
    if (!confirmed) return;
    onOpenChange(false);
  }, [confirmDiscardUnsavedChanges, onOpenChange]);

  const syncLineNumberScroll = useCallback(() => {
    if (!editorTextareaRef.current || !lineNumbersRef.current) return;
    lineNumbersRef.current.scrollTop = editorTextareaRef.current.scrollTop;
  }, []);

  useEffect(() => {
    if (!selectedFilePath) return;
    if (!lineNumbersRef.current || !editorTextareaRef.current) return;
    lineNumbersRef.current.scrollTop = 0;
    editorTextareaRef.current.scrollTop = 0;
  }, [selectedFilePath]);

  useEffect(() => {
    if (!isResizingEditor) return;

    const onMouseMove = (event: MouseEvent) => {
      const container = editorSplitContainerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      if (rect.height <= 0) return;

      const nextPercent = ((event.clientY - rect.top) / rect.height) * 100;
      const clamped = Math.max(24, Math.min(76, nextPercent));
      setExplorerPanePercent(clamped);
    };

    const onMouseUp = () => {
      setIsResizingEditor(false);
    };

    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [isResizingEditor]);

  useEffect(() => {
    if (!open) return;
    void loadWorkspace();
  }, [open, loadWorkspace]);

  useEffect(() => {
    if (!open) return;
    const pollId = window.setInterval(() => {
      void loadWorkspace(false);
    }, 3000);
    return () => window.clearInterval(pollId);
  }, [open, loadWorkspace]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!selectedFilePath) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void saveFile();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [selectedFilePath, saveFile]);

  const filteredFiles = useMemo(() => filterFileTree(files, searchQuery), [files, searchQuery]);
  const fileCount = rawEntries.filter((entry) => !entry.is_directory).length;
  const folderCount = rawEntries.filter((entry) => entry.is_directory).length;

  const workspacePost = useCallback(
    async (endpoint: string, payload: Record<string, unknown>) => {
      const response = await fetch(`${API_URL}/api/workspace/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const details = await response.text();
        throw new Error(details || `Request failed: ${endpoint}`);
      }
      return response.json();
    },
    [API_URL]
  );

  const toggleExpanded = useCallback((path: string) => {
    setExpandedPaths((previous) => {
      const nextExpanded = new Set(previous);
      if (nextExpanded.has(path)) {
        nextExpanded.delete(path);
      } else {
        nextExpanded.add(path);
      }
      return nextExpanded;
    });
  }, []);

  const expandAllFolders = useCallback(() => {
    setExpandedPaths(
      new Set(rawEntries.filter((entry) => entry.is_directory).map((entry) => entry.path))
    );
  }, [rawEntries]);

  const collapseAllFolders = useCallback(() => {
    setExpandedPaths(new Set());
  }, []);

  const openCreateFileDialog = useCallback((folderPath = '') => {
    setNewItemPath(folderPath);
    setNewItemName('');
    setCreateFileDialog(true);
  }, []);

  const openCreateFolderDialog = useCallback((folderPath = '') => {
    setNewItemPath(folderPath);
    setNewItemName('');
    setCreateFolderDialog(true);
  }, []);

  const openRenameDialog = useCallback((item: FileItem) => {
    setRenameTargetPath(item.path);
    setRenameTargetType(item.type);
    setRenameName(item.name);
    setRenameDialog(true);
  }, []);

  const renamePath = useCallback(async () => {
    if (!renameTargetPath || !renameName.trim()) return;
    try {
      const nextPath = joinWorkspacePath(parentPath(renameTargetPath), renameName.trim());
      await workspacePost('rename', { path: renameTargetPath, new_path: nextPath });

      if (selectedFilePath === renameTargetPath) {
        setSelectedFilePath(nextPath);
        setSelectedFileName(renameName.trim());
      }
      toast.success(`${renameTargetType === 'directory' ? 'Folder' : 'File'} renamed`);
      setRenameDialog(false);
      setRenameTargetPath('');
      setRenameTargetType(null);
      setRenameName('');
      await loadWorkspace(false);
    } catch (error) {
      console.error('Failed to rename path:', error);
      toast.error('Failed to rename');
    }
  }, [loadWorkspace, renameName, renameTargetPath, renameTargetType, selectedFilePath, workspacePost]);

  const deletePath = useCallback(
    async (item: FileItem) => {
      const confirmed = window.confirm(`Delete ${item.type} "${item.path}"?`);
      if (!confirmed) return;
      try {
        await workspacePost('delete', { path: item.path });
        if (
          selectedFilePath &&
          (selectedFilePath === item.path || isDescendantPath(selectedFilePath, item.path))
        ) {
          setSelectedFilePath(null);
          setSelectedFileName('');
          setSelectedFileContent('');
          setOriginalFileContent('');
        }
        toast.success(`${item.type === 'directory' ? 'Folder' : 'File'} deleted`);
        await loadWorkspace(false);
      } catch (error) {
        console.error('Failed to delete path:', error);
        toast.error('Failed to delete');
      }
    },
    [loadWorkspace, selectedFilePath, workspacePost]
  );

  const copyPath = useCallback(
    async (item: FileItem) => {
      const defaultPath = joinWorkspacePath(
        parentPath(item.path),
        buildCopyName(item.name, item.type === 'directory')
      );
      const destination = window.prompt('Copy to path:', defaultPath);
      if (!destination || !destination.trim()) return;
      try {
        await workspacePost('copy', {
          path: item.path,
          destination_path: destination.trim(),
        });
        toast.success('Copied');
        await loadWorkspace(false);
      } catch (error) {
        console.error('Failed to copy path:', error);
        toast.error('Failed to copy');
      }
    },
    [loadWorkspace, workspacePost]
  );

  const movePath = useCallback(
    async (item: FileItem) => {
      const defaultPath = joinWorkspacePath(parentPath(item.path), item.name);
      const destination = window.prompt('Move to path:', defaultPath);
      if (!destination || !destination.trim()) return;
      if (destination.trim() === item.path) return;
      try {
        await workspacePost('move', {
          path: item.path,
          destination_path: destination.trim(),
        });
        if (selectedFilePath === item.path && item.type === 'file') {
          setSelectedFilePath(destination.trim());
          setSelectedFileName(basenamePath(destination.trim()));
        }
        toast.success('Moved');
        await loadWorkspace(false);
      } catch (error) {
        console.error('Failed to move path:', error);
        toast.error('Failed to move');
      }
    },
    [loadWorkspace, selectedFilePath, workspacePost]
  );

  const canDropIntoDirectory = useCallback(
    (destinationDirPath: string, sourcePath: string | null): boolean => {
      if (!sourcePath) return false;
      if (destinationDirPath === sourcePath) return false;
      if (isDescendantPath(destinationDirPath, sourcePath)) return false;
      const sourceParent = parentPath(sourcePath);
      if (sourceParent === destinationDirPath) return false;
      return true;
    },
    []
  );

  const moveByDrop = useCallback(
    async (destinationDirPath: string) => {
      if (!draggingPath || !draggingType) return;
      if (!canDropIntoDirectory(destinationDirPath, draggingPath)) return;

      const destinationPath = joinWorkspacePath(destinationDirPath, basenamePath(draggingPath));
      try {
        await workspacePost('move', {
          path: draggingPath,
          destination_path: destinationPath,
        });
        if (draggingType === 'file' && selectedFilePath === draggingPath) {
          setSelectedFilePath(destinationPath);
          setSelectedFileName(basenamePath(destinationPath));
        }
        toast.success('Moved');
        setDropTargetPath(null);
        setDraggingPath(null);
        setDraggingType(null);
        await loadWorkspace(false);
      } catch (error) {
        console.error('Failed to move by drag-drop:', error);
        toast.error('Failed to move');
      }
    },
    [
      canDropIntoDirectory,
      draggingPath,
      draggingType,
      loadWorkspace,
      selectedFilePath,
      workspacePost,
    ]
  );

  const uploadToWorkspace = async (selectedFiles: File[]) => {
    if (!selectedFiles.length) return;

    try {
      const formData = new FormData();
      selectedFiles.forEach((file) => {
        const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
        const uploadName = relativePath && relativePath.trim() ? relativePath : file.name;
        formData.append('files', file, uploadName);
      });

      const response = await fetch(`${API_URL}/api/workspace/upload`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error(await response.text());
      }

      const data = await response.json();
      const uploadedCount = Array.isArray(data.files) ? data.files.length : selectedFiles.length;
      toast.success(`${uploadedCount} item(s) uploaded to workspace`);
      await loadWorkspace();
    } catch (error) {
      console.error('Failed to upload to workspace:', error);
      toast.error('Failed to upload to workspace');
    }
  };

  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFiles = Array.from(event.target.files || []);
    event.target.value = '';
    await uploadToWorkspace(selectedFiles);
  };

  const openFolderUpload = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.setAttribute('webkitdirectory', '');
    input.setAttribute('directory', '');
    input.onchange = async () => {
      const selectedFiles = Array.from(input.files || []);
      await uploadToWorkspace(selectedFiles);
    };
    input.click();
  };

  const createFile = async () => {
    if (!newItemName.trim()) return;

    try {
      const path = newItemPath ? `${newItemPath}/${newItemName}` : newItemName;
      const response = await fetch(`${API_URL}/api/workspace/create-file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content: '' }),
      });

      if (response.ok) {
        toast.success('File created successfully');
        setCreateFileDialog(false);
        setNewItemName('');
        setNewItemPath('');
        void loadWorkspace();
      } else {
        const errorText = await response.text();
        toast.error(`Failed to create file: ${errorText}`);
      }
    } catch (error) {
      console.error('Failed to create file:', error);
      toast.error('Failed to create file');
    }
  };

  const createFolder = async () => {
    if (!newItemName.trim()) return;

    try {
      const path = newItemPath ? `${newItemPath}/${newItemName}` : newItemName;
      const response = await fetch(`${API_URL}/api/workspace/create-folder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });

      if (response.ok) {
        toast.success('Folder created successfully');
        setCreateFolderDialog(false);
        setNewItemName('');
        setNewItemPath('');
        void loadWorkspace();
      } else {
        const errorText = await response.text();
        toast.error(`Failed to create folder: ${errorText}`);
      }
    } catch (error) {
      console.error('Failed to create folder:', error);
      toast.error('Failed to create folder');
    }
  };

  const renderFileTree = (items: FileItem[], level = 0) => {
    return items.map((item) => {
      const isDirectory = item.type === 'directory';
      const isSelectedFile = item.type === 'file' && selectedFilePath === item.path;
      const useCodeIcon = item.type === 'file' && isCodeLikeFile(item.name);
      const isDropTarget = isDirectory && dropTargetPath === item.path;
      return (
        <div key={item.path}>
          <div
            className={cn(
              'group/item flex items-center gap-1.5 rounded-sm py-1 pr-2 text-[12.5px] leading-5 cursor-pointer select-none',
              isSelectedFile ? 'bg-primary/20' : 'hover:bg-accent/60',
              isDropTarget && 'bg-primary/15 ring-1 ring-primary/40'
            )}
            style={{ paddingLeft: `${8 + level * 14}px` }}
            title={item.path}
            draggable
            onDragStart={(event) => {
              event.dataTransfer.setData('text/plain', item.path);
              event.dataTransfer.effectAllowed = 'move';
              setDraggingPath(item.path);
              setDraggingType(item.type);
            }}
            onDragEnd={() => {
              setDraggingPath(null);
              setDraggingType(null);
              setDropTargetPath(null);
            }}
            onDragOver={(event) => {
              if (!isDirectory || !canDropIntoDirectory(item.path, draggingPath)) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = 'move';
              if (dropTargetPath !== item.path) {
                setDropTargetPath(item.path);
              }
            }}
            onDragLeave={() => {
              if (dropTargetPath === item.path) {
                setDropTargetPath(null);
              }
            }}
            onDrop={(event) => {
              if (!isDirectory) return;
              event.preventDefault();
              event.stopPropagation();
              void moveByDrop(item.path);
            }}
            onClick={() => {
              if (isDirectory) {
                toggleExpanded(item.path);
                return;
              }
              requestOpenFile(item.path, item.name);
            }}
          >
            {isDirectory ? (
              <>
                {item.expanded ? (
                  <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
                )}
                {item.expanded ? (
                  <FolderOpen className="h-4 w-4 text-sky-500" />
                ) : (
                  <Folder className="h-4 w-4 text-sky-500" />
                )}
              </>
            ) : (
              <>
                <span className="w-3.5" />
                {useCodeIcon ? (
                  <FileCode className="h-4 w-4 text-emerald-500" />
                ) : (
                  <FileText className="h-4 w-4 text-muted-foreground" />
                )}
              </>
            )}

            <span className="truncate flex-1">{item.name}</span>
            {!isDirectory && item.size > 0 && (
              <span className="text-[10px] text-muted-foreground opacity-0 group-hover/item:opacity-100 transition-opacity">
                {formatFileSize(item.size)}
              </span>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 opacity-0 group-hover/item:opacity-100"
                  onClick={(event) => event.stopPropagation()}
                >
                  <MoreHorizontal className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" onClick={(event) => event.stopPropagation()}>
                {isDirectory ? (
                  <>
                    <DropdownMenuItem onSelect={() => openCreateFileDialog(item.path)}>
                      <FilePlus2 className="h-4 w-4" />
                      Add File
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => openCreateFolderDialog(item.path)}>
                      <FolderPlus className="h-4 w-4" />
                      Add Folder
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onSelect={() => openRenameDialog(item)}>
                      <Pencil className="h-4 w-4" />
                      Rename
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => void movePath(item)}>
                      <ChevronRight className="h-4 w-4" />
                      Move
                    </DropdownMenuItem>
                    <DropdownMenuItem variant="destructive" onSelect={() => void deletePath(item)}>
                      <Trash2 className="h-4 w-4" />
                      Delete
                    </DropdownMenuItem>
                  </>
                ) : (
                  <>
                    <DropdownMenuItem onSelect={() => requestOpenFile(item.path, item.name)}>
                      <FileText className="h-4 w-4" />
                      Open
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => void copyPath(item)}>
                      <Copy className="h-4 w-4" />
                      Copy
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => void movePath(item)}>
                      <ChevronRight className="h-4 w-4" />
                      Move
                    </DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => openRenameDialog(item)}>
                      <Pencil className="h-4 w-4" />
                      Rename
                    </DropdownMenuItem>
                    <DropdownMenuItem variant="destructive" onSelect={() => void deletePath(item)}>
                      <Trash2 className="h-4 w-4" />
                      Delete
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          {isDirectory && item.expanded && item.children && item.children.length > 0 && (
            <div className="relative">{renderFileTree(item.children, level + 1)}</div>
          )}
        </div>
      );
    });
  };

  return (
    <>
      <div
        className={cn(
          'h-full bg-card flex flex-col transition-opacity duration-200',
          open ? 'opacity-100' : 'opacity-0 pointer-events-none'
        )}
      >
        <div className="px-3 py-2 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Explorer
          </h2>
          <Button variant="ghost" size="icon" onClick={closeWorkspace}>
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="px-3 py-2 border-b border-border space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Workspace
            </span>
            <span className="text-[11px] text-muted-foreground">
              {folderCount} folders · {fileCount} files
            </span>
          </div>

          <div className="flex items-center gap-1.5">
            <Button size="icon" variant="ghost" title="Upload file" onClick={() => fileUploadRef.current?.click()}>
              <Upload className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" title="Upload folder" onClick={openFolderUpload}>
              <Folder className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" title="New file" onClick={() => openCreateFileDialog()}>
              <Plus className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" title="New folder" onClick={() => openCreateFolderDialog()}>
              <FolderPlus className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" title="Expand all" onClick={expandAllFolders}>
              <ChevronDown className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" title="Collapse all" onClick={collapseAllFolders}>
              <ChevronRight className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              title="Refresh"
              onClick={() => void loadWorkspace()}
              disabled={loading}
            >
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
            </Button>
          </div>

          <div className="relative">
            <Search className="h-3.5 w-3.5 text-muted-foreground absolute left-2.5 top-1/2 -translate-y-1/2" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Filter files..."
              className="h-8 text-xs pl-8"
            />
          </div>

          <input
            ref={fileUploadRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileUpload}
          />
        </div>

        <div className="flex-1 min-h-0 flex flex-col" ref={editorSplitContainerRef}>
          <div
            className={cn('min-h-0', selectedFilePath ? 'border-b border-border' : 'flex-1')}
            style={selectedFilePath ? { height: `${explorerPanePercent}%` } : undefined}
          >
            <ScrollArea className="h-full">
              <div
                className={cn(
                  'p-2',
                  dropTargetPath === '' && 'bg-primary/10 ring-1 ring-primary/30 rounded-sm'
                )}
                onDragOver={(event) => {
                  if (!draggingPath) return;
                  const sourceParent = parentPath(draggingPath);
                  if (!sourceParent) return;
                  event.preventDefault();
                  event.dataTransfer.dropEffect = 'move';
                  if (dropTargetPath !== '') {
                    setDropTargetPath('');
                  }
                }}
                onDrop={(event) => {
                  if (!draggingPath) return;
                  event.preventDefault();
                  event.stopPropagation();
                  void moveByDrop('');
                }}
              >
                {loading ? (
                  <div className="flex items-center justify-center py-8">
                    <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground" />
                  </div>
                ) : filteredFiles.length === 0 ? (
                  <div className="px-2 py-6 text-xs text-muted-foreground">
                    {searchQuery.trim() ? 'No files match this filter.' : 'Workspace is empty.'}
                  </div>
                ) : (
                  renderFileTree(filteredFiles)
                )}
              </div>
            </ScrollArea>
          </div>

          {selectedFilePath && (
            <>
              <div
                role="separator"
                aria-orientation="horizontal"
                aria-label="Resize file editor panel"
                onMouseDown={(event) => {
                  event.preventDefault();
                  setIsResizingEditor(true);
                }}
                className={cn(
                  'h-1 w-full cursor-row-resize border-t border-b border-border',
                  isResizingEditor ? 'bg-border' : 'bg-transparent hover:bg-border/60'
                )}
              />
              <div className="min-h-0 flex flex-col" style={{ height: `${100 - explorerPanePercent}%` }}>
                <div className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
                  <div className="text-sm truncate">
                    <span className="font-medium">{selectedFileName}</span>
                    {hasUnsavedChanges && <span className="text-amber-500 ml-1">*</span>}
                    <span className="text-muted-foreground ml-2">{selectedFilePath}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={cancelEditing}
                      disabled={fileSaving}
                    >
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => void saveFile()}
                      disabled={fileSaving || !hasUnsavedChanges}
                    >
                      <Save className="h-4 w-4 mr-2" />
                      {fileSaving ? 'Saving...' : 'Save'}
                    </Button>
                  </div>
                </div>
                <div className="flex-1 min-h-0">
                  {fileLoading ? (
                    <div className="h-full flex items-center justify-center">
                      <RefreshCw className="h-5 w-5 animate-spin text-muted-foreground" />
                    </div>
                  ) : (
                    <div className="h-full flex bg-background">
                      <div
                        ref={lineNumbersRef}
                        className="w-14 shrink-0 border-r border-border bg-muted/20 text-[12px] text-muted-foreground font-mono overflow-hidden select-none"
                      >
                        <div className="px-2 py-3">
                          {lineNumbers.map((lineNumber) => (
                            <div
                              key={lineNumber}
                              className="h-5 leading-5 text-right pr-1 transition-colors duration-150"
                            >
                              {lineNumber}
                            </div>
                          ))}
                        </div>
                      </div>
                      <textarea
                        ref={editorTextareaRef}
                        value={selectedFileContent}
                        onChange={(e) => setSelectedFileContent(e.target.value)}
                        onScroll={syncLineNumberScroll}
                        className="w-full h-full resize-none px-3 py-3 text-[13px] leading-5 font-mono bg-background border-0 outline-none"
                      />
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      <Dialog open={createFileDialog} onOpenChange={setCreateFileDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create New File</DialogTitle>
            <DialogDescription>Create a new file in the workspace.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="file-path">Path (optional)</Label>
              <Input
                id="file-path"
                placeholder="folder/subfolder"
                value={newItemPath}
                onChange={(e) => setNewItemPath(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="file-name">File Name</Label>
              <Input
                id="file-name"
                placeholder="filename.txt"
                value={newItemName}
                onChange={(e) => setNewItemName(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateFileDialog(false)}>
              Cancel
            </Button>
            <Button onClick={createFile}>Create</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={createFolderDialog} onOpenChange={setCreateFolderDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create New Folder</DialogTitle>
            <DialogDescription>Create a new folder in the workspace.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="folder-path">Path (optional)</Label>
              <Input
                id="folder-path"
                placeholder="parent/folder"
                value={newItemPath}
                onChange={(e) => setNewItemPath(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="folder-name">Folder Name</Label>
              <Input
                id="folder-name"
                placeholder="folder-name"
                value={newItemName}
                onChange={(e) => setNewItemName(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateFolderDialog(false)}>
              Cancel
            </Button>
            <Button onClick={createFolder}>Create</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={renameDialog}
        onOpenChange={(openState) => {
          setRenameDialog(openState);
          if (!openState) {
            setRenameTargetPath('');
            setRenameTargetType(null);
            setRenameName('');
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename {renameTargetType === 'directory' ? 'Folder' : 'File'}</DialogTitle>
            <DialogDescription>{renameTargetPath}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="rename-name">New Name</Label>
            <Input
              id="rename-name"
              value={renameName}
              onChange={(e) => setRenameName(e.target.value)}
              placeholder="Enter new name"
              autoFocus
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void renamePath();
                }
              }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameDialog(false)}>
              Cancel
            </Button>
            <Button onClick={() => void renamePath()} disabled={!renameName.trim()}>
              Rename
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
