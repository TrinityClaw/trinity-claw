#!/bin/bash
# Ollama entrypoint script - auto-pulls models on startup

# Start Ollama in background
/bin/ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready
echo "⏳ Waiting for Ollama to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "✅ Ollama is ready"
        break
    fi
    echo "  Attempt $i/30..."
    sleep 2
done

# Pull qwen3.5:9b if not already present
echo "📥 Checking for qwen3.5:9b model..."
if ! curl -s http://localhost:11434/api/tags | grep -q 'qwen3.5'; then
    echo "🔄 Pulling qwen3.5:9b (this may take several minutes, ~6.6GB)..."
    ollama pull qwen3.5:9b
    if [ $? -eq 0 ]; then
        echo "✅ qwen3.5:9b pulled successfully"
    else
        echo "⚠️  Failed to pull qwen3.5:9b, but continuing..."
    fi
else
    echo "✅ qwen3.5:9b already present"
fi

# Keep Ollama running
wait $OLLAMA_PID
