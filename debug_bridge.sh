#!/bin/bash

echo "=== Debug Bridge Responses ==="
echo ""

# Test 1: Simple request
echo "[Test 1] Simple math"
response=$(curl -s -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat", "messages": [{"role": "user", "content": "What is 1+1?"}]}')
echo "$response" | jq -r '.choices[0].message.content'
echo "Length: $(echo "$response" | jq -r '.choices[0].message.content' | wc -c) chars"
echo ""

# Test 2: Code request
echo "[Test 2] Simple code"
response=$(curl -s -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat", "messages": [{"role": "user", "content": "Write a Python function to reverse a string"}]}')
content=$(echo "$response" | jq -r '.choices[0].message.content')
echo "Length: $(echo "$content" | wc -c) chars"
echo "Preview: $(echo "$content" | head -c 200)"
echo ""

# Test 3: Check if conversation_id reuse causes issues
echo "[Test 3] Conversation continuity"
response1=$(curl -s -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-chat", "messages": [{"role": "user", "content": "My name is Alice"}]}')
conv_id=$(echo "$response1" | jq -r '.id')
echo "First response length: $(echo "$response1" | jq -r '.choices[0].message.content' | wc -c) chars"
echo "Conversation ID: $conv_id"

# Second message in same conversation
response2=$(curl -s -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"deepseek-chat\", \"messages\": [{\"role\": \"user\", \"content\": \"My name is Alice\"}, {\"role\": \"assistant\", \"content\": \"$(echo "$response1" | jq -r '.choices[0].message.content')\"}, {\"role\": \"user\", \"content\": \"What is my name?\"}]}")
echo "Second response length: $(echo "$response2" | jq -r '.choices[0].message.content' | wc -c) chars"
echo "Second response: $(echo "$response2" | jq -r '.choices[0].message.content')"
