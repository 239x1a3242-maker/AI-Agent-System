# Cloudflare Worker AI Setup

This directory contains the Cloudflare Worker code to replace the HuggingFace Space for AI inference.

## Files

- `worker.js` - The main worker code that handles chat requests
- `wrangler.toml` - Configuration for deploying the worker
- `worker2.js` - Image-to-text/description worker using `@cf/llava-hf/llava-1.5-7b-hf`
- `wrangler.worker2.toml` - Configuration for deploying `worker2.js`

## Deployment

1. Install Wrangler CLI:
   ```bash
   npm install -g wrangler
   ```

2. Login to Cloudflare:
   ```bash
   wrangler auth login
   ```

3. Deploy the worker:
   ```bash
   wrangler deploy
   ```

4. The worker will be deployed to `https://your-text-worker.your-subdomain.workers.dev`

5. Deploy the image worker:
   ```bash
   wrangler deploy --config wrangler.worker2.toml
   ```

6. Set backend env for image extraction:
   ```bash
   IMAGE_WORKER_URL=https://your-image-worker.your-subdomain.workers.dev
   ```

## API

The worker provides the same API as the HuggingFace Space:

- `POST /chat` - Stream chat completions
- `GET /health` - Health check

The image worker (`worker2.js`) provides:
- `POST /describe` - Image description/text extraction
- `GET /health` - Health check

### Image Request Format (`worker2.js`)

```json
{
  "filename": "invoice.png",
  "mime_type": "image/png",
  "prompt": "Describe this image and transcribe all visible text.",
  "image": "<base64-image-bytes>"
}
```

### Chat Request Format

```json
{
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true,
  "max_tokens": 8192,
  "temperature": 0.6,
  "tools": [] // optional
}
```

### Response

Returns Server-Sent Events (SSE) with the following format:

```
event: token
data: {"type": "token", "content": "Hello"}

event: done
data: {"type": "done", "content": ""}
```
