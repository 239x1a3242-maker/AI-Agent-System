import { useState, useEffect, useCallback } from 'react';
import { Mail, Server, Brain, Save, Loader2, Settings as SettingsIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';

interface SettingsProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface SettingsData {
  smtp_host: string;
  smtp_port: number;
  smtp_username: string;
  smtp_password: string;
  hf_space_url: string;
  custom_instructions: string;
}

interface ModelStatus {
  status: string;
  error?: string;
}

export function Settings({ open, onOpenChange }: SettingsProps) {
  const [settings, setSettings] = useState<SettingsData>({
    smtp_host: '',
    smtp_port: 587,
    smtp_username: '',
    smtp_password: '',
    hf_space_url: 'https://your-text-worker.your-subdomain.workers.dev',
    custom_instructions: '',
  });
  const [modelStatus, setModelStatus] = useState<ModelStatus>({ status: 'checking' });
  const [isSaving, setIsSaving] = useState(false);

  const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

  const loadSettings = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/api/settings`);
      if (response.ok) {
        const data = await response.json();
        setSettings(prev => ({
          ...prev,
          smtp_host: data.smtp_host || '',
          smtp_port: data.smtp_port || 587,
          smtp_username: data.smtp_username || '',
          hf_space_url: data.hf_space_url || 'https://your-text-worker.your-subdomain.workers.dev',
          custom_instructions: data.custom_instructions || '',
        }));
      }
    } catch (error) {
      console.error('Failed to load settings:', error);
    }
  }, [API_URL]);

  const checkModelStatus = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/api/model-status`);
      if (response.ok) {
        const data = await response.json();
        setModelStatus(data);
      } else {
        setModelStatus({ status: 'error', error: 'Failed to check status' });
      }
    } catch {
      setModelStatus({ status: 'offline', error: 'Cannot connect to backend' });
    }
  }, [API_URL]);

  // Load settings on mount
  useEffect(() => {
    if (open) {
      void loadSettings();
      void checkModelStatus();
    }
  }, [checkModelStatus, loadSettings, open]);

  // Check model status periodically
  useEffect(() => {
    if (!open) return;

    const interval = setInterval(() => {
      void checkModelStatus();
    }, 10000);
    return () => clearInterval(interval);
  }, [checkModelStatus, open]);

  const saveSettings = async () => {
    try {
      setIsSaving(true);
      const response = await fetch(`${API_URL}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          smtp_username: settings.smtp_username,
          smtp_password: settings.smtp_password,
          hf_space_url: settings.hf_space_url,
          custom_instructions: settings.custom_instructions,
        }),
      });

      if (response.ok) {
        toast.success('Settings saved successfully');
      } else {
        toast.error('Failed to save settings');
      }
    } catch (error) {
      toast.error('Failed to save settings');
      console.error('Save error:', error);
    } finally {
      setIsSaving(false);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'loaded':
        return 'text-green-500 bg-green-500/10';
      case 'loading':
        return 'text-yellow-500 bg-yellow-500/10';
      case 'error':
      case 'offline':
        return 'text-red-500 bg-red-500/10';
      default:
        return 'text-gray-500 bg-gray-500/10';
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case 'loaded':
        return 'Model Loaded & Ready';
      case 'loading':
        return 'Loading Model...';
      case 'error':
        return 'Error Loading Model';
      case 'offline':
        return 'Model Offline';
      default:
        return 'Checking Status...';
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SettingsIcon className="h-4 w-4" />
            Settings
          </DialogTitle>
        </DialogHeader>

        <Tabs defaultValue="model" className="mt-4">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="model" className="flex items-center gap-2">
              <Brain className="h-4 w-4" />
              Model
            </TabsTrigger>
            <TabsTrigger value="email" className="flex items-center gap-2">
              <Mail className="h-4 w-4" />
              Email
            </TabsTrigger>
            <TabsTrigger value="advanced" className="flex items-center gap-2">
              <Server className="h-4 w-4" />
              Advanced
            </TabsTrigger>
          </TabsList>

          {/* Model Status Tab */}
          <TabsContent value="model" className="space-y-4">
            <div className="p-4 rounded-lg border">
              <h3 className="font-medium mb-4">Model Status</h3>
              
              <div className={`p-4 rounded-lg ${getStatusColor(modelStatus.status)}`}>
                <div className="flex items-center gap-3">
                  <div className={`w-3 h-3 rounded-full ${
                    modelStatus.status === 'loaded' ? 'bg-green-500 animate-pulse' :
                    modelStatus.status === 'loading' ? 'bg-yellow-500 animate-pulse' :
                    'bg-red-500'
                  }`} />
                  <div>
                    <div className="font-medium">{getStatusText(modelStatus.status)}</div>
                    {modelStatus.error && (
                      <div className="text-sm opacity-80 mt-1">{modelStatus.error}</div>
                    )}
                  </div>
                </div>
              </div>

              <div className="mt-4 text-sm text-muted-foreground">
                <p>Connected to: <code className="bg-muted px-1 py-0.5 rounded">{settings.hf_space_url}</code></p>
              </div>
            </div>

            <div className="p-4 rounded-lg border">
              <h3 className="font-medium mb-4">Custom Instructions</h3>
              <p className="text-sm text-muted-foreground mb-3">
                Add custom instructions that will be appended to the system prompt for all conversations.
              </p>
              <Textarea
                value={settings.custom_instructions}
                onChange={(e) => setSettings(prev => ({ ...prev, custom_instructions: e.target.value }))}
                placeholder="Enter custom instructions..."
                rows={6}
              />
            </div>
          </TabsContent>

          {/* Email Tab */}
          <TabsContent value="email" className="space-y-4">
            <div className="p-4 rounded-lg border">
              <h3 className="font-medium mb-2">SMTP Configuration</h3>
              <p className="text-sm text-muted-foreground mb-4">
                Configure email settings for the send_email tool. Your password is stored securely.
              </p>

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="smtp_host">SMTP Host</Label>
                    <Input
                      id="smtp_host"
                      value={settings.smtp_host}
                      readOnly
                      disabled
                      placeholder="smtp.gmail.com"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="smtp_port">SMTP Port</Label>
                    <Input
                      id="smtp_port"
                      type="number"
                      value={settings.smtp_port}
                      readOnly
                      disabled
                      placeholder="587"
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="smtp_username">Email Address</Label>
                  <Input
                    id="smtp_username"
                    type="email"
                    value={settings.smtp_username}
                    onChange={(e) => setSettings(prev => ({ ...prev, smtp_username: e.target.value }))}
                    placeholder="your-email@gmail.com"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="smtp_password">App Password</Label>
                  <Input
                    id="smtp_password"
                    type="password"
                    value={settings.smtp_password}
                    onChange={(e) => setSettings(prev => ({ ...prev, smtp_password: e.target.value }))}
                    placeholder="********"
                  />
                  <p className="text-xs text-muted-foreground">
                    For Gmail, use an <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer" className="text-primary underline">App Password</a> instead of your regular password.
                  </p>
                </div>
              </div>
            </div>
          </TabsContent>

          {/* Advanced Tab */}
          <TabsContent value="advanced" className="space-y-4">
            <div className="p-4 rounded-lg border">
              <h3 className="font-medium mb-2">HuggingFace Space URL</h3>
              <p className="text-sm text-muted-foreground mb-3">
                The URL of your HuggingFace Space running the Nanbeige4.1-3B model.
              </p>
              <Input
                value={settings.hf_space_url}
                onChange={(e) => setSettings(prev => ({ ...prev, hf_space_url: e.target.value }))}
                placeholder="https://your-space.hf.space"
              />
            </div>
          </TabsContent>
        </Tabs>

        {/* Save Button */}
        <div className="flex justify-end gap-2 mt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={saveSettings} disabled={isSaving}>
            {isSaving ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Saving...
              </>
            ) : (
              <>
                <Save className="h-4 w-4 mr-2" />
                Save Settings
              </>
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
