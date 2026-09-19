#!/bin/bash
# Test bridge stability with batch requests

echo "=== Bridge Stability Test ==="
echo "Sending 10 requests to test DeepSeek bridge (free tier)"
echo ""

success=0
fail=0
total_latency=0

for i in {1..10}; do
    echo "Request $i/10..."
    start=$(date +%s%N)
    
    response=$(curl -s -w "HTTP_CODE:%{http_code}" -X POST http://localhost:8001/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "What is the capital of France? Answer in one sentence."}]
      }' 2>&1)
    
    end=$(date +%s%N)
    latency=$((($end - $start) / 1000000))
    
    http_code=$(echo "$response" | grep -o 'HTTP_CODE:[0-9]*' | cut -d: -f2)
    
    if [ "$http_code" = "200" ]; then
        success=$((success + 1))
        total_latency=$((total_latency + latency))
        echo "  ✓ Success (${latency}ms)"
    else
        fail=$((fail + 1))
        echo "  ✗ Failed (HTTP $http_code, ${latency}ms)"
        echo "  Response: $(echo "$response" | head -c 200)"
    fi
    
    echo ""
done

echo "=== Results ==="
echo "Success: $success/10"
echo "Failed: $fail/10"

if [ $success -gt 0 ]; then
    avg_latency=$((total_latency / success))
    echo "Average latency (successful): ${avg_latency}ms"
fi

if [ $fail -gt 0 ]; then
    echo ""
    echo "Warning: $fail requests failed. Check bridge logs for details."
fi
