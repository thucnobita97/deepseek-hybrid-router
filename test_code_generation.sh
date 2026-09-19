#!/bin/bash
# Test DeepSeek bridge code generation: basic to advanced

echo "=== DeepSeek Bridge Code Generation Test ==="
echo "Testing from basic to advanced coding challenges"
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

test_code() {
    local level=$1
    local description=$2
    local prompt=$3
    
    echo -e "${YELLOW}[$level] $description${NC}"
    start=$(date +%s%N)
    
    response=$(curl -s -X POST http://localhost:8001/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d "{
        \"model\": \"deepseek-chat\",
        \"messages\": [
          {\"role\": \"user\", \"content\": \"$prompt\"}
        ]
      }" 2>&1)
    
    end=$(date +%s%N)
    latency=$((($end - $start) / 1000000))
    
    # Extract content (handle both bridge and standard format)
    content=$(echo "$response" | grep -o '"content":"[^"]*"' | head -1 | sed 's/"content":"\(.*\)"/\1/')
    content_len=${#content}
    
    # Check if code blocks present
    has_code=$(echo "$content" | grep -c '```' || true)
    
    if [ $content_len -gt 100 ] && [ $has_code -gt 0 ]; then
        echo -e "${GREEN}✓ Success${NC} (${latency}ms, ${content_len} chars, ${has_code} code blocks)"
        # Show first 200 chars of content
        echo "  Preview: ${content:0:200}..."
    else
        echo -e "${RED}✗ Failed${NC} (${latency}ms, ${content_len} chars)"
        echo "  Response: $(echo "$response" | head -c 300)"
    fi
    
    echo ""
    sleep 2
}

# BASIC Level
echo "=== BASIC LEVEL ==="
test_code "BASIC" "String reversal" \
    "Write a Python function to reverse a string without using built-in reverse methods."

test_code "BASIC" "Palindrome check" \
    "Write a Python function to check if a string is a palindrome."

test_code "BASIC" "Fibonacci sequence" \
    "Write a Python function to generate the first n Fibonacci numbers."

# INTERMEDIATE Level
echo ""
echo "=== INTERMEDIATE LEVEL ==="
test_code "INTERMEDIATE" "Binary search" \
    "Implement binary search algorithm in Python with proper error handling."

test_code "INTERMEDIATE" "Linked list operations" \
    "Write a Python class for a singly linked list with insert, delete, and search methods."

test_code "INTERMEDIATE" "Hash table implementation" \
    "Implement a simple hash table in Python with put, get, and delete operations. Handle collisions."

# ADVANCED Level
echo ""
echo "=== ADVANCED LEVEL ==="
test_code "ADVANCED" "LRU Cache" \
    "Implement an LRU (Least Recently Used) cache in Python with O(1) get and put operations. Use OrderedDict or implement from scratch."

test_code "ADVANCED" "Graph BFS and DFS" \
    "Implement breadth-first search and depth-first search algorithms for a graph. Include cycle detection."

test_code "ADVANCED" "Decorator pattern" \
    "Write a Python decorator that measures execution time of functions. Include support for both sync and async functions."

# EXPERT Level
echo ""
echo "=== EXPERT LEVEL ==="
test_code "EXPERT" "Rate limiter" \
    "Implement a rate limiter using the token bucket algorithm in Python. Support configurable rate and burst capacity."

test_code "EXPERT" "Consistent hashing" \
    "Implement consistent hashing for distributed systems in Python. Include virtual nodes for better distribution."

test_code "EXPERT" "Event-driven architecture" \
    "Write a simple event-driven system in Python with publishers, subscribers, and event bus. Support filtering and async handlers."

echo ""
echo "=== Test Complete ==="
