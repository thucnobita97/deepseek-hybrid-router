#!/bin/bash
# Test reasoning stability with expert model

echo "=== Reasoning Stability Test (Expert Model) ==="
echo "Sending 5 complex reasoning requests to DeepSeek expert"
echo ""

success=0
fail=0
total_latency=0

questions=(
    "Explain the difference between TCP and UDP protocols. When should you use each?"
    "Compare microservices vs monolithic architecture. What are the trade-offs?"
    "What is the time complexity of quicksort? Explain the best, worst, and average cases."
    "How does garbage collection work in Java? Explain the different algorithms."
    "What is the CAP theorem? How does it affect distributed database design?"
)

for i in {1..5}; do
    echo "Request $i/5: ${questions[$i-1]:0:60}..."
    start=$(date +%s%N)
    
    response=$(curl -s -w "HTTP_CODE:%{http_code}" -X POST http://localhost:8001/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d "{
        \"model\": \"deepseek-chat\",
        \"messages\": [
          {\"role\": \"user\", \"content\": \"${questions[$i-1]}\"},
          {\"role\": \"system\", \"content\": \"You are a helpful assistant. Provide a concise, accurate answer in 2-3 sentences.\"}
        ],
        \"temperature\": 0.7
      }" 2>&1)
    
    end=$(date +%s%N)
    latency=$((($end - $start) / 1000000))
    
    http_code=$(echo "$response" | grep -o 'HTTP_CODE:[0-9]*' | cut -d: -f2)
    
    if [ "$http_code" = "200" ]; then
        success=$((success + 1))
        total_latency=$((total_latency + latency))
        
        # Extract content length
        content=$(echo "$response" | jq -r '.choices[0].message.content' 2>/dev/null)
        content_len=${#content}
        
        echo "  ✓ Success (${latency}ms, ${content_len} chars)"
        echo "  Response: ${content:0:80}..."
    else
        fail=$((fail + 1))
        echo "  ✗ Failed (HTTP $http_code, ${latency}ms)"
        echo "  Response: $(echo "$response" | head -c 200)"
    fi
    
    echo ""
    sleep 2  # Rate limiting
done

echo "=== Results ==="
echo "Success: $success/5"
echo "Failed: $fail/5"

if [ $success -gt 0 ]; then
    avg_latency=$((total_latency / success))
    echo "Average latency (successful): ${avg_latency}ms"
fi

if [ $fail -gt 0 ]; then
    echo ""
    echo "Warning: $fail requests failed. Check bridge logs for details."
fi
