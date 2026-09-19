# Phase 2: Vision Reverse-Engineering - Manual Guide

## Status: ❌ BLOCKED (Requires Human Action)

### Why This Can't Be Automated
Phase 2 requires **manual reverse-engineering** of DeepSeek's upload API using:
1. Browser DevTools (F12) - Network tab
2. Manual login to chat.deepseek.com
3. Manual image upload to capture API calls
4. Network traffic inspection

### Step-by-Step Instructions

#### 1. Setup Network Inspection
```bash
# Open Chrome/Chromium
# Press F12 to open DevTools
# Go to Network tab
# Enable "Preserve log" checkbox
# Clear any existing requests (🚫 icon)
```

#### 2. Login and Upload Image
1. Navigate to https://chat.deepseek.com
2. Login with your account
3. Start a new conversation
4. Click the image upload button (usually 📎 or image icon)
5. Upload a small test image (<1MB PNG/JPG)
6. Add text: "Describe this image"
7. Send the message

#### 3. Capture Upload API Call
In Network tab, look for:
- **Method**: POST
- **URL pattern**: contains `/upload`, `/file`, or `/image`
- **Content-Type**: `multipart/form-data`
- **Status**: 200 OK

Click the request → **Payload** tab to see:
- File field name
- Any additional form fields
- Authorization headers

Click **Response** tab to see:
- Response format (JSON?)
- How file_id is returned
- Any metadata

#### 4. Capture Completion Request
After upload, look for the `/v1/chat/completions` request:
- Click **Payload** tab
- Check how image is referenced in messages
- Is it `ref_file_ids: ["file_xxx"]`?
- Or inline in message content?
- Or a temporary URL?

#### 5. Document Findings
Create file: `~/dev/deepseek-api-fork/docs/vision_api.md`

Include:
```markdown
## Upload Endpoint
- URL: /api/v0/chat/???
- Method: POST
- Headers: Authorization: Bearer *** ...
- Form fields:
  - file: (binary image data)
  - ??? (other fields)
- Response:
  ```json
  {
    "file_id": "file_xxx",
    "filename": "test.png",
    "size": 12345
  }
  ```

## Completion Request
- How image referenced:
  - Option A: `ref_file_ids: ["file_xxx"]`
  - Option B: Inline in message content
  - Option C: Temporary URL
- Example message structure:
  ```json
  {
    "model": "deepseek-chat",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "Describe this image"},
          {"type": "image", "image_id": "file_xxx"}
        ]
      }
    ]
  }
  ```
```

#### 6. Implement After Documentation
Once you have the API details, I can implement:
- `deepseek/upload.py` - Upload client
- Update `server/openai_format.py` - Handle image_url
- Update `router/adapters/bridge.py` - Vision support
- Integration tests

### Estimated Time
- Manual research: 2-3 hours
- Implementation (after docs): 3-4 hours (can delegate to subagent)

### Alternative: Skip Phase 2
If vision upload is too complex, we can:
1. Remove bridge vision routing from `config/routing.yaml`
2. Keep only DeepInfra GLM-5.3-Flash for vision
3. Mark Phase 2 as "deferred"

### Current Vision Routing (Works)
Vision requests already route to **DeepInfra GLM-5.3-Flash** (working):
```yaml
# config/routing.yaml
vision:
  primary: deepinfra:GLM-5.3-Flash
  fallbacks:
    - deepseek-bridge:v4-instant
```

### Next Steps
Choose one:
1. **Do manual research** → Document API → I implement Phase 2
2. **Skip Phase 2** → Keep GLM-5.3-Flash only → Mark as deferred
3. **Defer indefinitely** → Focus on other features

Let me know which option you prefer.
