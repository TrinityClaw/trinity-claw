import sys
import requests
import os
from dotenv import load_dotenv

load_dotenv()

AGENT_URL = "http://localhost:8001"
ASCII_ART = """
╔════════════════════════════════════════╗
║       ✨ TRINITY CLAW v1.3 ✨          ║
║                                        ║
║   Advanced AI Agent with Memory        ║
║   Powered by Ollama + LiteLLM         ║
╚════════════════════════════════════════╝

[TrinityClaw v1.3] :: Connected
"""

def main():
    print(ASCII_ART)
    print("Type 'exit' to quit. Type 'exec: <code>' to run sandboxed code.")
    print("-" * 50)
    
    while True:
        try:
            user_input = input("> ").strip()
            if user_input.lower() in ['exit', 'quit']:
                print("Shutting down claw.")
                break
            
            if user_input.startswith("exec:"):
                code = user_input[5:].strip()
                res = requests.post(f"{AGENT_URL}/sandbox/exec", json={"message": code})
            else:
                res = requests.post(f"{AGENT_URL}/chat", json={"message": user_input})
            
            if res.status_code == 200:
                print(res.json().get('reply') or res.json().get('output'))
            else:
                print(f"Error: {res.text}")
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Connection Error: {e}")

if __name__ == "__main__":
    main()
