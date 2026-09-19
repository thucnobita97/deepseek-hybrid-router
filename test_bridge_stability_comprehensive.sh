#!/bin/bash

# Comprehensive Bridge Stability Test
# Test scenarios: consecutive requests, long conversations, edge cases, error recovery

echo "=== DeepSeek Bridge Comprehensive Stability Test ==="
echo "Testing multiple scenarios to evaluate bridge reliability"
echo ""

SUCCESS=0
FAIL=0
TOTAL=0

# Helper function to test a request
test_request() {
    local test_name="$1"
    local prompt="$2"
    local timeout="${3:-60}"
    
    echo "[$test_name]"
    start=$(date +%s)
    
    response=$(timeout $timeout curl -s -X POST http://localhost:8001/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d "{
            \"model\": \"deepseek-chat\",
            \"messages\": [{\"role\": \"user\", \"content\": \"$prompt\"}]
        }" 2>&1)
    
    end=$(date +%s)
    duration=$((end - start))
    
    # Check if response contains expected fields
    if echo "$response" | grep -q '"choices"'; then
        content_length=$(echo "$response" | grep -o '"content":"[^"]*"' | wc -c)
        echo "  ✓ Success (${duration}s, ${content_length} chars)"
        SUCCESS=$((SUCCESS + 1))
    else
        echo "  ✗ Failed (${duration}s)"
        echo "  Response: $(echo "$response" | head -c 200)"
        FAIL=$((FAIL + 1))
    fi
    
    TOTAL=$((TOTAL + 1))
    echo ""
}

# Test 1: Rapid Fire (10 requests in quick succession)
echo "=== Test 1: Rapid Fire (10 quick requests) ==="
for i in {1..10}; do
    test_request "Rapid-$i" "What is $i + $i? Answer in one word." 30
    sleep 1
done

# Test 2: Long Conversation (simulate multi-turn)
echo "=== Test 2: Long Conversation (5 turns) ==="
CONV_ID=""
for turn in {1..5}; do
    if [ $turn -eq 1 ]; then
        test_request "Conv-Turn-$turn" "Let's discuss Python decorators. Start with a simple example." 60
    else
        test_request "Conv-Turn-$turn" "Continue with a more advanced decorator example." 60
    fi
    sleep 2
done

# Test 3: Edge Cases
echo "=== Test 3: Edge Cases ==="
test_request "Edge-Long-Prompt" "$(python3 -c "print('Explain quantum computing. ' * 50)")" 60
test_request "Edge-Special-Chars" "What is the meaning of: @#\$%^&*()_+-=[]{}|;':\",./<>? in programming?" 30
test_request "Edge-Empty-Response" "Say exactly: hello" 20
test_request "Edge-Code-Heavy" "Write a complete REST API in Flask with authentication, database, and Docker support. Keep it under 100 lines." 90

# Test 4: Mixed Workload (different types)
echo "=== Test 4: Mixed Workload ==="
test_request "Mixed-Reasoning" "If all roses are flowers and some flowers fade quickly, can we conclude that some roses fade quickly? Explain your reasoning." 60
test_request "Mixed-Creative" "Write a haiku about programming bugs." 30
test_request "Mixed-Technical" "Explain the difference between TCP and UDP in 3 sentences." 45
test_request "Mixed-Translation" "Translate to French: 'The quick brown fox jumps over the lazy dog'" 30

# Test 5: Stress Test (back-to-back complex requests)
echo "=== Test 5: Stress Test (5 complex requests back-to-back) ==="
for i in {1..5}; do
    test_request "Stress-$i" "Implement a binary search tree in Python with insert, delete, and search operations. Include time complexity analysis." 90
    sleep 3
done

# Summary
echo "=== Test Summary ==="
echo "Total: $TOTAL"
echo "Success: $SUCCESS"
echo "Failed: $FAIL"
echo "Success Rate: $(( SUCCESS * 100 / TOTAL ))%"
echo ""

if [ $FAIL -eq 0 ]; then
    echo "🎉 All tests passed! Bridge is stable."
elif [ $(( FAIL * 100 / TOTAL )) -lt 20 ]; then
    echo "✓ Bridge is mostly stable with minor issues."
else
    echo "⚠ Bridge has significant stability issues."
fi
