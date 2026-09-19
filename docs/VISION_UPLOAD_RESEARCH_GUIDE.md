# Vision Upload API Research Guide

This guide explains how to manually reverse-engineer DeepSeek's vision upload API using browser DevTools and network sniffing.

## Prerequisites

- Chrome/Chromium with DevTools
- mitmproxy (optional, for advanced traffic inspection)
- DeepSeek account with vision access
- Basic understanding of HTTP requests and multipart forms

## Research Approach

### Step 1: Access DeepSeek Chat with Vision

1. Open Chrome DevTools (F12)
2. Navigate to `https://chat.deepseek.com`
3. Login to your account
4. Go to **Network tab** in DevTools
5. Enable "Preserve log" to keep requests across page navigations

### Step 2: Trigger Vision Upload

1. Start a new conversation
2. Look for image upload button (usually a paperclip or image icon)
3. Upload a test image (small PNG/JPG, <1MB)
4. Send a message with the image
5. Watch Network tab for new requests

### Step 3: Identify Upload Endpoint

Look for requests with these characteristics:
- **Method**: POST
- **Content-Type**: multipart/form-data
- **URL pattern**: likely contains `/upload`, `/file`, or `/image`
- **Response**: JSON with file_id or similar identifier

Example request to look for:
```
POST https://chat.deepseek.com/api/v0/chat/file_upload
Content-Type: multipart/form-data; boundary=----WebKitFormBoundary...

------WebKitFormBoundary...
Content-Disposition: form-data; name="file"; filename="test.png"
Content-Type: image/png

[binary image data]
------WebKitFormBoundary...--
```

### Step 4: Analyze Request Structure

For each upload request, document:

1. **Endpoint URL**: Full path including any version numbers
2. **Headers**: 
   - Authorization (Bearer token?)
   - Content-Type (multipart boundary)
   - Any custom headers (X-Request-ID, etc.)
3. **Form fields**:
   - Field names (file, image, attachment, etc.)
   - Additional metadata fields
4. **Response structure**:
   - Success response format
   - Error response format
   - How file_id is returned

### Step 5: Test with curl

Once you identify the endpoint, test with curl:

```bash
curl -X POST https://chat.deepseek.com/api/v0/chat/file_upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Accept: application/json" \
  -F "file=@/path/to/test.png" \
  -v
```

### Step 6: Analyze Completion Request

After upload, send a vision message and inspect the completion request:

1. Look for `/v1/chat/completions` request
2. Check how `ref_file_ids` or image references are included
3. Document the message structure:

```json
{
  "model": "deepseek-chat",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image", "image_id": "file_abc123"}
      ]
    }
  ]
}
```

## Expected Findings

Based on typical chat APIs, you should find:

### Upload Endpoint
- **URL**: `/api/v0/chat/file_upload` or similar
- **Method**: POST multipart/form-data
- **Auth**: Bearer token from login
- **Response**: `{"file_id": "file_xxx", "filename": "test.png", "size": 12345}`

### Completion Request
- **Image reference**: Either `ref_file_ids: ["file_xxx"]` or inline in message content
- **Message format**: OpenAI-compatible with image_url or image type

### Rate Limits & Restrictions
- Max file size (likely 10-20MB)
- Supported formats (PNG, JPG, GIF, WebP)
- Upload rate limits (requests per minute)
- Vision model availability

## Implementation Checklist

Once you have the API details, implement:

- [ ] `deepseek/upload.py` - Upload client class
- [ ] `router/adapters/bridge.py` - Update to handle image_url
- [ ] `router/adapters/deepinfra.py` - Update vision routing
- [ ] Test with various image formats and sizes
- [ ] Add error handling for upload failures
- [ ] Document in `docs/vision.md`

## Troubleshooting

### No upload requests appearing
- Vision feature might be disabled for your account
- Try different image formats
- Check if you're in the right chat mode

### Upload fails with 403/401
- Token might be expired
- Vision access might require specific plan
- Check if upload endpoint requires different auth

### Completion request doesn't reference uploaded file
- File might be embedded as base64 in message content
- Check for temporary file URLs instead of file_ids
- Look for different message structure

## Example Implementation Skeleton

```python
# deepseek/upload.py
import httpx
from pathlib import Path

class DeepSeekUploader:
    def __init__(self, base_url: str, token: str):
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"}
        )
    
    def upload_image(self, image_path: Path) -> str:
        """Upload image and return file_id"""
        with open(image_path, "rb") as f:
            response = self.client.post(
                "/api/v0/chat/file_upload",  # Update with actual endpoint
                files={"file": (image_path.name, f, "image/png")}
            )
        response.raise_for_status()
        return response.json()["file_id"]
```

## Next Steps

After completing research:

1. Update `config/routing.yaml` to enable bridge vision routing
2. Implement upload logic in bridge adapter
3. Add integration tests with real images
4. Update this guide with actual API findings

## Resources

- [DeepSeek Official Chat](https://chat.deepseek.com)
- [Chrome DevTools Network Panel](https://developer.chrome.com/docs/devtools/network)
- [mitmproxy Documentation](https://docs.mitmproxy.org/)
- [HTTP Multipart RFC](https://datatracker.ietf.org/doc/html/rfc7578)
